//! 爆款视频本地缓存（P2 · 决策 #8/#13/#14/#16）。
//!
//! 只做三件事：把源站原片多线程分段取回本地、视频号在本地解密、把进度报给界面。
//!
//! 设计约束（`docs/superpowers/specs/2026-09-21-viral-search-local-cache-design.md`）：
//! - §6.2 **索引即文件系统**——文件存在即已缓存，不引入本地数据库；元数据一律
//!   回服务端内容池取。因此本模块只回答「在不在」，不缓存任何业务字段。
//! - §6.2 视频号原片是加密的，**解密在本地完成**，原始文件不经过服务端。
//! - 决策 #14/#16：单文件分段并发 + 多视频队列并行 + 进度可视（事件驱动，不轮询）。
//! - §13-6 默认值：单文件 4 连接、视频队列并行 3（均可调）。
//!
//! 直链与 `decode_key` 由服务端同批下发（`decode_key` 每次请求都会变），本模块
//! 只负责取用，不自行刷新——刷新失败由界面提示用户重试。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncSeekExt, AsyncWriteExt};
use tokio::sync::Semaphore;

use crate::viral_decrypt::{decrypt_head, is_encrypted_mp4, KEYSTREAM_SIZE};

/// 单文件分段并发数（§13-6 默认值，可调）。
const SEGMENT_CONNECTIONS: usize = 4;
/// 同时下载的视频数（§13-6 默认值，可调）。
const VIDEO_CONCURRENCY: usize = 3;
/// 失败自动重试次数（不含首次），与 §6.2 的「≤2 次」一致。
const MAX_RETRIES: usize = 2;
/// 小于此长度不分段——分段的固定开销会超过收益。
const MIN_SEGMENT_BYTES: u64 = 512 * 1024;
/// 进度事件最小间隔，避免刷爆前端（事件驱动但需要节流）。
const PROGRESS_INTERVAL: Duration = Duration::from_millis(300);
/// 单次源站请求超时。
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
/// 缓存根目录名，位于 app_data_dir 之下。
const CACHE_DIR_NAME: &str = "viral-cache";

/// 常见浏览器的 UA。部分 CDN 对无 UA 的请求直接拒绝。
const BROWSER_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
     (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

/// 缓存条目在界面上的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum CacheState {
    Queued,
    Downloading,
    Cached,
    Failed,
}

/// 一次缓存任务的身份。`video_id` 视频号侧是不透明串，可能含 `/`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CacheKey {
    pub platform: String,
    pub video_id: String,
}

impl CacheKey {
    fn storage_id(&self) -> String {
        format!("{}:{}", self.platform, self.video_id)
    }
}

/// 推给前端的进度事件载荷（`viral-cache-progress`）。
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CacheProgress {
    pub platform: String,
    pub video_id: String,
    pub state: CacheState,
    pub downloaded_bytes: u64,
    pub total_bytes: Option<u64>,
    pub speed_bytes_per_second: u64,
    pub error: Option<String>,
}

/// 已缓存条目的磁盘视图（`viral_cache_list`）。
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CachedItem {
    pub platform: String,
    pub video_id: String,
    pub bytes: u64,
}

struct Entry {
    progress: CacheProgress,
    started_at: Instant,
}

/// Tauri 托管的缓存服务。`permits` 限制同时在下载的视频数；`statuses` 是内存
/// 视图，真值仍以文件系统为准。
pub struct ViralCache {
    root: PathBuf,
    client: reqwest::Client,
    permits: Arc<Semaphore>,
    statuses: Arc<Mutex<HashMap<String, Entry>>>,
}

impl ViralCache {
    pub fn new(root: PathBuf) -> Self {
        let client = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .unwrap_or_default();
        Self {
            root,
            client,
            permits: Arc::new(Semaphore::new(VIDEO_CONCURRENCY)),
            statuses: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// 解析缓存根目录：`{app_data_dir}/viral-cache`。
    pub fn from_app<R: tauri::Runtime>(app: &AppHandle<R>) -> Result<Self, String> {
        let base = app
            .path()
            .app_data_dir()
            .map_err(|error| format!("无法定位应用数据目录：{error}"))?;
        Ok(Self::new(base.join(CACHE_DIR_NAME)))
    }

    fn video_path(&self, key: &CacheKey) -> PathBuf {
        self.root
            .join(safe_segment(&key.platform))
            .join(format!("{}.mp4", safe_segment(&key.video_id)))
    }

    fn part_path(&self, key: &CacheKey) -> PathBuf {
        self.video_path(key).with_extension("mp4.part")
    }

    fn status_of(&self, key: &CacheKey) -> Option<CacheProgress> {
        self.statuses
            .lock()
            .ok()
            .and_then(|map| map.get(&key.storage_id()).map(|e| e.progress.clone()))
    }

    fn remember(&self, progress: CacheProgress) {
        let started_at = self
            .statuses
            .lock()
            .ok()
            .and_then(|map| map.get(&progress_key(&progress)).map(|e| e.started_at))
            .unwrap_or_else(Instant::now);
        if let Ok(mut map) = self.statuses.lock() {
            map.insert(
                progress_key(&progress),
                Entry {
                    progress,
                    started_at,
                },
            );
        }
    }
}

fn progress_key(progress: &CacheProgress) -> String {
    format!("{}:{}", progress.platform, progress.video_id)
}

/// 把任意 id 编码成安全的单层文件名。
///
/// 视频号的 `video_id` 是不透明串、可能含 `/`；只放行 `[A-Za-z0-9_-]`，
/// 其余一律百分号转义。这样结果**不可能**是 `.`、`..` 或含路径分隔符，
/// 从根上排除目录穿越。
pub fn safe_segment(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' => encoded.push(byte as char),
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    if encoded.is_empty() {
        // 空 id 无法构成合法文件名，用一个不可能与真实 id 冲突的字面量兜底。
        return "%00empty".to_string();
    }
    encoded
}

/// 把总长度切成分段区间（闭区间，便于直接写 `Range: bytes=start-end`）。
///
/// 段数会随文件变小而收敛，避免为小文件开出大量 1 字节请求。
pub fn segment_ranges(total: u64) -> Vec<(u64, u64)> {
    if total == 0 {
        return Vec::new();
    }
    let mut count = SEGMENT_CONNECTIONS;
    while count > 1 && total / (count as u64) < MIN_SEGMENT_BYTES {
        count -= 1;
    }
    if count <= 1 {
        return vec![(0, total - 1)];
    }
    let chunk = total.div_ceil(count as u64);
    let mut ranges = Vec::with_capacity(count);
    let mut start = 0u64;
    while start < total {
        let end = (start + chunk - 1).min(total - 1);
        ranges.push((start, end));
        start = end + 1;
    }
    ranges
}

/// 从 `Content-Range: bytes 0-0/12345` 里取出总长度。
pub fn content_range_total(value: &str) -> Option<u64> {
    value.rsplit('/').next()?.trim().parse::<u64>().ok()
}

/// 源站 Referer。部分 CDN 会校验来源页。
fn referer_for(platform: &str) -> &'static str {
    match platform {
        "wechat_channels" => "https://channels.weixin.qq.com/",
        _ => "https://www.douyin.com/",
    }
}

/// 单个视频的下载任务。
struct Job<'a> {
    app: &'a AppHandle,
    client: &'a reqwest::Client,
    key: &'a CacheKey,
    url: &'a str,
    decode_key: Option<&'a str>,
    part: PathBuf,
    final_path: PathBuf,
    downloaded: AtomicU64,
    /// 上一次上报时的累计字节，仅用于算瞬时速度。
    last_bytes: AtomicU64,
    total: AtomicU64,
    last_emit: Mutex<Instant>,
}

impl Job<'_> {
    fn emit(&self, state: CacheState, error: Option<String>, force: bool) {
        let total = match self.total.load(Ordering::Relaxed) {
            0 => None,
            value => Some(value),
        };
        let downloaded = self.downloaded.load(Ordering::Relaxed);
        let mut speed = 0u64;
        if let Ok(mut last) = self.last_emit.lock() {
            let elapsed = last.elapsed();
            if !force && elapsed < PROGRESS_INTERVAL {
                return;
            }
            // 速度必须是「这一个间隔内新增的字节 / 间隔」，用累计值直接除会把
            // 平均速度当成瞬时速度，进度条上的速率会一路虚高。
            let previous = self.last_bytes.swap(downloaded, Ordering::Relaxed);
            let delta = downloaded.saturating_sub(previous);
            let seconds = elapsed.as_secs_f64();
            if seconds > 0.0 {
                speed = (delta as f64 / seconds) as u64;
            }
            *last = Instant::now();
        }
        let _ = self.app.emit(
            "viral-cache-progress",
            CacheProgress {
                platform: self.key.platform.clone(),
                video_id: self.key.video_id.clone(),
                state,
                downloaded_bytes: downloaded,
                total_bytes: total,
                speed_bytes_per_second: speed,
                error,
            },
        );
    }

    fn headers(&self, request: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        request
            .header(reqwest::header::USER_AGENT, BROWSER_USER_AGENT)
            .header(reqwest::header::REFERER, referer_for(&self.key.platform))
    }

    /// 取回原片并落到 `.part`，随后本地解密、原子改名为正式文件。
    async fn run(&self) -> Result<PathBuf, String> {
        if let Some(parent) = self.part.parent() {
            tokio::fs::create_dir_all(parent)
                .await
                .map_err(|error| format!("无法创建缓存目录：{error}"))?;
        }
        self.emit(CacheState::Downloading, None, true);

        let probe = self
            .headers(
                self.client
                    .get(self.url)
                    .header(reqwest::header::RANGE, "bytes=0-0"),
            )
            .send()
            .await
            .map_err(|error| format!("请求源站失败：{error}"))?;

        let status = probe.status();
        if !status.is_success() {
            return Err(format!("源站返回 HTTP {}", status.as_u16()));
        }

        let ranged_total = probe
            .headers()
            .get(reqwest::header::CONTENT_RANGE)
            .and_then(|value| value.to_str().ok())
            .and_then(content_range_total);

        if status == reqwest::StatusCode::PARTIAL_CONTENT {
            if let Some(total) = ranged_total {
                if total > 0 {
                    self.total.store(total, Ordering::Relaxed);
                    self.download_segmented(total).await?;
                    return self.finish().await;
                }
            }
        }

        // 源站忽略了 Range（返回 200），探测响应本身就是完整内容，直接复用。
        if let Some(length) = probe.content_length() {
            self.total.store(length, Ordering::Relaxed);
        }
        self.write_stream(probe, 0).await?;
        self.finish().await
    }

    /// 分段并发：先按最终长度建立文件，各段 seek 到自身偏移顺序写入。
    async fn download_segmented(&self, total: u64) -> Result<(), String> {
        let file = tokio::fs::File::create(&self.part)
            .await
            .map_err(|error| format!("无法创建临时文件：{error}"))?;
        file.set_len(total)
            .await
            .map_err(|error| format!("无法预分配空间：{error}"))?;
        drop(file);

        let ranges = segment_ranges(total);
        let mut handles = Vec::with_capacity(ranges.len());
        for (start, end) in ranges {
            let request = self.headers(
                self.client
                    .get(self.url)
                    .header(reqwest::header::RANGE, format!("bytes={start}-{end}")),
            );
            let response = request
                .send()
                .await
                .map_err(|error| format!("分段请求失败：{error}"))?;
            if response.status() != reqwest::StatusCode::PARTIAL_CONTENT {
                return Err(format!(
                    "源站未按分段响应（HTTP {}）",
                    response.status().as_u16()
                ));
            }
            handles.push(self.write_stream(response, start));
        }
        for handle in handles {
            handle.await?;
        }
        Ok(())
    }

    /// 把一个响应体顺序写到 `offset` 起始的位置，并累计进度。
    async fn write_stream(&self, response: reqwest::Response, offset: u64) -> Result<(), String> {
        let mut file = tokio::fs::OpenOptions::new()
            .write(true)
            .open(&self.part)
            .await
            .map_err(|error| format!("无法打开临时文件：{error}"))?;
        file.seek(std::io::SeekFrom::Start(offset))
            .await
            .map_err(|error| format!("无法定位写入偏移：{error}"))?;
        let mut response = response;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| format!("读取源站数据失败：{error}"))?
        {
            file.write_all(&chunk)
                .await
                .map_err(|error| format!("写入缓存失败：{error}"))?;
            self.downloaded
                .fetch_add(chunk.len() as u64, Ordering::Relaxed);
            self.emit(CacheState::Downloading, None, false);
        }
        file.flush()
            .await
            .map_err(|error| format!("刷新缓存失败：{error}"))?;
        Ok(())
    }

    /// 落盘后本地解密（仅视频号且确实加密时），再原子改名。
    async fn finish(&self) -> Result<PathBuf, String> {
        if let Some(decode_key) = self.decode_key {
            self.decrypt_in_place(decode_key).await?;
        }
        tokio::fs::rename(&self.part, &self.final_path)
            .await
            .map_err(|error| format!("无法落盘缓存文件：{error}"))?;
        self.emit(CacheState::Cached, None, true);
        Ok(self.final_path.clone())
    }

    /// 只解前 [`KEYSTREAM_SIZE`] 字节；已是明文则原样保留（幂等）。
    async fn decrypt_in_place(&self, decode_key: &str) -> Result<(), String> {
        use tokio::io::AsyncReadExt;
        let mut file = tokio::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&self.part)
            .await
            .map_err(|error| format!("无法打开临时文件：{error}"))?;
        let mut head = vec![0u8; KEYSTREAM_SIZE];
        let read = file
            .read(&mut head)
            .await
            .map_err(|error| format!("无法读取文件头：{error}"))?;
        head.truncate(read);
        if !is_encrypted_mp4(&head) {
            return Ok(());
        }
        let decrypted = decrypt_head(&head, decode_key).map_err(|error| error.to_string())?;
        file.seek(std::io::SeekFrom::Start(0))
            .await
            .map_err(|error| format!("无法回到文件头：{error}"))?;
        file.write_all(&decrypted)
            .await
            .map_err(|error| format!("写回解密结果失败：{error}"))?;
        file.flush()
            .await
            .map_err(|error| format!("刷新解密结果失败：{error}"))?;
        Ok(())
    }
}

/// 入队一个视频的缓存任务；已缓存则直接返回。
async fn ensure(
    cache: &ViralCache,
    app: &AppHandle,
    key: CacheKey,
    url: String,
    decode_key: Option<String>,
) -> Result<PathBuf, String> {
    let final_path = cache.video_path(&key);
    if final_path.is_file() {
        return Ok(final_path);
    }
    let permit = cache
        .permits
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| "缓存队列已关闭".to_string())?;

    cache.remember(CacheProgress {
        platform: key.platform.clone(),
        video_id: key.video_id.clone(),
        state: CacheState::Queued,
        downloaded_bytes: 0,
        total_bytes: None,
        speed_bytes_per_second: 0,
        error: None,
    });

    let part = cache.part_path(&key);
    let job = Job {
        app,
        client: &cache.client,
        key: &key,
        url: &url,
        decode_key: decode_key.as_deref(),
        part: part.clone(),
        final_path: final_path.clone(),
        downloaded: AtomicU64::new(0),
        last_bytes: AtomicU64::new(0),
        total: AtomicU64::new(0),
        last_emit: Mutex::new(Instant::now() - PROGRESS_INTERVAL),
    };

    let mut last_error = String::from("下载失败");
    for attempt in 0..=MAX_RETRIES {
        job.downloaded.store(0, Ordering::Relaxed);
        job.last_bytes.store(0, Ordering::Relaxed);
        let _ = tokio::fs::remove_file(&part).await;
        match job.run().await {
            Ok(path) => {
                cache.remember(CacheProgress {
                    platform: key.platform.clone(),
                    video_id: key.video_id.clone(),
                    state: CacheState::Cached,
                    downloaded_bytes: job.downloaded.load(Ordering::Relaxed),
                    total_bytes: match job.total.load(Ordering::Relaxed) {
                        0 => None,
                        value => Some(value),
                    },
                    speed_bytes_per_second: 0,
                    error: None,
                });
                drop(permit);
                return Ok(path);
            }
            Err(error) => {
                last_error = error;
                if attempt < MAX_RETRIES {
                    tokio::time::sleep(Duration::from_millis(500 * (attempt as u64 + 1))).await;
                }
            }
        }
    }

    let _ = tokio::fs::remove_file(&part).await;
    cache.remember(CacheProgress {
        platform: key.platform.clone(),
        video_id: key.video_id.clone(),
        state: CacheState::Failed,
        downloaded_bytes: job.downloaded.load(Ordering::Relaxed),
        total_bytes: None,
        speed_bytes_per_second: 0,
        error: Some(last_error.clone()),
    });
    job.emit(CacheState::Failed, Some(last_error.clone()), true);
    drop(permit);
    Err(last_error)
}

// ---------------------------------------------------------------------------
// 命令
// ---------------------------------------------------------------------------

/// 入队下载（已在本地则直接返回）。进度经 `viral-cache-progress` 事件推送。
#[tauri::command]
pub async fn viral_cache_ensure(
    app: AppHandle,
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
    url: String,
    decode_key: Option<String>,
) -> Result<(), String> {
    if url.trim().is_empty() {
        return Err("缺少可下载的媒体地址".to_string());
    }
    // 入队即返回：真正下载交给后台队列按并发上限推进，界面靠事件更新进度。
    // 这里把共享字段克隆进任务，而不是把 State 的借用带过去（它会随命令返回失效）。
    let owned = ViralCache {
        root: cache.root.clone(),
        client: cache.client.clone(),
        permits: cache.permits.clone(),
        statuses: cache.statuses.clone(),
    };
    let key = CacheKey { platform, video_id };
    let app_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let _ = ensure(&owned, &app_handle, key, url, decode_key).await;
    });
    Ok(())
}

/// 批量查询缓存状态（页面角标用）。文件系统是唯一真源。
#[tauri::command]
pub fn viral_cache_status(
    cache: tauri::State<'_, ViralCache>,
    items: Vec<CacheKey>,
) -> Vec<CacheProgress> {
    items
        .into_iter()
        .map(|key| {
            let final_path = cache.video_path(&key);
            if final_path.is_file() {
                let bytes = std::fs::metadata(&final_path)
                    .map(|meta| meta.len())
                    .unwrap_or(0);
                return CacheProgress {
                    platform: key.platform.clone(),
                    video_id: key.video_id.clone(),
                    state: CacheState::Cached,
                    downloaded_bytes: bytes,
                    total_bytes: Some(bytes),
                    speed_bytes_per_second: 0,
                    error: None,
                };
            }
            cache.status_of(&key).unwrap_or(CacheProgress {
                platform: key.platform,
                video_id: key.video_id,
                state: CacheState::Queued,
                downloaded_bytes: 0,
                total_bytes: None,
                speed_bytes_per_second: 0,
                error: None,
            })
        })
        .collect()
}

/// 已缓存列表 + 各条占用。索引即文件系统，因此直接遍历目录。
#[tauri::command]
pub async fn viral_cache_list(
    cache: tauri::State<'_, ViralCache>,
) -> Result<Vec<CachedItem>, String> {
    let root = cache.root.clone();
    let mut items = Vec::new();
    let mut platforms = match tokio::fs::read_dir(&root).await {
        Ok(entries) => entries,
        // 还没缓存过任何东西时目录不存在，属正常空集合。
        Err(_) => return Ok(items),
    };
    while let Some(platform_entry) = platforms
        .next_entry()
        .await
        .map_err(|error| format!("读取缓存目录失败：{error}"))?
    {
        if !platform_entry
            .file_type()
            .await
            .map(|kind| kind.is_dir())
            .unwrap_or(false)
        {
            continue;
        }
        let platform = platform_entry.file_name().to_string_lossy().into_owned();
        let mut files = match tokio::fs::read_dir(platform_entry.path()).await {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        while let Some(file_entry) = files
            .next_entry()
            .await
            .map_err(|error| format!("读取缓存目录失败：{error}"))?
        {
            let name = file_entry.file_name().to_string_lossy().into_owned();
            // 只认正式文件；`.part` 是在途任务，不算已缓存。
            let Some(video_id) = name.strip_suffix(".mp4") else {
                continue;
            };
            let bytes = file_entry
                .metadata()
                .await
                .map(|meta| meta.len())
                .unwrap_or(0);
            items.push(CachedItem {
                platform: platform.clone(),
                video_id: decode_segment(video_id),
                bytes,
            });
        }
    }
    Ok(items)
}

/// 删除单条缓存。
#[tauri::command]
pub async fn viral_cache_delete(
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
) -> Result<(), String> {
    let key = CacheKey { platform, video_id };
    let path = cache.video_path(&key);
    if path.is_file() {
        tokio::fs::remove_file(&path)
            .await
            .map_err(|error| format!("删除缓存失败：{error}"))?;
    }
    if let Ok(mut map) = cache.statuses.lock() {
        map.remove(&key.storage_id());
    }
    Ok(())
}

/// 批量清理：`all` 清全部，`platform` 只清某个平台。
#[tauri::command]
pub async fn viral_cache_clear(
    cache: tauri::State<'_, ViralCache>,
    scope: String,
    platform: Option<String>,
) -> Result<(), String> {
    let root = cache.root.clone();
    let target = match (scope.as_str(), platform) {
        ("platform", Some(platform)) => root.join(safe_segment(&platform)),
        _ => root,
    };
    if target.is_dir() {
        tokio::fs::remove_dir_all(&target)
            .await
            .map_err(|error| format!("清理缓存失败：{error}"))?;
    }
    if let Ok(mut map) = cache.statuses.lock() {
        map.clear();
    }
    Ok(())
}

/// 打开缓存目录（供用户自行查看/清理）。
#[tauri::command]
pub fn viral_cache_open_folder(cache: tauri::State<'_, ViralCache>) -> Result<(), String> {
    let root = cache.root.clone();
    std::fs::create_dir_all(&root).map_err(|error| format!("无法创建缓存目录：{error}"))?;
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&root)
            .spawn()
            .map_err(|error| format!("无法打开缓存目录：{error}"))?;
        return Ok(());
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = root;
        Err("仅支持在 Windows 上打开缓存目录".to_string())
    }
}

/// `safe_segment` 的逆运算，用于把磁盘文件名还原成原始 `video_id`。
fn decode_segment(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            if let Ok(byte) = u8::from_str_radix(&value[index + 1..index + 3], 16) {
                decoded.push(byte);
                index += 3;
                continue;
            }
        }
        decoded.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_segment_escapes_path_separators() {
        assert_eq!(safe_segment("abc/def"), "abc%2Fdef");
        assert_eq!(safe_segment("a b"), "a%20b");
        assert_eq!(safe_segment("plain-id_1"), "plain-id_1");
    }

    #[test]
    fn safe_segment_can_never_be_a_dot_segment() {
        // 目录穿越的两个元凶：`.` 与 `..`。两者都必须被转义掉。
        assert_eq!(safe_segment("."), "%2E");
        assert_eq!(safe_segment(".."), "%2E%2E");
        assert_eq!(safe_segment(""), "%00empty");
        assert!(!safe_segment("../../etc/passwd").contains('/'));
    }

    #[test]
    fn safe_segment_round_trips() {
        for raw in ["abc/def", "视频号 opaque id", "%2F", "a.b.c"] {
            assert_eq!(decode_segment(&safe_segment(raw)), raw);
        }
    }

    #[test]
    fn segment_ranges_covers_whole_file_without_overlap() {
        let total = 10 * MIN_SEGMENT_BYTES;
        let ranges = segment_ranges(total);
        assert_eq!(ranges.len(), SEGMENT_CONNECTIONS);
        assert_eq!(ranges[0].0, 0);
        assert_eq!(ranges[ranges.len() - 1].1, total - 1);
        for pair in ranges.windows(2) {
            assert_eq!(pair[0].1 + 1, pair[1].0, "分段之间必须连续且不重叠");
        }
    }

    #[test]
    fn segment_ranges_collapses_for_small_files() {
        // 小文件不为了凑并发而开出大量碎请求。
        assert_eq!(
            segment_ranges(MIN_SEGMENT_BYTES),
            vec![(0, MIN_SEGMENT_BYTES - 1)]
        );
        assert_eq!(segment_ranges(1), vec![(0, 0)]);
        assert!(segment_ranges(0).is_empty());
    }

    #[test]
    fn content_range_total_reads_the_denominator() {
        assert_eq!(content_range_total("bytes 0-0/131072"), Some(131_072));
        assert_eq!(content_range_total("bytes 0-0/*"), None);
        assert_eq!(content_range_total("nonsense"), None);
    }
}
