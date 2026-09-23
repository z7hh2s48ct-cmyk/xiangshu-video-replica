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
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::task::Poll;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncSeekExt, AsyncWriteExt};
use tokio::sync::{Notify, Semaphore};

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
    Paused,
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

#[derive(Default)]
struct TaskControl {
    paused: AtomicBool,
    started: AtomicBool,
    resumed: Notify,
}

/// Tauri 托管的缓存服务。`permits` 限制同时在下载的视频数；`statuses` 是内存
/// 视图，真值仍以文件系统为准。
pub struct ViralCache {
    root: PathBuf,
    client: reqwest::Client,
    permits: Arc<Semaphore>,
    statuses: Arc<Mutex<HashMap<String, Entry>>>,
    controls: Arc<Mutex<HashMap<String, Arc<TaskControl>>>>,
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
            controls: Arc::new(Mutex::new(HashMap::new())),
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

    /// 供同任务的其他模块（如 viral_audio）定位已缓存文件。
    pub fn video_path_for(&self, platform: &str, video_id: &str) -> PathBuf {
        self.video_path(&CacheKey {
            platform: platform.to_string(),
            video_id: video_id.to_string(),
        })
    }

    /// 缓存根目录：派生产物（音轨）与整体清理都挂在它下面。
    pub fn root_dir(&self) -> PathBuf {
        self.root.clone()
    }

    fn part_path(&self, key: &CacheKey) -> PathBuf {
        self.video_path(key).with_extension("mp4.part")
    }

    fn status_of(&self, key: &CacheKey) -> Option<CacheProgress> {
        let progress = self
            .statuses
            .lock()
            .ok()
            .and_then(|map| map.get(&key.storage_id()).map(|e| e.progress.clone()))?;
        if progress.state == CacheState::Cached && !self.video_path(key).is_file() {
            if let Ok(mut statuses) = self.statuses.lock() {
                if statuses
                    .get(&key.storage_id())
                    .is_some_and(|entry| entry.progress.state == CacheState::Cached)
                {
                    statuses.remove(&key.storage_id());
                }
            }
            return None;
        }
        Some(progress)
    }

    #[cfg(test)]
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

    /// 原子认领单个缓存键。状态与控制器在锁内一起登记，因此并发 ensure 只有一个
    /// 能成为真正的 writer；其余调用看到 active 状态后直接复用现有任务。
    fn claim_task(&self, key: &CacheKey) -> Result<Option<Arc<TaskControl>>, String> {
        let mut statuses = self
            .statuses
            .lock()
            .map_err(|_| "缓存任务状态锁已损坏".to_string())?;
        if statuses
            .get(&key.storage_id())
            .is_some_and(|entry| is_active_state(entry.progress.state))
        {
            return Ok(None);
        }
        let control = Arc::new(TaskControl::default());
        let mut controls = self
            .controls
            .lock()
            .map_err(|_| "缓存任务控制锁已损坏".to_string())?;
        controls.insert(key.storage_id(), control.clone());
        statuses.insert(
            key.storage_id(),
            Entry {
                progress: CacheProgress {
                    platform: key.platform.clone(),
                    video_id: key.video_id.clone(),
                    state: CacheState::Queued,
                    downloaded_bytes: 0,
                    total_bytes: None,
                    speed_bytes_per_second: 0,
                    error: None,
                },
                started_at: Instant::now(),
            },
        );
        Ok(Some(control))
    }

    fn remove_control_if_same(&self, key: &CacheKey, expected: &Arc<TaskControl>) {
        if let Ok(mut controls) = self.controls.lock() {
            let matches = controls
                .get(&key.storage_id())
                .is_some_and(|current| Arc::ptr_eq(current, expected));
            if matches {
                controls.remove(&key.storage_id());
            }
        }
    }
}

#[cfg(test)]
fn progress_key(progress: &CacheProgress) -> String {
    format!("{}:{}", progress.platform, progress.video_id)
}

fn is_active_state(state: CacheState) -> bool {
    matches!(
        state,
        CacheState::Queued | CacheState::Downloading | CacheState::Paused
    )
}

fn replace_active_progress_if_current(
    statuses: &Mutex<HashMap<String, Entry>>,
    controls: &Mutex<HashMap<String, Arc<TaskControl>>>,
    key: &CacheKey,
    expected: &Arc<TaskControl>,
    progress: CacheProgress,
) -> bool {
    let Ok(mut statuses) = statuses.lock() else {
        return false;
    };
    let storage_id = key.storage_id();
    let Some(existing) = statuses.get(&storage_id) else {
        return false;
    };
    if !is_active_state(existing.progress.state) {
        return false;
    }
    let started_at = existing.started_at;
    let is_current = controls.lock().is_ok_and(|controls| {
        controls
            .get(&storage_id)
            .is_some_and(|current| Arc::ptr_eq(current, expected))
    });
    if !is_current {
        return false;
    }
    statuses.insert(
        storage_id,
        Entry {
            progress,
            started_at,
        },
    );
    true
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

fn validate_content_range(
    value: &str,
    expected_start: u64,
    expected_end: u64,
    expected_total: u64,
) -> Result<(), String> {
    let value = value
        .strip_prefix("bytes ")
        .ok_or_else(|| format!("分段响应缺少有效 Content-Range：{value}"))?;
    let (range, total) = value
        .split_once('/')
        .ok_or_else(|| format!("分段响应缺少有效 Content-Range：{value}"))?;
    let (start, end) = range
        .split_once('-')
        .ok_or_else(|| format!("分段响应缺少有效 Content-Range：{value}"))?;
    let parsed = (
        start.trim().parse::<u64>(),
        end.trim().parse::<u64>(),
        total.trim().parse::<u64>(),
    );
    if parsed != (Ok(expected_start), Ok(expected_end), Ok(expected_total)) {
        return Err(format!(
            "源站返回了错误分段：期望 bytes {expected_start}-{expected_end}/{expected_total}，实际 {value}"
        ));
    }
    Ok(())
}

async fn try_join_all<F>(futures: Vec<F>) -> Result<(), String>
where
    F: Future<Output = Result<(), String>>,
{
    let mut futures: Vec<Pin<Box<F>>> = futures.into_iter().map(Box::pin).collect();
    std::future::poll_fn(|context| {
        let mut index = 0;
        while index < futures.len() {
            match futures[index].as_mut().poll(context) {
                Poll::Ready(Ok(())) => {
                    drop(futures.swap_remove(index));
                }
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Pending => index += 1,
            }
        }
        if futures.is_empty() {
            Poll::Ready(Ok(()))
        } else {
            Poll::Pending
        }
    })
    .await
}

/// §13-1 默认缓存上限 20 GB（数值可调）。
pub const DEFAULT_CACHE_LIMIT_BYTES: u64 = 20 * 1024 * 1024 * 1024;

/// 淘汰决策需要的条目元数据。
pub struct CacheEntryMeta {
    pub platform: String,
    pub video_id: String,
    pub bytes: u64,
    /// 最近使用时间，不是下载时间：播放会刷新它（见 `viral_cache_local_path`），
    /// 所以这里是真的 LRU，而不是「最久未下载」。
    pub last_used: std::time::SystemTime,
}

fn storage_key(platform: &str, video_id: &str) -> String {
    format!("{platform}:{video_id}")
}

/// 计算需要淘汰的条目（最久未用优先），返回它们的存储键。
///
/// `protected` 里的条目**即使超限也不淘汰**：收藏是用户显式说过「我要留着」，
/// 被后台清理悄悄删掉是最难解释、也最伤信任的一类数据丢失。因此当受保护条目
/// 本身就超过上限时，本函数会返回空列表、宁可暂时超限——这是有意的取舍。
pub fn eviction_plan(
    entries: &[CacheEntryMeta],
    limit_bytes: u64,
    protected: &std::collections::HashSet<String>,
) -> Vec<String> {
    let total: u64 = entries.iter().map(|entry| entry.bytes).sum();
    if total <= limit_bytes {
        return Vec::new();
    }
    let mut candidates: Vec<&CacheEntryMeta> = entries
        .iter()
        .filter(|entry| !protected.contains(&storage_key(&entry.platform, &entry.video_id)))
        .collect();
    candidates.sort_by_key(|entry| entry.last_used);
    let mut remaining = total;
    let mut victims = Vec::new();
    for candidate in candidates {
        if remaining <= limit_bytes {
            break;
        }
        remaining = remaining.saturating_sub(candidate.bytes);
        victims.push(storage_key(&candidate.platform, &candidate.video_id));
    }
    victims
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
    statuses: Arc<Mutex<HashMap<String, Entry>>>,
    controls: Arc<Mutex<HashMap<String, Arc<TaskControl>>>>,
    control: Arc<TaskControl>,
}

impl Job<'_> {
    async fn wait_if_paused(&self) {
        loop {
            let resumed = self.control.resumed.notified();
            if !self.control.paused.load(Ordering::Acquire) {
                break;
            }
            self.emit(CacheState::Paused, None, true);
            resumed.await;
        }
    }

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
        let progress = CacheProgress {
            platform: self.key.platform.clone(),
            video_id: self.key.video_id.clone(),
            state,
            downloaded_bytes: downloaded,
            total_bytes: total,
            speed_bytes_per_second: speed,
            error,
        };
        if replace_active_progress_if_current(
            &self.statuses,
            &self.controls,
            self.key,
            &self.control,
            progress.clone(),
        ) {
            let _ = self.app.emit("viral-cache-progress", progress);
        }
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
        self.wait_if_paused().await;
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
            let total = ranged_total
                .filter(|total| *total > 0)
                .ok_or_else(|| "分段探测响应缺少有效文件总长度".to_string())?;
            self.total.store(total, Ordering::Relaxed);
            self.download_segmented(total).await?;
            return self.finish().await;
        }

        // 源站忽略了 Range（返回 200），探测响应本身就是完整内容，直接复用。
        let expected_bytes = probe.content_length();
        if let Some(length) = expected_bytes {
            self.total.store(length, Ordering::Relaxed);
        }
        self.write_stream(probe, 0, expected_bytes).await?;
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

        let downloads = segment_ranges(total)
            .into_iter()
            .map(|range| self.download_segment(range, total))
            .collect();
        try_join_all(downloads).await?;
        let downloaded = self.downloaded.load(Ordering::Relaxed);
        if downloaded != total {
            return Err(format!(
                "下载文件长度不完整：期望 {total} 字节，实际 {downloaded} 字节"
            ));
        }
        Ok(())
    }

    async fn download_segment(&self, range: (u64, u64), total: u64) -> Result<(), String> {
        self.wait_if_paused().await;
        let (start, end) = range;
        let response = self
            .headers(
                self.client
                    .get(self.url)
                    .header(reqwest::header::RANGE, format!("bytes={start}-{end}")),
            )
            .send()
            .await
            .map_err(|error| format!("分段请求失败：{error}"))?;
        if response.status() != reqwest::StatusCode::PARTIAL_CONTENT {
            return Err(format!(
                "源站未按分段响应（HTTP {}）",
                response.status().as_u16()
            ));
        }
        let content_range = response
            .headers()
            .get(reqwest::header::CONTENT_RANGE)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| "分段响应缺少 Content-Range".to_string())?;
        validate_content_range(content_range, start, end, total)?;
        self.write_stream(response, start, Some(end - start + 1))
            .await?;
        Ok(())
    }

    /// 把一个响应体顺序写到 `offset` 起始的位置，并累计进度。
    async fn write_stream(
        &self,
        response: reqwest::Response,
        offset: u64,
        expected_bytes: Option<u64>,
    ) -> Result<u64, String> {
        let mut file = tokio::fs::OpenOptions::new()
            .write(true)
            .open(&self.part)
            .await
            .map_err(|error| format!("无法打开临时文件：{error}"))?;
        file.seek(std::io::SeekFrom::Start(offset))
            .await
            .map_err(|error| format!("无法定位写入偏移：{error}"))?;
        let mut response = response;
        let mut written = 0u64;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| format!("读取源站数据失败：{error}"))?
        {
            self.wait_if_paused().await;
            self.emit(CacheState::Downloading, None, false);
            written = written.saturating_add(chunk.len() as u64);
            if expected_bytes.is_some_and(|expected| written > expected) {
                return Err(format!(
                    "源站分段长度超出范围：期望最多 {} 字节，实际已收到 {written} 字节",
                    expected_bytes.unwrap_or_default()
                ));
            }
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
        if expected_bytes.is_some_and(|expected| written != expected) {
            return Err(format!(
                "源站分段长度不完整：期望 {} 字节，实际 {written} 字节",
                expected_bytes.unwrap_or_default()
            ));
        }
        Ok(written)
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
    control: Arc<TaskControl>,
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
    control.started.store(true, Ordering::Release);

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
        statuses: cache.statuses.clone(),
        controls: cache.controls.clone(),
        control,
    };

    let mut last_error = String::from("下载失败");
    for attempt in 0..=MAX_RETRIES {
        job.downloaded.store(0, Ordering::Relaxed);
        job.last_bytes.store(0, Ordering::Relaxed);
        let _ = tokio::fs::remove_file(&part).await;
        match job.run().await {
            Ok(path) => {
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
    let key = CacheKey { platform, video_id };
    if cache.video_path(&key).is_file() {
        return Ok(());
    }
    let Some(control) = cache.claim_task(&key)? else {
        return Ok(());
    };
    // 入队即返回：真正下载交给后台队列按并发上限推进，界面靠事件更新进度。
    // 这里把共享字段克隆进任务，而不是把 State 的借用带过去（它会随命令返回失效）。
    let owned = ViralCache {
        root: cache.root.clone(),
        client: cache.client.clone(),
        permits: cache.permits.clone(),
        statuses: cache.statuses.clone(),
        controls: cache.controls.clone(),
    };
    let app_handle = app.clone();
    let cleanup_key = key.clone();
    let cleanup_control = control.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(error) = ensure(&owned, &app_handle, key, url, decode_key, control).await {
            if let Some(mut progress) = owned.status_of(&cleanup_key) {
                progress.state = CacheState::Failed;
                progress.speed_bytes_per_second = 0;
                progress.error = Some(error);
                if replace_active_progress_if_current(
                    &owned.statuses,
                    &owned.controls,
                    &cleanup_key,
                    &cleanup_control,
                    progress.clone(),
                ) {
                    let _ = app_handle.emit("viral-cache-progress", progress);
                }
            }
        }
        owned.remove_control_if_same(&cleanup_key, &cleanup_control);
    });
    Ok(())
}

/// 批量查询缓存状态（页面角标用）。文件系统是唯一真源。
#[tauri::command]
pub fn viral_cache_status(
    cache: tauri::State<'_, ViralCache>,
    items: Vec<CacheKey>,
) -> Vec<CacheProgress> {
    cache_statuses(&cache, items)
}

fn cache_statuses(cache: &ViralCache, items: Vec<CacheKey>) -> Vec<CacheProgress> {
    items
        .into_iter()
        .filter_map(|key| {
            let final_path = cache.video_path(&key);
            if final_path.is_file() {
                let bytes = std::fs::metadata(&final_path)
                    .map(|meta| meta.len())
                    .unwrap_or(0);
                return Some(CacheProgress {
                    platform: key.platform.clone(),
                    video_id: key.video_id.clone(),
                    state: CacheState::Cached,
                    downloaded_bytes: bytes,
                    total_bytes: Some(bytes),
                    speed_bytes_per_second: 0,
                    error: None,
                });
            }
            cache.status_of(&key)
        })
        .collect()
}

fn has_active_task(cache: &ViralCache, key: &CacheKey) -> bool {
    cache
        .status_of(key)
        .is_some_and(|progress| is_active_state(progress.state))
}

fn has_active_tasks_for_platform(cache: &ViralCache, platform: Option<&str>) -> bool {
    cache.statuses.lock().is_ok_and(|statuses| {
        statuses.values().any(|entry| {
            is_active_state(entry.progress.state)
                && platform.is_none_or(|platform| entry.progress.platform == platform)
        })
    })
}

fn cache_task_snapshot(cache: &ViralCache) -> Vec<CacheProgress> {
    let Ok(mut statuses) = cache.statuses.lock() else {
        return Vec::new();
    };
    statuses.retain(|_, entry| {
        if entry.progress.state != CacheState::Cached {
            return true;
        }
        cache
            .video_path(&CacheKey {
                platform: entry.progress.platform.clone(),
                video_id: entry.progress.video_id.clone(),
            })
            .is_file()
    });
    let mut tasks: Vec<(Instant, CacheProgress)> = statuses
        .values()
        .map(|entry| (entry.started_at, entry.progress.clone()))
        .collect();
    tasks.sort_by_key(|(started_at, _)| *started_at);
    tasks.into_iter().map(|(_, progress)| progress).collect()
}

/// 下载管理面板使用的任务快照，包含排队、下载、暂停、失败与本会话已完成任务。
#[tauri::command]
pub fn viral_cache_tasks(cache: tauri::State<'_, ViralCache>) -> Vec<CacheProgress> {
    cache_task_snapshot(&cache)
}

#[tauri::command]
pub fn viral_cache_pause(
    app: AppHandle,
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
) -> Result<(), String> {
    let key = CacheKey { platform, video_id };
    let mut progress = cache
        .status_of(&key)
        .filter(|progress| is_active_state(progress.state))
        .ok_or_else(|| "缓存任务已结束或不存在".to_string())?;
    let control = cache
        .controls
        .lock()
        .ok()
        .and_then(|controls| controls.get(&key.storage_id()).cloned())
        .ok_or_else(|| "缓存任务已结束或不存在".to_string())?;
    control.paused.store(true, Ordering::Release);
    progress.state = CacheState::Paused;
    progress.speed_bytes_per_second = 0;
    if !replace_active_progress_if_current(
        &cache.statuses,
        &cache.controls,
        &key,
        &control,
        progress.clone(),
    ) {
        control.paused.store(false, Ordering::Release);
        return Err("缓存任务已结束或不存在".to_string());
    }
    let _ = app.emit("viral-cache-progress", progress);
    Ok(())
}

#[tauri::command]
pub fn viral_cache_resume(
    app: AppHandle,
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
) -> Result<(), String> {
    let key = CacheKey { platform, video_id };
    let mut progress = cache
        .status_of(&key)
        .filter(|progress| is_active_state(progress.state))
        .ok_or_else(|| "缓存任务已结束或不存在".to_string())?;
    let control = cache
        .controls
        .lock()
        .ok()
        .and_then(|controls| controls.get(&key.storage_id()).cloned())
        .ok_or_else(|| "缓存任务已结束或不存在".to_string())?;
    control.paused.store(false, Ordering::Release);
    control.resumed.notify_waiters();
    progress.state = if control.started.load(Ordering::Acquire) {
        CacheState::Downloading
    } else {
        CacheState::Queued
    };
    progress.speed_bytes_per_second = 0;
    if !replace_active_progress_if_current(
        &cache.statuses,
        &cache.controls,
        &key,
        &control,
        progress.clone(),
    ) {
        return Err("缓存任务已结束或不存在".to_string());
    }
    let _ = app.emit("viral-cache-progress", progress);
    Ok(())
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
    if has_active_task(&cache, &key) {
        return Err("缓存任务仍在运行，请等待完成后再删除".to_string());
    }
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
    let platform = match scope.as_str() {
        "all" => None,
        "platform" => Some(platform.ok_or_else(|| "按平台清理时必须指定平台".to_string())?),
        _ => return Err("未知的缓存清理范围".to_string()),
    };
    if has_active_tasks_for_platform(&cache, platform.as_deref()) {
        return Err("仍有缓存任务在运行，请等待完成后再清理".to_string());
    }
    let target = platform
        .as_deref()
        .map(|platform| root.join(safe_segment(platform)))
        .unwrap_or(root);
    if target.is_dir() {
        tokio::fs::remove_dir_all(&target)
            .await
            .map_err(|error| format!("清理缓存失败：{error}"))?;
    }
    if let Ok(mut map) = cache.statuses.lock() {
        if let Some(platform) = platform.as_deref() {
            map.retain(|_, entry| entry.progress.platform != platform);
        } else {
            map.clear();
        }
    }
    Ok(())
}

/// 已缓存视频的本地绝对路径（未缓存则 None），供前端走 asset 协议播放。
///
/// 路径由 Rust 侧按 `safe_segment` 拼出，前端只能问「某个 platform+videoId 缓存了没」，
/// 无法借此读取缓存目录之外的任意文件。
#[tauri::command]
pub fn viral_cache_local_path(
    cache: tauri::State<'_, ViralCache>,
    platform: String,
    video_id: String,
) -> Option<String> {
    let path = cache.video_path_for(&platform, &video_id);
    if !path.is_file() {
        return None;
    }
    // 播放即「最近使用」：刷新 mtime，让 §13-1 的 LRU 淘汰依据是真实使用时间，
    // 而不是下载时间——否则常看的老片会被当成最旧的先删掉。刷新失败不影响播放。
    if let Ok(file) = std::fs::OpenOptions::new().write(true).open(&path) {
        let _ = file.set_modified(std::time::SystemTime::now());
    }
    path.to_str().map(str::to_string)
}

/// 遍历缓存目录，取出带 mtime 的条目（淘汰决策用）。
async fn collect_cache_entries(root: PathBuf) -> Result<Vec<CacheEntryMeta>, String> {
    let mut entries = Vec::new();
    let mut platforms = match tokio::fs::read_dir(&root).await {
        Ok(iterator) => iterator,
        // 还没缓存过东西时目录不存在，属正常空集合。
        Err(_) => return Ok(entries),
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
            Ok(iterator) => iterator,
            Err(_) => continue,
        };
        while let Some(file_entry) = files
            .next_entry()
            .await
            .map_err(|error| format!("读取缓存目录失败：{error}"))?
        {
            let name = file_entry.file_name().to_string_lossy().into_owned();
            // 只认正式文件；`.part` 是在途任务，不算缓存。
            let Some(video_id) = name.strip_suffix(".mp4") else {
                continue;
            };
            let Ok(metadata) = file_entry.metadata().await else {
                continue;
            };
            entries.push(CacheEntryMeta {
                platform: platform.clone(),
                video_id: decode_segment(video_id),
                bytes: metadata.len(),
                last_used: metadata
                    .modified()
                    .unwrap_or(std::time::SystemTime::UNIX_EPOCH),
            });
        }
    }
    Ok(entries)
}

/// 按 LRU 把缓存压回上限内（§13-1），返回实际释放的字节数。
///
/// 受保护条目（收藏）永不淘汰，需要调用方传入：收藏是服务端状态，Rust 侧看不到，
/// 只能由界面把当前收藏列表带进来。
#[tauri::command]
pub async fn viral_cache_enforce_limit(
    cache: tauri::State<'_, ViralCache>,
    protected: Vec<CacheKey>,
    limit_bytes: Option<u64>,
) -> Result<u64, String> {
    let limit = limit_bytes.unwrap_or(DEFAULT_CACHE_LIMIT_BYTES);
    let protected_keys: std::collections::HashSet<String> =
        protected.iter().map(CacheKey::storage_id).collect();
    let root = cache.root_dir();
    let entries = collect_cache_entries(root.clone()).await?;
    let victims = eviction_plan(&entries, limit, &protected_keys);
    let mut freed = 0u64;
    for victim in &victims {
        // 键是 "platform:video_id"，两段都不含 ':'，split_once 安全。
        let Some((platform, video_id)) = victim.split_once(':') else {
            continue;
        };
        let path = cache.video_path_for(platform, video_id);
        if let Ok(metadata) = tokio::fs::metadata(&path).await {
            freed += metadata.len();
        }
        if tokio::fs::remove_file(&path).await.is_err() {
            // 删不掉就跳过（例如正在被播放器占用），下一轮再处理。
            continue;
        }
        // 派生的音轨一并清掉，否则删了视频还留着几百 KB 死文件。
        let _ =
            tokio::fs::remove_file(crate::viral_audio::audio_path(&root, platform, video_id)).await;
    }
    // 内存状态同步清掉，避免角标继续显示已被删除的条目。
    if let Ok(mut statuses) = cache.statuses.lock() {
        for victim in &victims {
            statuses.remove(victim);
        }
    }
    Ok(freed)
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
    use std::task::Context;

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

    #[test]
    fn content_range_must_match_the_requested_segment() {
        assert!(validate_content_range("bytes 10-19/100", 10, 19, 100).is_ok());
        assert!(validate_content_range("bytes 0-19/100", 10, 19, 100).is_err());
        assert!(validate_content_range("bytes 10-20/100", 10, 19, 100).is_err());
        assert!(validate_content_range("bytes 10-19/99", 10, 19, 100).is_err());
        assert!(validate_content_range("nonsense", 10, 19, 100).is_err());
    }

    struct ConcurrentProbe {
        arrivals: Arc<AtomicU64>,
        arrived: bool,
    }

    impl Future for ConcurrentProbe {
        type Output = Result<(), String>;

        fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
            if !self.arrived {
                self.arrived = true;
                self.arrivals.fetch_add(1, Ordering::SeqCst);
                context.waker().wake_by_ref();
            }
            if self.arrivals.load(Ordering::SeqCst) >= 2 {
                Poll::Ready(Ok(()))
            } else {
                Poll::Pending
            }
        }
    }

    #[test]
    fn segment_futures_are_polled_concurrently() {
        let arrivals = Arc::new(AtomicU64::new(0));
        let futures = vec![
            ConcurrentProbe {
                arrivals: arrivals.clone(),
                arrived: false,
            },
            ConcurrentProbe {
                arrivals: arrivals.clone(),
                arrived: false,
            },
        ];
        tauri::async_runtime::block_on(try_join_all(futures)).unwrap();
        assert_eq!(arrivals.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn unknown_items_are_not_reported_as_queued() {
        let root = std::env::temp_dir().join(format!(
            "viral-cache-status-{}-{}",
            std::process::id(),
            std::thread::current().name().unwrap_or("test")
        ));
        let cache = ViralCache::new(root);
        let statuses = cache_statuses(
            &cache,
            vec![CacheKey {
                platform: "douyin".to_string(),
                video_id: "never-enqueued".to_string(),
            }],
        );
        assert!(statuses.is_empty());
    }

    #[test]
    fn active_tasks_block_overlapping_delete_or_clear() {
        let cache = ViralCache::new(std::env::temp_dir().join("viral-cache-active-test"));
        let key = CacheKey {
            platform: "douyin".to_string(),
            video_id: "active".to_string(),
        };
        cache.claim_task(&key).unwrap().unwrap();

        assert!(has_active_task(&cache, &key));
        assert!(has_active_tasks_for_platform(&cache, None));
        assert!(has_active_tasks_for_platform(&cache, Some("douyin")));
        assert!(!has_active_tasks_for_platform(
            &cache,
            Some("wechat_channels")
        ));
    }

    #[test]
    fn cached_state_disappears_when_the_file_is_gone() {
        let cache = ViralCache::new(std::env::temp_dir().join("viral-cache-stale-test"));
        let key = CacheKey {
            platform: "douyin".to_string(),
            video_id: "deleted".to_string(),
        };
        cache.remember(CacheProgress {
            platform: key.platform.clone(),
            video_id: key.video_id.clone(),
            state: CacheState::Cached,
            downloaded_bytes: 100,
            total_bytes: Some(100),
            speed_bytes_per_second: 0,
            error: None,
        });

        assert!(cache.status_of(&key).is_none());
        assert!(cache_task_snapshot(&cache).is_empty());
    }

    #[test]
    fn concurrent_claims_only_start_one_task_per_key() {
        let cache = Arc::new(ViralCache::new(
            std::env::temp_dir().join("viral-cache-claim-test"),
        ));
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let mut threads = Vec::new();
        for _ in 0..8 {
            let cache = cache.clone();
            let barrier = barrier.clone();
            threads.push(std::thread::spawn(move || {
                let key = CacheKey {
                    platform: "douyin".to_string(),
                    video_id: "same-video".to_string(),
                };
                barrier.wait();
                cache.claim_task(&key).unwrap().is_some()
            }));
        }
        let claimed = threads
            .into_iter()
            .map(|thread| thread.join().unwrap())
            .filter(|claimed| *claimed)
            .count();
        assert_eq!(claimed, 1);
    }

    #[test]
    fn old_task_cannot_overwrite_or_remove_a_new_retry() {
        let cache = ViralCache::new(std::env::temp_dir().join("viral-cache-retry-test"));
        let key = CacheKey {
            platform: "douyin".to_string(),
            video_id: "retry".to_string(),
        };
        let old = cache.claim_task(&key).unwrap().unwrap();
        cache.remember(CacheProgress {
            platform: key.platform.clone(),
            video_id: key.video_id.clone(),
            state: CacheState::Failed,
            downloaded_bytes: 0,
            total_bytes: None,
            speed_bytes_per_second: 0,
            error: Some("old failed".to_string()),
        });
        let new = cache.claim_task(&key).unwrap().unwrap();

        let old_failed = CacheProgress {
            platform: key.platform.clone(),
            video_id: key.video_id.clone(),
            state: CacheState::Failed,
            downloaded_bytes: 12,
            total_bytes: None,
            speed_bytes_per_second: 0,
            error: Some("late old failure".to_string()),
        };
        assert!(!replace_active_progress_if_current(
            &cache.statuses,
            &cache.controls,
            &key,
            &old,
            old_failed,
        ));
        assert_eq!(cache.status_of(&key).unwrap().state, CacheState::Queued);

        let new_downloading = CacheProgress {
            platform: key.platform.clone(),
            video_id: key.video_id.clone(),
            state: CacheState::Downloading,
            downloaded_bytes: 1,
            total_bytes: Some(10),
            speed_bytes_per_second: 1,
            error: None,
        };
        assert!(replace_active_progress_if_current(
            &cache.statuses,
            &cache.controls,
            &key,
            &new,
            new_downloading,
        ));
        cache.remove_control_if_same(&key, &old);
        let current = cache
            .controls
            .lock()
            .unwrap()
            .get(&key.storage_id())
            .cloned();
        assert!(current.is_some_and(|control| Arc::ptr_eq(&control, &new)));
    }

    use std::collections::HashSet;

    /// 造一个条目；`age_secs` 越大表示越久没用过。
    fn meta(platform: &str, video_id: &str, bytes: u64, age_secs: u64) -> CacheEntryMeta {
        CacheEntryMeta {
            platform: platform.to_string(),
            video_id: video_id.to_string(),
            bytes,
            last_used: std::time::SystemTime::UNIX_EPOCH
                + Duration::from_secs(1_000_000 - age_secs),
        }
    }

    #[test]
    fn eviction_plan_is_empty_when_under_the_limit() {
        let entries = vec![meta("douyin", "a", 10, 0), meta("douyin", "b", 10, 5)];
        assert!(eviction_plan(&entries, 100, &HashSet::new()).is_empty());
        // 恰好等于上限也不该淘汰。
        assert!(eviction_plan(&entries, 20, &HashSet::new()).is_empty());
    }

    #[test]
    fn eviction_plan_takes_the_least_recently_used_first() {
        // 三个 10 字节条目共 30、上限 20：只需淘汰一个即可达标，且必须是最久未用的。
        let entries = vec![
            meta("douyin", "new", 10, 0),
            meta("douyin", "oldest", 10, 900),
            meta("douyin", "middle", 10, 300),
        ];
        assert_eq!(
            eviction_plan(&entries, 20, &HashSet::new()),
            vec!["douyin:oldest"]
        );
        // 收紧到 15 就必须连次旧的一起淘汰——只删一个仍会超限。
        assert_eq!(
            eviction_plan(&entries, 15, &HashSet::new()),
            vec!["douyin:oldest", "douyin:middle"]
        );
    }

    #[test]
    fn eviction_plan_keeps_evicting_until_under_the_limit() {
        let entries = vec![
            meta("douyin", "a", 10, 900),
            meta("douyin", "b", 10, 600),
            meta("douyin", "c", 10, 300),
        ];
        // 总共 30、上限 5：每步只减 10，因此必须三个全淘汰才可能达标。
        assert_eq!(
            eviction_plan(&entries, 5, &HashSet::new()),
            vec!["douyin:a", "douyin:b", "douyin:c"]
        );
    }

    #[test]
    fn eviction_plan_never_touches_protected_entries() {
        let entries = vec![
            meta("douyin", "favorite", 10, 999),
            meta("douyin", "ordinary", 10, 1),
        ];
        let protected: HashSet<String> = ["douyin:favorite".to_string()].into_iter().collect();
        // 最旧的是 favorite，但它受保护，只能退而淘汰次旧的。
        assert_eq!(
            eviction_plan(&entries, 10, &protected),
            vec!["douyin:ordinary"]
        );
    }

    #[test]
    fn eviction_plan_prefers_exceeding_the_limit_over_deleting_protected() {
        // 全是受保护条目：宁可暂时超限，也不删用户明确说过要留的东西。
        let entries = vec![meta("douyin", "a", 100, 900), meta("douyin", "b", 100, 1)];
        let protected: HashSet<String> = ["douyin:a".to_string(), "douyin:b".to_string()]
            .into_iter()
            .collect();
        assert!(eviction_plan(&entries, 10, &protected).is_empty());
    }

    #[test]
    fn eviction_plan_handles_empty_cache() {
        assert!(eviction_plan(&[], 10, &HashSet::new()).is_empty());
    }
}
