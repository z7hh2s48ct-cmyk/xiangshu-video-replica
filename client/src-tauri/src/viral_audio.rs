//! 客户端本地抽音轨（P2 · 决策 #15/C）。
//!
//! 已缓存的视频 → 单声道 16kHz 32k AAC。参数与服务端
//! `media_tools.extract_audio` **逐项一致**（`-vn -ac 1 -ar 16000 -c:a aac
//! -b:a 32k`），否则同一段音频在本地链路与服务端链路会得到不同的转写结果。
//!
//! ffmpeg/ffprobe 随 NSIS 分发（`bundle.resources` 打进 `resources/ffmpeg/`），
//! 因此这里**不依赖 PATH**；找不到就 fail-closed 报明确错误，不静默降级——
//! 与 `media_tools.resolve_media_binary` 是同一姿态。

use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::Manager;

use crate::viral_cache::{safe_segment, ViralCache};

/// 在资源目录下按候选相对路径找可执行文件。
///
/// Tauri 按 `bundle.resources` 里写的相对路径原样复制，所以
/// `resources/ffmpeg/ffmpeg.exe` 会落在 `<resource_dir>/resources/ffmpeg/`；
/// 另外两个候选覆盖不同打包口径，免得升级 Tauri 后这里悄悄失效。
pub fn find_within(resource_dir: &Path, file: &str) -> Option<PathBuf> {
    [
        format!("resources/ffmpeg/{file}"),
        format!("ffmpeg/{file}"),
        file.to_string(),
    ]
    .into_iter()
    .map(|relative| resource_dir.join(relative))
    .find(|candidate| candidate.is_file())
}

fn missing_tool_message(resource_dir: &Path, file: &str) -> String {
    format!(
        "安装包缺少 {file}（已在 {} 下查找）。请重新安装完整安装包，不要手工删除安装目录内的文件。",
        resource_dir.display()
    )
}

/// 定位随包分发的 ffmpeg 与 ffprobe。
pub fn locate_tools<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
) -> Result<(PathBuf, PathBuf), String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位应用资源目录：{error}"))?;
    let ffmpeg = find_within(&resource_dir, "ffmpeg.exe")
        .ok_or_else(|| missing_tool_message(&resource_dir, "ffmpeg.exe"))?;
    let ffprobe = find_within(&resource_dir, "ffprobe.exe")
        .ok_or_else(|| missing_tool_message(&resource_dir, "ffprobe.exe"))?;
    Ok((ffmpeg, ffprobe))
}

/// 只保留 stderr 的尾部：ffmpeg 失败时前面几十行都是无关的流信息，
/// 真正的原因在最后几行。
fn failure_tail(stderr: &str) -> String {
    let trimmed = stderr.trim();
    let lines: Vec<&str> = trimmed.lines().collect();
    let start = lines.len().saturating_sub(4);
    lines[start..].join("\n")
}

/// 抽音轨。参数顺序与服务端一致，便于对照排查。
pub fn extract_audio(ffmpeg: &Path, video: &Path, audio: &Path) -> Result<(), String> {
    let output = Command::new(ffmpeg)
        .arg("-y")
        .arg("-i")
        .arg(video)
        .args([
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
        ])
        .arg(audio)
        .output()
        .map_err(|error| format!("无法启动 ffmpeg：{error}"))?;
    if !output.status.success() {
        return Err(format!(
            "抽取音轨失败：{}",
            failure_tail(&String::from_utf8_lossy(&output.stderr))
        ));
    }
    Ok(())
}

/// 派生音轨的存放位置：与视频同根，单独一个 audio/ 子树，便于整体清理。
/// `pub(crate)`：删除缓存时要连派生的音轨一起清掉（见 viral_cache 的淘汰逻辑）。
pub(crate) fn audio_path(root: &Path, platform: &str, video_id: &str) -> PathBuf {
    root.join("audio")
        .join(safe_segment(platform))
        .join(format!("{}.m4a", safe_segment(video_id)))
}

/// 抽出已缓存视频的音轨并直接回传字节。
///
/// 回传字节而不是路径：调用方是 WebView，它读不了任意本地路径（本地播放要等
/// assetProtocol，见 §6.3），而音轨本身只有几百 KB，直接给字节最省事。
/// 用 `tauri::ipc::Response` 走原始字节通道，避免 `Vec<u8>` 被序列化成
/// 逐元素的 JSON 数组（那样一个 240KB 的音轨会膨胀到 MB 级）。
#[tauri::command]
pub async fn viral_extract_audio(
    app: tauri::AppHandle,
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
) -> Result<tauri::ipc::Response, String> {
    let video = cache.video_path_for(&platform, &video_id);
    if !video.is_file() {
        // 没有本地缓存就没有可抽的音轨——调用方应先 ensure，而不是在这里
        // 悄悄回服务端取（那会让「服务器不参与」的约束失效）。
        return Err("该视频尚未缓存到本地，请先缓存后再提取文案。".to_string());
    }
    let root = cache.root_dir();
    let audio = audio_path(&root, &platform, &video_id);
    if let Some(parent) = audio.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|error| format!("无法创建音轨目录：{error}"))?;
    }
    let (ffmpeg, _ffprobe) = locate_tools(&app)?;
    let video_for_task = video.clone();
    let audio_for_task = audio.clone();
    // ffmpeg 是阻塞进程，放到阻塞线程池，别占住异步运行时的工作线程。
    tauri::async_runtime::spawn_blocking(move || {
        extract_audio(&ffmpeg, &video_for_task, &audio_for_task)
    })
    .await
    .map_err(|error| format!("抽取音轨任务异常：{error}"))??;
    let bytes = tokio::fs::read(&audio)
        .await
        .map_err(|error| format!("读取音轨失败：{error}"))?;
    Ok(tauri::ipc::Response::new(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("viral-audio-test-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("创建临时目录");
        dir
    }

    #[test]
    fn find_within_prefers_the_bundled_resource_layout() {
        let dir = scratch("prefer");
        fs::create_dir_all(dir.join("resources/ffmpeg")).expect("建目录");
        fs::write(dir.join("resources/ffmpeg/ffmpeg.exe"), b"x").expect("写文件");
        assert_eq!(
            find_within(&dir, "ffmpeg.exe"),
            Some(dir.join("resources/ffmpeg/ffmpeg.exe"))
        );
    }

    #[test]
    fn find_within_falls_back_to_flatter_layouts() {
        let dir = scratch("fallback");
        fs::create_dir_all(dir.join("ffmpeg")).expect("建目录");
        fs::write(dir.join("ffmpeg/ffmpeg.exe"), b"x").expect("写文件");
        assert_eq!(
            find_within(&dir, "ffmpeg.exe"),
            Some(dir.join("ffmpeg/ffmpeg.exe"))
        );
    }

    #[test]
    fn find_within_returns_none_when_the_installer_did_not_ship_it() {
        // 缺 ffmpeg 必须能被察觉（fail-closed），绝不能回退到 PATH 上的任意
        // ffmpeg——那会让「随包分发」的保证名存实亡。
        let dir = scratch("absent");
        assert_eq!(find_within(&dir, "ffmpeg.exe"), None);
    }

    #[test]
    fn audio_path_is_scoped_and_path_safe() {
        let root = Path::new("C:/cache");
        let path = audio_path(root, "wechat_channels", "abc/def");
        assert!(path.starts_with(root.join("audio")));
        // 视频号 id 里的 `/` 不能逃出 audio 子树。
        assert!(path.to_string_lossy().contains("abc%2Fdef"));
        assert!(!path.to_string_lossy().contains("abc/def"));
    }

    #[test]
    fn failure_tail_keeps_only_the_last_lines() {
        let stderr = (1..=20)
            .map(|index| format!("line-{index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let tail = failure_tail(&stderr);
        assert!(tail.contains("line-20"));
        assert!(!tail.contains("line-1\n"), "前面几十行流信息不该带出来");
    }
}
