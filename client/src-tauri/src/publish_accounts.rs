//! Local platform profiles. WebView2 owns Cookie persistence for manual publishing.
//! PUBLISH-DELIVERY-20260917: at the moment a login is confirmed (and on explicit
//! re-verification) the profile's cookies + localStorage are exported ONCE to the
//! main window as a Playwright-shaped storage_state, which the studio uploads to the
//! server (Fernet at rest) so the publish worker can deliver on the account's behalf.
//! Nothing is written to disk here and no other window can request the export.
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize, Debug, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Platform {
    Douyin,
    WechatChannels,
    Xiaohongshu,
}

impl Platform {
    fn origin(&self) -> &'static str {
        match self {
            Self::Douyin => "https://creator.douyin.com",
            Self::WechatChannels => "https://channels.weixin.qq.com",
            Self::Xiaohongshu => "https://creator.xiaohongshu.com",
        }
    }

    fn login_url(&self) -> &'static str {
        match self {
            Self::Douyin => "https://creator.douyin.com/",
            Self::WechatChannels => "https://channels.weixin.qq.com/platform",
            Self::Xiaohongshu => "https://creator.xiaohongshu.com/login",
        }
    }
}

#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct LocalAccount {
    id: String,
    platform: Platform,
    platform_user_id: String,
    username: String,
    verified_at: u64,
    /// Public avatar CDN link; absent on records written before avatars shipped.
    #[serde(default)]
    avatar_url: Option<String>,
}

#[derive(Serialize, Deserialize, Clone)]
struct Identity {
    platform_user_id: String,
    username: String,
    #[serde(default)]
    avatar_url: Option<String>,
}

/// Keep only a bounded https link, matching the official page's own guard.
fn valid_avatar_url(value: &str) -> bool {
    let trimmed = value.trim();
    !trimmed.is_empty()
        && trimmed.len() <= 1024
        && trimmed.starts_with("https://")
        && !trimmed.contains(char::is_whitespace)
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum PagePhase {
    Loading,
    QrReady,
    Confirming,
    ActionRequired,
    Expired,
}

#[derive(Deserialize)]
struct PageLogin {
    phase: PagePhase,
    image: Option<String>,
    // Milliseconds since the epoch as delivered by the page (`Date.now()`). Kept as
    // f64: WebView2 can serialise the JS number in scientific notation, which an
    // integer target rejects outright.
    observed_at: f64,
}

#[derive(Deserialize)]
struct PageSnapshot {
    identity: Option<Identity>,
    login: Option<PageLogin>,
}

#[derive(Serialize)]
pub struct LoginStatus {
    phase: &'static str,
    image: Option<String>,
    account: Option<LocalAccount>,
    /// Present only on `connected`: the one-shot export for the server-side worker.
    /// `None` on `connected` means the export failed and the UI should offer a retry.
    storage_state: Option<serde_json::Value>,
}

#[derive(Serialize)]
pub struct ExportedLogin {
    identity: Identity,
    storage_state: serde_json::Value,
}

impl LoginStatus {
    fn phase(phase: &'static str) -> Self {
        Self {
            phase,
            image: None,
            account: None,
            storage_state: None,
        }
    }

    fn from_page(page: Option<PageLogin>, now: u64) -> Self {
        let Some(page) = page else {
            return Self::phase("loading");
        };
        let now = now as f64;
        if page.observed_at > now + 5000.0 || now - page.observed_at > 15000.0 {
            return Self::phase("action_required");
        }
        let phase = match page.phase {
            PagePhase::Loading => "loading",
            PagePhase::QrReady => "qr_ready",
            PagePhase::Confirming => "confirming",
            PagePhase::ActionRequired => "action_required",
            PagePhase::Expired => "expired",
        };
        let image = page.image.filter(|value| valid_qr_image(value));
        if phase == "qr_ready" {
            if image.is_none() {
                return Self::phase("action_required");
            }
            Self {
                phase,
                image,
                account: None,
                storage_state: None,
            }
        } else {
            Self::phase(phase)
        }
    }
}

/// Parse the snapshot returned by the login page's evaluation. The page script
/// stringifies the payload, so the callback value is normally a JSON string holding
/// the snapshot; a bare object stays accepted for pages that predate the change, and
/// any `null` shape (bare or stringified) means the page reported nothing yet.
fn parse_page_snapshot(value: &str) -> Result<Option<PageSnapshot>, String> {
    let parsed = match serde_json::from_str::<Option<String>>(value) {
        Ok(Some(inner)) => serde_json::from_str::<Option<PageSnapshot>>(&inner),
        _ => serde_json::from_str::<Option<PageSnapshot>>(value),
    };
    parsed.map_err(|_| "平台账号信息解析失败".into())
}

fn valid_qr_image(value: &str) -> bool {
    value.len() <= 600_000
        && [
            "data:image/png;base64,",
            "data:image/jpeg;base64,",
            "data:image/webp;base64,",
        ]
        .iter()
        .any(|prefix| {
            value.strip_prefix(prefix).is_some_and(|data| {
                !data.is_empty()
                    && data
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/' | b'='))
            })
        })
}

struct Login {
    owner: String,
    id: String,
    platform: Platform,
    started: Instant,
    existing: Option<LocalAccount>,
}

#[derive(Default)]
pub struct PublishAccounts {
    logins: Mutex<HashMap<String, Login>>,
    disk: Mutex<()>,
}

fn validate_owner(owner: &str) -> Result<(), String> {
    if owner.is_empty()
        || owner.len() > 96
        || !owner
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return Err("无效的用户标识".into());
    }
    Ok(())
}

fn root(app: &AppHandle, owner: &str) -> Result<PathBuf, String> {
    validate_owner(owner)?;
    // Encode case as bytes: Windows paths are case-insensitive, user IDs are not.
    let owner_dir: String = owner.bytes().map(|byte| format!("{byte:02x}")).collect();
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join("publish-accounts")
        .join(owner_dir))
}

fn account_path(root: &Path, id: &str) -> Result<PathBuf, String> {
    let id = Uuid::parse_str(id).map_err(|_| "无效的账号标识")?;
    Ok(root.join(format!("{id}.json")))
}

fn main_only(window: &WebviewWindow) -> Result<(), String> {
    let origin = window
        .url()
        .map_err(|e| e.to_string())?
        .origin()
        .ascii_serialization();
    let local = matches!(
        origin.as_str(),
        "http://tauri.localhost" | "https://tauri.localhost"
    ) || (cfg!(debug_assertions) && origin == "http://127.0.0.1:5173");
    if window.label() != "main" || !local {
        return Err("仅允许本机主窗口管理发布账号".into());
    }
    Ok(())
}

fn read_accounts(root: &Path) -> Result<Vec<LocalAccount>, String> {
    if !root.exists() {
        return Ok(vec![]);
    }
    let mut accounts = vec![];
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().and_then(|v| v.to_str()) != Some("json") {
            continue;
        }
        let data = fs::read(&path).map_err(|e| e.to_string())?;
        let account: LocalAccount =
            serde_json::from_slice(&data).map_err(|_| "本机账号记录损坏，请联系支持")?;
        if account_path(root, &account.id)? != path {
            return Err("本机账号记录不匹配".into());
        }
        accounts.push(account);
    }
    accounts.sort_by(|a, b| b.verified_at.cmp(&a.verified_at).then(a.id.cmp(&b.id)));
    Ok(accounts)
}

#[derive(Clone, Copy, PartialEq)]
enum OfficialWindowMode {
    Login,
    Publish,
    Clear,
}

impl OfficialWindowMode {
    fn visible(self) -> bool {
        self == Self::Publish
    }
}

fn official_window(
    app: &AppHandle,
    dir: &Path,
    id: &str,
    platform: &Platform,
    mode: OfficialWindowMode,
) -> Result<WebviewWindow, String> {
    if !cfg!(windows) {
        return Err("本地扫码账号管理需要 Windows 桌面客户端".into());
    }
    Uuid::parse_str(id).map_err(|_| "无效的账号标识")?;
    let label = format!("publish-{id}");
    if app.get_webview_window(&label).is_some() {
        return Err("该账号的官方窗口已打开，请先关闭后重试".into());
    }
    let profile = dir.join(id).join("browser");
    fs::create_dir_all(&profile).map_err(|e| e.to_string())?;
    let url = if mode == OfficialWindowMode::Clear {
        "about:blank"
    } else {
        platform.login_url()
    };
    WebviewWindowBuilder::new(
        app,
        label,
        WebviewUrl::External(url.parse().map_err(|_| "平台地址错误")?),
    )
    .visible(mode.visible())
    .focused(mode.visible())
    .skip_taskbar(!mode.visible())
    .title("官方平台 · 扫码登录 / 发布")
    .inner_size(1080.0, 780.0)
    .data_directory(profile)
    .initialization_script(include_str!("publish_identity.js"))
    .initialization_script_for_all_frames(include_str!("publish_login.js"))
    .build()
    .map_err(|e| e.to_string())
}

fn clear_profile(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(windows)]
    {
        use webview2_com::{
            ClearBrowsingDataCompletedHandler,
            Microsoft::Web::WebView2::Win32::{ICoreWebView2Profile2, ICoreWebView2_13},
        };
        use windows_core::Interface;
        let (send, receive) = std::sync::mpsc::channel();
        window
            .with_webview(move |webview| {
                let completed = send.clone();
                let result: windows_core::Result<()> = (|| unsafe {
                    webview
                        .controller()
                        .CoreWebView2()?
                        .cast::<ICoreWebView2_13>()?
                        .Profile()?
                        .cast::<ICoreWebView2Profile2>()?
                        .ClearBrowsingDataAll(&ClearBrowsingDataCompletedHandler::create(Box::new(
                            move |result| {
                                let _ = completed.send(result.map_err(|error| error.to_string()));
                                Ok(())
                            },
                        )))
                })();
                if let Err(error) = result {
                    let _ = send.send(Err(error.to_string()));
                }
            })
            .map_err(|error| error.to_string())?;
        // Commands using this function run off the UI thread; completion runs on it.
        receive
            .recv_timeout(Duration::from_secs(30))
            .map_err(|_| "清除本机登录状态超时，请重试")?
    }
    #[cfg(not(windows))]
    {
        let _ = window;
        Err("本机账号清理需要 Windows 桌面客户端".into())
    }
}

const MAX_LOCAL_STORAGE_JSON: usize = 1_000_000;
const MAX_COOKIE_COUNT: usize = 500;

/// Playwright `storage_state` cookie record built from one WebView2 cookie.
fn cookie_record(
    name: String,
    value: String,
    domain: String,
    path: String,
    expires: f64,
    http_only: bool,
    secure: bool,
    same_site: &str,
) -> serde_json::Value {
    serde_json::json!({
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": if expires.is_finite() && expires > 0.0 { expires } else { -1.0 },
        "httpOnly": http_only,
        "secure": secure,
        "sameSite": same_site,
    })
}

fn platform_cookie_domain(domain: &str, platform: &Platform) -> bool {
    let host = domain.trim_start_matches('.').to_ascii_lowercase();
    let roots: &[&str] = match platform {
        Platform::Douyin => &["douyin.com"],
        Platform::WechatChannels => &["weixin.qq.com", "qq.com"],
        Platform::Xiaohongshu => &["xiaohongshu.com"],
    };
    roots
        .iter()
        .any(|root| host == *root || host.ends_with(&format!(".{root}")))
}

#[cfg(windows)]
fn collect_cookie_records(
    list: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2CookieList,
    platform: &Platform,
) -> Result<Vec<serde_json::Value>, String> {
    use webview2_com::{
        take_pwstr,
        Microsoft::Web::WebView2::Win32::{
            COREWEBVIEW2_COOKIE_SAME_SITE_KIND_LAX, COREWEBVIEW2_COOKIE_SAME_SITE_KIND_STRICT,
        },
    };
    use windows_core::PWSTR;
    let mut count = 0u32;
    // SAFETY: plain COM getters on a live cookie list handed to the completion callback.
    unsafe { list.Count(&mut count) }.map_err(|e| e.to_string())?;
    let mut records = Vec::new();
    for index in 0..count.min(MAX_COOKIE_COUNT as u32) {
        let cookie = unsafe { list.GetValueAtIndex(index) }.map_err(|e| e.to_string())?;
        let mut name = PWSTR::null();
        let mut value = PWSTR::null();
        let mut domain = PWSTR::null();
        let mut path = PWSTR::null();
        let mut expires = 0f64;
        let mut http_only = windows_core::BOOL(0);
        let mut secure = windows_core::BOOL(0);
        let mut same_site = COREWEBVIEW2_COOKIE_SAME_SITE_KIND_LAX;
        unsafe {
            cookie.Name(&mut name).map_err(|e| e.to_string())?;
            cookie.Value(&mut value).map_err(|e| e.to_string())?;
            cookie.Domain(&mut domain).map_err(|e| e.to_string())?;
            cookie.Path(&mut path).map_err(|e| e.to_string())?;
            cookie.Expires(&mut expires).map_err(|e| e.to_string())?;
            cookie
                .IsHttpOnly(&mut http_only)
                .map_err(|e| e.to_string())?;
            cookie.IsSecure(&mut secure).map_err(|e| e.to_string())?;
            cookie.SameSite(&mut same_site).map_err(|e| e.to_string())?;
        }
        // take_pwstr frees the CoTaskMem buffers even for cookies we drop.
        let (name, value, domain, path) = (
            take_pwstr(name),
            take_pwstr(value),
            take_pwstr(domain),
            take_pwstr(path),
        );
        if !platform_cookie_domain(&domain, platform) {
            continue;
        }
        let same_site_label = if same_site == COREWEBVIEW2_COOKIE_SAME_SITE_KIND_STRICT {
            "Strict"
        } else if same_site == COREWEBVIEW2_COOKIE_SAME_SITE_KIND_LAX {
            "Lax"
        } else {
            "None"
        };
        records.push(cookie_record(
            name,
            value,
            domain,
            path,
            expires,
            http_only.as_bool(),
            secure.as_bool(),
            same_site_label,
        ));
    }
    Ok(records)
}

fn read_cookies(
    window: &WebviewWindow,
    platform: &Platform,
) -> Result<Vec<serde_json::Value>, String> {
    #[cfg(windows)]
    {
        use webview2_com::{
            GetCookiesCompletedHandler,
            Microsoft::Web::WebView2::Win32::{ICoreWebView2CookieList, ICoreWebView2_2},
        };
        use windows_core::{Interface, HSTRING};
        let (send, receive) = std::sync::mpsc::channel::<Result<Vec<serde_json::Value>, String>>();
        let origin = HSTRING::from(platform.origin());
        let platform = platform.clone();
        window
            .with_webview(move |webview| {
                let completed = send.clone();
                let handler = GetCookiesCompletedHandler::create(Box::new(
                    move |result: windows_core::Result<()>,
                          list: Option<ICoreWebView2CookieList>| {
                        let outcome = result
                            .map_err(|error| error.to_string())
                            .and_then(|_| list.ok_or_else(|| "cookie list missing".to_string()))
                            .and_then(|list| collect_cookie_records(&list, &platform));
                        let _ = completed.send(outcome);
                        Ok(())
                    },
                ));
                let result: windows_core::Result<()> = (|| unsafe {
                    webview
                        .controller()
                        .CoreWebView2()?
                        .cast::<ICoreWebView2_2>()?
                        .CookieManager()?
                        .GetCookies(&origin, &handler)
                })();
                if let Err(error) = result {
                    let _ = send.send(Err(error.to_string()));
                }
            })
            .map_err(|error| error.to_string())?;
        receive
            .recv_timeout(Duration::from_secs(30))
            .map_err(|_| "读取本机登录 Cookie 超时，请重试".to_string())?
    }
    #[cfg(not(windows))]
    {
        let _ = (window, platform);
        Err("本机登录态导出需要 Windows 桌面客户端".into())
    }
}

fn read_local_storage(window: &WebviewWindow) -> Result<Vec<serde_json::Value>, String> {
    let (send, receive) = std::sync::mpsc::channel();
    window
        .eval_with_callback(
            "JSON.stringify(Object.entries(localStorage).map(([name, value]) => ({name, value})))",
            move |value| {
                let _ = send.send(value);
            },
        )
        .map_err(|e| e.to_string())?;
    let raw = receive
        .recv_timeout(Duration::from_secs(5))
        .map_err(|_| "读取本机登录状态超时，请重试")?;
    if raw.len() > MAX_LOCAL_STORAGE_JSON {
        return Err("平台登录状态过大，请重新扫码".into());
    }
    // eval returns the JSON of a JSON string; unwrap both layers.
    let inner: String = serde_json::from_str(&raw).map_err(|_| "平台登录状态解析失败")?;
    let entries: Vec<serde_json::Value> =
        serde_json::from_str(&inner).map_err(|_| "平台登录状态解析失败")?;
    Ok(entries
        .into_iter()
        .filter(|entry| entry.get("name").and_then(|v| v.as_str()).is_some())
        .filter(|entry| entry.get("value").and_then(|v| v.as_str()).is_some())
        .collect())
}

/// Export the official window's login state in Playwright `storage_state` shape.
fn export_storage_state(
    window: &WebviewWindow,
    platform: &Platform,
) -> Result<serde_json::Value, String> {
    let cookies = read_cookies(window, platform)?;
    if cookies.is_empty() {
        return Err("本机登录态没有平台 Cookie，请重新扫码".into());
    }
    let local_storage = read_local_storage(window)?;
    Ok(serde_json::json!({
        "cookies": cookies,
        "origins": [{"origin": platform.origin(), "localStorage": local_storage}],
    }))
}

pub fn close_all_windows(app: &AppHandle) {
    for (label, window) in app.webview_windows() {
        if label.starts_with("publish-") {
            let _ = window.close();
        }
    }
}

/// Close the official window of a session that reached a terminal state without
/// a saved account (expiry, duplicate scan, mismatched identity). Fresh logins
/// also get their WebView2 profile cleared so a half-scanned platform session
/// never leaks into the next QR pull; re-verifications keep the stored profile.
fn discard_login_session(
    app: &AppHandle,
    dir: &Path,
    logins: &mut HashMap<String, Login>,
    login_id: &str,
) -> Result<(), String> {
    let Some(login) = logins.get(login_id) else {
        return Ok(());
    };
    let fresh = login.existing.is_none();
    let platform = login.platform.clone();
    let opened = app.get_webview_window(&format!("publish-{login_id}"));
    let window = match opened {
        Some(window) => {
            window
                .navigate("about:blank".parse().map_err(|_| "页面地址错误")?)
                .map_err(|e| e.to_string())?;
            window
        }
        None if fresh => official_window(app, dir, login_id, &platform, OfficialWindowMode::Clear)?,
        None => {
            logins.remove(login_id);
            return Ok(());
        }
    };
    if fresh {
        if let Err(error) = clear_profile(&window) {
            let _ = window.close();
            return Err(error);
        }
    }
    window.close().map_err(|e| e.to_string())?;
    logins.remove(login_id);
    Ok(())
}

#[tauri::command]
pub async fn list_local_publish_accounts(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
) -> Result<Vec<LocalAccount>, String> {
    main_only(&window)?;
    let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
    read_accounts(&root(&app, &owner)?)
}

#[tauri::command]
pub async fn start_local_publish_login(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    platform: Platform,
    account_id: Option<String>,
) -> Result<String, String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    let mut logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
    // Closed windows from an interrupted UI session do not block the next login.
    logins.retain(|_, login| {
        app.get_webview_window(&format!("publish-{}", login.id))
            .is_some()
    });
    if logins.values().any(|login| login.owner == owner) {
        return Err("请先完成或取消当前扫码".into());
    }
    let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
    let existing = match account_id {
        Some(id) => Some(
            read_accounts(&dir)?
                .into_iter()
                .find(|a| a.id == id && a.platform == platform)
                .ok_or("账号不存在")?,
        ),
        None => None,
    };
    let id = existing
        .as_ref()
        .map(|a| a.id.clone())
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    official_window(&app, &dir, &id, &platform, OfficialWindowMode::Login)?;
    logins.insert(
        id.clone(),
        Login {
            owner,
            id: id.clone(),
            platform,
            started: Instant::now(),
            existing,
        },
    );
    Ok(id)
}

#[tauri::command]
pub async fn check_local_publish_login(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    login_id: String,
) -> Result<LoginStatus, String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    let (platform, elapsed) = {
        let mut logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
        let login = logins
            .get(&login_id)
            .filter(|v| v.owner == owner)
            .ok_or("扫码会话不存在")?;
        if login.started.elapsed() > Duration::from_secs(300) {
            // Terminal: close the hidden window and drop the session so the next
            // login is not blocked by a QR pull nobody polls anymore.
            discard_login_session(&app, &dir, &mut logins, &login_id)?;
            return Ok(LoginStatus::phase("expired"));
        }
        (login.platform.clone(), login.started.elapsed())
    };
    let Some(official) = app.get_webview_window(&format!("publish-{login_id}")) else {
        return Ok(LoginStatus::phase("closed"));
    };
    let url = official.url().map_err(|e| e.to_string())?;
    if url.as_str() == "about:blank" && elapsed < Duration::from_secs(45) {
        return Ok(LoginStatus::phase("loading"));
    }
    if url.origin().ascii_serialization() != platform.origin() {
        return Ok(LoginStatus::phase("action_required"));
    }
    let (send, receive) = std::sync::mpsc::channel();
    // Return the payload as a JSON string: WebView2's default serialisation writes
    // `Date.now()` as a float token (e.g. 1.789963907261e+12), which the Rust side
    // must not have to guess at. Stringifying in the page keeps it byte-exact.
    let script = format!(
        "location.origin === {} ? JSON.stringify({{identity: window.__xiangshuPublishIdentity || null, login: window.__xiangshuReadPublishLogin ? window.__xiangshuReadPublishLogin() : window.__xiangshuPublishLogin || null}}) : \"null\"",
        serde_json::to_string(platform.origin()).map_err(|e| e.to_string())?
    );
    official
        .eval_with_callback(script, move |value| {
            let _ = send.send(value);
        })
        .map_err(|e| e.to_string())?;
    let response =
        tauri::async_runtime::spawn_blocking(move || receive.recv_timeout(Duration::from_secs(5)))
            .await
            .map_err(|e| e.to_string())?;
    // Navigation can briefly delay script execution; do not exhaust the UI's
    // retry budget before the platform's initial login page has loaded.
    let value = match response {
        Ok(value) => value,
        Err(_) if elapsed < Duration::from_secs(45) => return Ok(LoginStatus::phase("loading")),
        Err(_) => return Err("官方页面未响应，请重新获取二维码或打开官方窗口检查".into()),
    };
    if value.len() > 620_000 {
        return Err("平台登录信息过大，请重新扫码".into());
    }
    let snapshot = parse_page_snapshot(&value)?;
    let Some(snapshot) = snapshot else {
        return Ok(LoginStatus::phase("loading"));
    };
    let Some(identity) = snapshot.identity else {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_millis() as u64;
        return Ok(LoginStatus::from_page(snapshot.login, now));
    };
    if identity.username.trim().is_empty()
        || identity.platform_user_id.trim().is_empty()
        || identity.username.len() > 1024
        || identity.platform_user_id.len() > 1024
    {
        return Err("平台返回的账号信息不完整".into());
    }
    let mut logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
    // Copy the field the terminal path needs; `discard_login_session` takes &mut.
    let existing_uid = {
        let login = logins
            .get(&login_id)
            .filter(|v| v.owner == owner)
            .ok_or("扫码会话已取消")?;
        login.existing.as_ref().map(|a| a.platform_user_id.clone())
    };
    if let Some(previous_uid) = existing_uid {
        if previous_uid != identity.platform_user_id {
            let account_file = account_path(&dir, &login_id)?;
            discard_login_session(&app, &dir, &mut logins, &login_id)?;
            if account_file.exists() {
                fs::remove_file(&account_file).map_err(|e| e.to_string())?;
            }
            return Err("扫码账号与原账号不同，已清除该登录状态；请取消并重新添加账号".into());
        }
    }
    let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
    if read_accounts(&dir)?.iter().any(|a| {
        a.platform == platform
            && a.platform_user_id == identity.platform_user_id
            && a.id != login_id
    }) {
        // Terminal: the scanned account already exists, so this session must not
        // keep its window or half-used profile around.
        discard_login_session(&app, &dir, &mut logins, &login_id)?;
        return Err("该平台账号已连接，请使用已有账号重新验证".into());
    }
    let account = LocalAccount {
        id: login_id.clone(),
        platform,
        platform_user_id: identity.platform_user_id,
        username: identity.username.trim().into(),
        verified_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_secs(),
        avatar_url: identity
            .avatar_url
            .filter(|value| valid_avatar_url(value))
            .map(|value| value.trim().to_string()),
    };
    let bytes = serde_json::to_vec(&account).map_err(|e| e.to_string())?;
    super::customer_credentials::write_file_atomically(&account_path(&dir, &login_id)?, &bytes)
        .map_err(|e| e.to_string())?;
    // One-shot export for the server-side worker; a failure here keeps the local
    // account (manual publishing still works) and surfaces as `storage_state: null`.
    let storage_state = export_storage_state(&official, &account.platform).ok();
    official.close().map_err(|e| e.to_string())?;
    logins.remove(&login_id);
    Ok(LoginStatus {
        phase: "connected",
        image: None,
        account: Some(account),
        storage_state,
    })
}

#[tauri::command]
pub async fn export_local_publish_account_state(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    account_id: String,
) -> Result<ExportedLogin, String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    {
        let logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
        if logins.contains_key(&account_id) {
            return Err("请先完成或取消该账号的扫码会话".into());
        }
    }
    let account = {
        let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
        read_accounts(&dir)?
            .into_iter()
            .find(|a| a.id == account_id)
            .ok_or("账号不存在，请重新扫码")?
    };
    let official = official_window(
        &app,
        &dir,
        &account.id,
        &account.platform,
        OfficialWindowMode::Login,
    )?;
    let outcome = wait_for_identity(&official, &account.platform)
        .await
        .and_then(|identity| {
            if identity.platform_user_id != account.platform_user_id {
                return Err("当前登录的平台账号与原账号不同，请解绑后重新添加".to_string());
            }
            let storage_state = export_storage_state(&official, &account.platform)?;
            Ok(ExportedLogin {
                identity,
                storage_state,
            })
        });
    let _ = official.close();
    outcome
}

/// Poll the hidden official window until the identity endpoint has been observed.
async fn wait_for_identity(
    official: &WebviewWindow,
    platform: &Platform,
) -> Result<Identity, String> {
    let deadline = Instant::now() + Duration::from_secs(45);
    let script = format!(
        "location.origin === {} ? (window.__xiangshuPublishIdentity || null) : null",
        serde_json::to_string(platform.origin()).map_err(|e| e.to_string())?
    );
    while Instant::now() < deadline {
        let url = official.url().map_err(|e| e.to_string())?;
        if url.origin().ascii_serialization() == platform.origin() {
            let (send, receive) = std::sync::mpsc::channel();
            official
                .eval_with_callback(script.clone(), move |value| {
                    let _ = send.send(value);
                })
                .map_err(|e| e.to_string())?;
            let response = tauri::async_runtime::spawn_blocking(move || {
                receive.recv_timeout(Duration::from_secs(5))
            })
            .await
            .map_err(|e| e.to_string())?;
            if let Ok(value) = response {
                if value.len() <= 20_000 {
                    if let Ok(Some(identity)) = serde_json::from_str::<Option<Identity>>(&value) {
                        if !identity.platform_user_id.trim().is_empty()
                            && !identity.username.trim().is_empty()
                        {
                            return Ok(identity);
                        }
                    }
                }
            }
        }
        tauri::async_runtime::spawn_blocking(|| std::thread::sleep(Duration::from_secs(1)))
            .await
            .map_err(|e| e.to_string())?;
    }
    Err("本机登录态已失效，请重新扫码".into())
}

#[tauri::command]
pub async fn focus_local_publish_login(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    login_id: String,
) -> Result<(), String> {
    main_only(&window)?;
    validate_owner(&owner)?;
    let logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
    logins
        .get(&login_id)
        .filter(|login| login.owner == owner)
        .ok_or("扫码会话不存在")?;
    let official = app
        .get_webview_window(&format!("publish-{login_id}"))
        .ok_or("官方窗口已关闭，请重新扫码")?;
    official.unminimize().map_err(|e| e.to_string())?;
    official
        .set_skip_taskbar(false)
        .map_err(|e| e.to_string())?;
    official.show().map_err(|e| e.to_string())?;
    official.set_focus().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn cancel_local_publish_login(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    login_id: String,
) -> Result<(), String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    let mut logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
    let fresh = logins
        .get(&login_id)
        .filter(|v| v.owner == owner)
        .map(|login| login.existing.is_none());
    if let Some(fresh) = fresh {
        // A failed close after metadata persistence can leave a pending login.
        // Cancel must remove that partial record only after clearing succeeds.
        discard_login_session(&app, &dir, &mut logins, &login_id)?;
        if fresh {
            let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
            let metadata = account_path(&dir, &login_id)?;
            if metadata.exists() {
                fs::remove_file(metadata).map_err(|e| e.to_string())?;
            }
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn open_local_publish_account(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    account_id: String,
) -> Result<(), String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
    let account = read_accounts(&dir)?
        .into_iter()
        .find(|a| a.id == account_id)
        .ok_or("账号不存在，请重新扫码")?;
    official_window(
        &app,
        &dir,
        &account.id,
        &account.platform,
        OfficialWindowMode::Publish,
    )?;
    Ok(())
}

#[tauri::command]
pub async fn remove_local_publish_account(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, PublishAccounts>,
    owner: String,
    account_id: String,
) -> Result<(), String> {
    main_only(&window)?;
    let dir = root(&app, &owner)?;
    let logins = state.logins.lock().map_err(|_| "扫码会话忙，请重试")?;
    if logins.contains_key(&account_id) {
        return Err("请先取消该账号的扫码会话".into());
    }
    let _lock = state.disk.lock().map_err(|_| "账号存储忙，请重试")?;
    let account = read_accounts(&dir)?
        .into_iter()
        .find(|a| a.id == account_id)
        .ok_or("账号不存在")?;
    // Clear only this exact WebView2 profile; no cookies are read into Rust or JS.
    let official = official_window(
        &app,
        &dir,
        &account.id,
        &account.platform,
        OfficialWindowMode::Clear,
    )?;
    if let Err(error) = clear_profile(&official) {
        let _ = official.close();
        return Err(error);
    }
    official.close().map_err(|e| e.to_string())?;
    fs::remove_file(account_path(&dir, &account.id)?).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn login_and_cleanup_do_not_open_a_visible_official_window() {
        assert!(!OfficialWindowMode::Login.visible());
        assert!(!OfficialWindowMode::Clear.visible());
        assert!(OfficialWindowMode::Publish.visible());
    }
    #[test]
    fn qr_snapshot_rejects_stale_remote_and_non_raster_images() {
        for image in [
            "https://example.com/qr.png",
            "data:image/svg+xml;base64,cXI=",
            "data:image/png;base64,",
        ] {
            assert!(!valid_qr_image(image));
        }
        assert!(!valid_qr_image(&format!(
            "data:image/png;base64,{}",
            "a".repeat(600_000)
        )));
        let page = |at| {
            Some(PageLogin {
                phase: PagePhase::QrReady,
                image: Some("data:image/png;base64,cXI=".into()),
                observed_at: at,
            })
        };
        let ready = LoginStatus::from_page(page(20_000.0), 20_001);
        assert_eq!(ready.phase, "qr_ready");
        assert!(ready.image.is_some());
        assert!(ready.account.is_none());
        let stale = LoginStatus::from_page(page(1000.0), 20_001);
        assert_eq!(stale.phase, "action_required");
        assert!(stale.image.is_none());
        assert_eq!(
            LoginStatus::from_page(page(30_000.0), 20_001).phase,
            "action_required"
        );
    }
    #[test]
    fn owner_and_account_paths_cannot_escape_profile_root() {
        for invalid in [
            "",
            "..",
            "../alice",
            "alice/bob",
            "C:\\alice",
            " alice",
            "a.b",
        ] {
            assert!(validate_owner(invalid).is_err());
        }
        assert!(validate_owner("customer_1-a").is_ok());
        let base = Path::new("profiles");
        assert!(account_path(base, "../alice").is_err());
        let id = Uuid::new_v4().to_string();
        assert_eq!(
            account_path(base, &id).unwrap(),
            base.join(format!("{id}.json"))
        );
    }
    #[test]
    fn cookie_export_filters_platform_domains_and_normalizes_playwright_fields() {
        assert!(platform_cookie_domain(".douyin.com", &Platform::Douyin));
        assert!(platform_cookie_domain(
            "creator.douyin.com",
            &Platform::Douyin
        ));
        assert!(!platform_cookie_domain("notdouyin.com", &Platform::Douyin));
        assert!(!platform_cookie_domain(".example.com", &Platform::Douyin));
        assert!(platform_cookie_domain(
            ".weixin.qq.com",
            &Platform::WechatChannels
        ));
        assert!(platform_cookie_domain(".qq.com", &Platform::WechatChannels));
        assert!(!platform_cookie_domain(
            ".douyin.com",
            &Platform::WechatChannels
        ));
        let session = cookie_record(
            "sid".into(),
            "v".into(),
            ".douyin.com".into(),
            "/".into(),
            -1.0,
            true,
            true,
            "Lax",
        );
        assert_eq!(session["expires"], -1.0);
        assert_eq!(session["httpOnly"], true);
        assert_eq!(session["sameSite"], "Lax");
        let persistent = cookie_record(
            "a".into(),
            "b".into(),
            "creator.douyin.com".into(),
            "/x".into(),
            1_800_000_000.5,
            false,
            false,
            "None",
        );
        assert_eq!(persistent["expires"], 1_800_000_000.5);
        assert_eq!(persistent["path"], "/x");
    }
    #[test]
    fn avatar_url_accepts_only_bounded_https_links() {
        assert!(valid_avatar_url("https://p26.douyinpic.com/a.jpeg"));
        assert!(valid_avatar_url("https://wx.qlogo.cn/finderhead/abc/0"));
        for invalid in [
            "",
            "   ",
            "http://p26.douyinpic.com/a.jpeg",
            "data:image/png;base64,iVBORw0KGgo=",
            "javascript:alert(1)",
            "https://example.test/a b.jpeg",
        ] {
            assert!(!valid_avatar_url(invalid), "should reject {invalid:?}");
        }
        assert!(!valid_avatar_url(&format!(
            "https://p26.douyinpic.com/{}.jpeg",
            "a".repeat(1024)
        )));
    }

    #[test]
    fn accounts_saved_before_avatars_still_load() {
        // Records written by earlier builds carry no avatar_url key.
        let legacy = serde_json::json!({
            "id": Uuid::new_v4().to_string(),
            "platform": "douyin",
            "platform_user_id": "synthetic-uid",
            "username": "旧记录",
            "verified_at": 1,
        });
        let account: LocalAccount = serde_json::from_value(legacy).unwrap();
        assert_eq!(account.username, "旧记录");
        assert!(account.avatar_url.is_none());
    }

    #[test]
    fn index_never_contains_credentials_and_is_isolated_per_owner() {
        let root = std::env::temp_dir().join(format!("publish-test-{}", Uuid::new_v4()));
        let a = root.join("alice");
        let b = root.join("bob");
        let account = LocalAccount {
            id: Uuid::new_v4().to_string(),
            platform: Platform::Douyin,
            platform_user_id: "synthetic-uid".into(),
            username: "测试昵称".into(),
            verified_at: 1,
            avatar_url: Some("https://p26.douyinpic.com/synthetic.jpeg".into()),
        };
        let bytes = serde_json::to_vec(&account).unwrap();
        super::super::customer_credentials::write_file_atomically(
            &account_path(&a, &account.id).unwrap(),
            &bytes,
        )
        .unwrap();
        assert_eq!(read_accounts(&a).unwrap()[0].username, "测试昵称");
        assert!(read_accounts(&b).unwrap().is_empty());
        assert!(!String::from_utf8(bytes).unwrap().contains("cookie"));
        fs::remove_file(account_path(&a, &account.id).unwrap()).unwrap();
        fs::remove_dir(a).unwrap();
        fs::remove_dir(root).unwrap();
    }

    // A real snapshot reaches Rust through WebView2's script callback: `Date.now()`
    // serialises as a float (e.g. 1.789963907261e+12) and the callback may hand the
    // whole object back wrapped in a JSON string. Both shapes must parse.
    #[test]
    fn page_snapshot_accepts_string_wrapped_payload() {
        let inner = serde_json::to_string(&serde_json::json!({
            "identity": null,
            "login": {
                "phase": "qr_ready",
                "image": "data:image/png;base64,cXI=",
                "observed_at": 1_789_963_907_261u64,
            },
        }))
        .unwrap();
        let wrapped = serde_json::to_string(&inner).unwrap();
        let snapshot = parse_page_snapshot(&wrapped).unwrap().unwrap();
        assert!(matches!(snapshot.login.unwrap().phase, PagePhase::QrReady));
    }

    #[test]
    fn page_snapshot_accepts_scientific_notation_inside_wrapped_json() {
        let inner = r#"{"identity":null,"login":{"phase":"qr_ready","image":"data:image/png;base64,cXI=","observed_at":1.789963907261e+12}}"#;
        let wrapped = serde_json::to_string(inner).unwrap();
        let snapshot = parse_page_snapshot(&wrapped).unwrap().unwrap();
        assert!(matches!(snapshot.login.unwrap().phase, PagePhase::QrReady));
    }

    #[test]
    fn page_snapshot_accepts_raw_float_timestamp() {
        let raw = r#"{"identity":null,"login":{"phase":"loading","image":null,"observed_at":1789963907261.0}}"#;
        let snapshot = parse_page_snapshot(raw).unwrap().unwrap();
        assert!(matches!(snapshot.login.unwrap().phase, PagePhase::Loading));
    }

    #[test]
    fn page_snapshot_treats_string_null_as_loading() {
        assert!(parse_page_snapshot("\"null\"").unwrap().is_none());
    }

    #[test]
    fn page_snapshot_treats_bare_null_as_loading() {
        assert!(parse_page_snapshot("null").unwrap().is_none());
    }

    #[test]
    fn page_snapshot_rejects_garbage() {
        assert!(parse_page_snapshot("<html>login</html>").is_err());
    }
}
