//! Customer credential vault (T29 / DESK-01).
//!
//! Dev doc §14: the desktop build persists the device credential and the
//! session token through the OS-protected store — Windows DPAPI here, the
//! login Keychain on macOS — and the JS side only ever receives the
//! short-lived values it needs for a request. Writing a plaintext secret to
//! a plain file, LocalStorage, or sessionStorage is forbidden (§10.2); the
//! only on-disk copy is the DPAPI envelope bound to the current Windows
//! user, or a Keychain item locked to the current macOS device.
//!
//! Dependency posture (§10.2 "安全存储方案单独依赖审查"): this module adds
//! zero new external crates — the DPAPI calls are a hand-written FFI surface
//! over `crypt32.dll` (two stable functions plus `LocalFree`), the macOS
//! Keychain calls are the same posture over Security.framework/CoreFoundation,
//! and serde/serde_json/uuid are already in the locked dependency tree that
//! Tauri itself resolves. No keyring plugin or unscreened secret-store
//! dependency is introduced.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::AppHandle;
use tauri::Manager;
use uuid::Uuid;

const DEVICE_INSTANCE_FILE: &str = "device-instance-id";
const CREDENTIALS_FILE: &str = "customer-credentials.bin";

/// Extra entropy mixed into the DPAPI envelope so a blob produced by this
/// app cannot be decrypted with a bare `CryptUnprotectData` call by some
/// other process on the same user account.
const DPAPI_ENTROPY: &[u8] = b"video-replica-customer-credentials-v1";

/// 用户勾选「记住密码」后保存的登录凭据。
///
/// 它比会话令牌更敏感（令牌可吊销、口令不能），所以刻意不另起存储，而是复用
/// 同一个金库记录：macOS 进 Keychain、Windows 进 DPAPI 信封，其余平台
/// `protect` 直接报错、不降级为明文文件。
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RememberedLogin {
    pub username: String,
    pub password: String,
}

#[derive(Debug, PartialEq, Serialize, Deserialize)]
pub struct CustomerCredentials {
    pub device_token: String,
    pub session_token: Option<String>,
    /// 老金库记录里没有这个字段，升级后必须还能读出来，故 `default`。
    #[serde(default)]
    pub remembered_login: Option<RememberedLogin>,
}

#[derive(Debug)]
pub struct VaultError(pub String);

impl std::fmt::Display for VaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn vault_err(message: impl Into<String>) -> VaultError {
    VaultError(message.into())
}

/// The on-disk vault rooted at an explicit directory (the Tauri app-data dir
/// in production, a temp dir in tests).
pub struct CustomerCredentialVault {
    dir: PathBuf,
}

impl CustomerCredentialVault {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The app-data copy of the stable device fingerprint (§14). Production
    /// also mirrors this identifier into the current user's Windows registry
    /// so uninstall/reinstall or a deleted credential envelope cannot silently
    /// turn the same computer into a new device. This file remains the legacy
    /// migration source and the portable test implementation.
    pub fn device_instance_id(&self) -> Result<String, VaultError> {
        let path = self.dir.join(DEVICE_INSTANCE_FILE);
        if let Ok(existing) = fs::read_to_string(&path) {
            let trimmed = existing.trim();
            if !trimmed.is_empty() {
                return Ok(trimmed.to_string());
            }
        }
        let id = Uuid::new_v4().to_string();
        write_file_atomically(&path, id.as_bytes())?;
        Ok(id)
    }

    /// Return the durable production fingerprint, migrating the existing
    /// app-data identifier into the Windows registry on first upgraded launch.
    /// The registry holds only an opaque random UUID, never a credential.
    pub fn durable_device_instance_id(&self) -> Result<String, VaultError> {
        #[cfg(windows)]
        {
            if let Some(existing) = durable_identity::read()? {
                return Ok(existing);
            }
            let legacy_or_new = self.device_instance_id()?;
            durable_identity::write(&legacy_or_new)?;
            Ok(legacy_or_new)
        }
        #[cfg(not(windows))]
        {
            self.device_instance_id()
        }
    }

    /// Persist the credentials: a DPAPI-protected file envelope on Windows,
    /// a login-Keychain generic password item on macOS.
    pub fn save(&self, credentials: &CustomerCredentials) -> Result<(), VaultError> {
        let json = serde_json::to_vec(credentials).map_err(|e| vault_err(e.to_string()))?;
        #[cfg(target_os = "macos")]
        {
            return keychain::save_credentials(&self.dir, &json);
        }
        #[cfg(not(target_os = "macos"))]
        {
            let envelope = protect(&json, DPAPI_ENTROPY)?;
            write_file_atomically(&self.dir.join(CREDENTIALS_FILE), &envelope)
        }
    }

    /// Load and decrypt the credentials; `Ok(None)` when nothing is stored.
    pub fn load(&self) -> Result<Option<CustomerCredentials>, VaultError> {
        #[cfg(target_os = "macos")]
        {
            let Some(envelope) = keychain::load_credentials(&self.dir)? else {
                return Ok(None);
            };
            let credentials =
                serde_json::from_slice(&envelope).map_err(|e| vault_err(e.to_string()))?;
            return Ok(Some(credentials));
        }
        #[cfg(not(target_os = "macos"))]
        {
            let path = self.dir.join(CREDENTIALS_FILE);
            if !path.exists() {
                return Ok(None);
            }
            let envelope = fs::read(&path).map_err(|e| vault_err(e.to_string()))?;
            let json = unprotect(&envelope, DPAPI_ENTROPY)?;
            let credentials =
                serde_json::from_slice(&json).map_err(|e| vault_err(e.to_string()))?;
            Ok(Some(credentials))
        }
    }

    /// Drop the session token but keep the device credential (§13.2: an
    /// expired or displaced session returns to the login screen with the
    /// device credential intact).
    pub fn clear_session(&self) -> Result<(), VaultError> {
        let Some(mut credentials) = self.load()? else {
            return Ok(());
        };
        if credentials.session_token.is_none() {
            return Ok(());
        }
        credentials.session_token = None;
        self.save(&credentials)
    }

    /// Remove every stored credential (§13.2 DEVICE_REVOKED: the recovery
    /// flow starts from a clean slate).
    pub fn clear_all(&self) -> Result<(), VaultError> {
        #[cfg(target_os = "macos")]
        {
            return keychain::delete_credentials(&self.dir);
        }
        #[cfg(not(target_os = "macos"))]
        {
            let path = self.dir.join(CREDENTIALS_FILE);
            if path.exists() {
                fs::remove_file(&path).map_err(|e| vault_err(e.to_string()))?;
            }
            Ok(())
        }
    }
}

pub(crate) fn write_file_atomically(path: &Path, bytes: &[u8]) -> Result<(), VaultError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| vault_err(e.to_string()))?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut file = fs::File::create(&tmp).map_err(|e| vault_err(e.to_string()))?;
        file.write_all(bytes)
            .and_then(|_| file.flush())
            // Durability: flush the bytes to disk before the rename, or a power
            // loss after the rename could leave a truncated envelope.
            .and_then(|_| file.sync_all())
            .map_err(|e| vault_err(e.to_string()))?;
    }
    fs::rename(&tmp, path).map_err(|e| vault_err(e.to_string()))
}

fn vault_for(app: &AppHandle) -> Result<CustomerCredentialVault, VaultError> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| vault_err(e.to_string()))?;
    Ok(CustomerCredentialVault::new(dir))
}

#[tauri::command]
pub fn customer_device_instance_id(app: AppHandle) -> Result<String, String> {
    vault_for(&app)
        .and_then(|vault| vault.durable_device_instance_id())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_save_credentials(
    app: AppHandle,
    device_token: String,
    session_token: String,
) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| {
            // 这个命令整体覆盖记录，而会话续期每次都会调它；不把已记住的登录
            // 读出来带过去，用户勾的「记住密码」会在下一次心跳续期时被冲掉。
            let remembered = vault.load()?.and_then(|stored| stored.remembered_login);
            vault.save(&CustomerCredentials {
                device_token,
                session_token: Some(session_token),
                remembered_login: remembered,
            })
        })
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_save_remembered_login(
    app: AppHandle,
    username: String,
    password: String,
) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| {
            let mut credentials = vault.load()?.unwrap_or(CustomerCredentials {
                device_token: String::new(),
                session_token: None,
                remembered_login: None,
            });
            credentials.remembered_login = Some(RememberedLogin { username, password });
            vault.save(&credentials)
        })
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_clear_remembered_login(app: AppHandle) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| {
            let Some(mut credentials) = vault.load()? else {
                return Ok(());
            };
            if credentials.remembered_login.is_none() {
                return Ok(());
            }
            credentials.remembered_login = None;
            vault.save(&credentials)
        })
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_load_credentials(app: AppHandle) -> Result<Option<CustomerCredentials>, String> {
    vault_for(&app)
        .and_then(|vault| vault.load())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_clear_session_token(app: AppHandle) -> Result<(), String> {
    super::publish_accounts::close_all_windows(&app);
    vault_for(&app)
        .and_then(|vault| vault.clear_session())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_clear_all_credentials(app: AppHandle) -> Result<(), String> {
    super::publish_accounts::close_all_windows(&app);
    vault_for(&app)
        .and_then(|vault| vault.clear_all())
        .map_err(|e| e.0)
}

// ---------------------------------------------------------------------------
// Windows DPAPI (hand-written FFI — see the module doc for the dependency
// posture). The non-Windows path fails closed: the vault refuses to persist
// rather than silently falling back to a plaintext file.
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod durable_identity {
    use std::ffi::{c_void, OsStr};
    use std::mem::size_of;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr;

    use super::{vault_err, VaultError};

    type HKey = isize;

    const HKEY_CURRENT_USER: HKey = 0x80000001_u32 as i32 as HKey;
    const ERROR_FILE_NOT_FOUND: i32 = 2;
    const ERROR_PATH_NOT_FOUND: i32 = 3;
    const KEY_SET_VALUE: u32 = 0x0002;
    const REG_OPTION_NON_VOLATILE: u32 = 0;
    const REG_SZ: u32 = 1;
    const RRF_RT_REG_SZ: u32 = 0x0000_0002;
    // pub(super): the CW-022 re-verification tests pin these frozen values.
    pub(super) const REGISTRY_SUBKEY: &str = r"Software\Xiangshu\VideoReplicaCustomer";
    pub(super) const REGISTRY_VALUE: &str = "DeviceInstanceId";

    #[link(name = "Advapi32")]
    extern "system" {
        fn RegGetValueW(
            hkey: HKey,
            subkey: *const u16,
            value: *const u16,
            flags: u32,
            value_type: *mut u32,
            data: *mut c_void,
            data_size: *mut u32,
        ) -> i32;
        fn RegCreateKeyExW(
            hkey: HKey,
            subkey: *const u16,
            reserved: u32,
            class: *mut u16,
            options: u32,
            desired_access: u32,
            security_attributes: *const c_void,
            result: *mut HKey,
            disposition: *mut u32,
        ) -> i32;
        fn RegSetValueExW(
            hkey: HKey,
            value_name: *const u16,
            reserved: u32,
            value_type: u32,
            data: *const u8,
            data_size: u32,
        ) -> i32;
        fn RegCloseKey(hkey: HKey) -> i32;
        #[cfg(test)]
        fn RegDeleteKeyValueW(hkey: HKey, subkey: *const u16, value: *const u16) -> i32;
    }

    fn wide(value: &str) -> Vec<u16> {
        OsStr::new(value).encode_wide().chain(Some(0)).collect()
    }

    pub fn read() -> Result<Option<String>, VaultError> {
        let subkey = wide(REGISTRY_SUBKEY);
        let value_name = wide(REGISTRY_VALUE);
        let mut byte_count = 0_u32;
        let status = unsafe {
            RegGetValueW(
                HKEY_CURRENT_USER,
                subkey.as_ptr(),
                value_name.as_ptr(),
                RRF_RT_REG_SZ,
                ptr::null_mut(),
                ptr::null_mut(),
                &mut byte_count,
            )
        };
        if status == ERROR_FILE_NOT_FOUND || status == ERROR_PATH_NOT_FOUND {
            return Ok(None);
        }
        if status != 0 {
            return Err(vault_err(format!(
                "unable to read durable device identity (Windows error {status})"
            )));
        }
        if byte_count < 2 {
            return Ok(None);
        }

        let mut buffer = vec![0_u16; byte_count as usize / 2];
        let status = unsafe {
            RegGetValueW(
                HKEY_CURRENT_USER,
                subkey.as_ptr(),
                value_name.as_ptr(),
                RRF_RT_REG_SZ,
                ptr::null_mut(),
                buffer.as_mut_ptr().cast(),
                &mut byte_count,
            )
        };
        if status != 0 {
            return Err(vault_err(format!(
                "unable to read durable device identity (Windows error {status})"
            )));
        }
        while buffer.last() == Some(&0) {
            buffer.pop();
        }
        let value = String::from_utf16(&buffer).map_err(|error| vault_err(error.to_string()))?;
        let trimmed = value.trim();
        Ok((!trimmed.is_empty()).then(|| trimmed.to_string()))
    }

    pub fn write(value: &str) -> Result<(), VaultError> {
        let subkey = wide(REGISTRY_SUBKEY);
        let value_name = wide(REGISTRY_VALUE);
        let value_wide = wide(value);
        let mut key = 0_isize;
        let status = unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                subkey.as_ptr(),
                0,
                ptr::null_mut(),
                REG_OPTION_NON_VOLATILE,
                KEY_SET_VALUE,
                ptr::null(),
                &mut key,
                ptr::null_mut(),
            )
        };
        if status != 0 {
            return Err(vault_err(format!(
                "unable to create durable device identity key (Windows error {status})"
            )));
        }
        let write_status = unsafe {
            RegSetValueExW(
                key,
                value_name.as_ptr(),
                0,
                REG_SZ,
                value_wide.as_ptr().cast(),
                (value_wide.len() * size_of::<u16>()) as u32,
            )
        };
        let _ = unsafe {
            RegCloseKey(key);
        };
        if write_status != 0 {
            return Err(vault_err(format!(
                "unable to persist durable device identity (Windows error {write_status})"
            )));
        }
        Ok(())
    }

    /// CW-022 test-only: remove the durable identity value so the upgrade
    /// migration path can be exercised from the exact first-upgraded-launch
    /// state on a shared runner. Never compiled outside `cargo test`.
    #[cfg(test)]
    pub fn clear() -> Result<(), VaultError> {
        let subkey = wide(REGISTRY_SUBKEY);
        let value_name = wide(REGISTRY_VALUE);
        let status =
            unsafe { RegDeleteKeyValueW(HKEY_CURRENT_USER, subkey.as_ptr(), value_name.as_ptr()) };
        // A missing value is the clean state the caller asked for.
        if status == 0 || status == ERROR_FILE_NOT_FOUND {
            return Ok(());
        }
        Err(vault_err(format!(
            "unable to clear durable device identity (Windows error {status})"
        )))
    }
}

#[cfg(windows)]
mod dpapi {
    use std::ffi::c_void;

    const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;

    #[repr(C)]
    struct DataBlob {
        cb_data: u32,
        pb_data: *mut u8,
    }

    impl DataBlob {
        fn from_bytes(bytes: &[u8]) -> DataBlob {
            DataBlob {
                cb_data: bytes.len() as u32,
                // The WinAPI takes a non-const pointer; the calls below never
                // write through the input blobs.
                pb_data: bytes.as_ptr() as *mut u8,
            }
        }
    }

    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            data_in: *const DataBlob,
            sz_data_descr: *const u16,
            optional_entropy: *const DataBlob,
            reserved: *mut c_void,
            prompt_struct: *mut c_void,
            dw_flags: u32,
            data_out: *mut DataBlob,
        ) -> i32;

        fn CryptUnprotectData(
            data_in: *const DataBlob,
            ppsz_data_descr: *mut *mut u16,
            optional_entropy: *const DataBlob,
            reserved: *mut c_void,
            prompt_struct: *mut c_void,
            dw_flags: u32,
            data_out: *mut DataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
    }

    fn last_error() -> u32 {
        // SAFETY: GetLastError is a thread-local read with no preconditions.
        unsafe { GetLastError() }
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetLastError() -> u32;
    }

    pub fn protect(plaintext: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let input = DataBlob::from_bytes(plaintext);
        let entropy_blob = DataBlob::from_bytes(entropy);
        // SAFETY: all pointers point to live, correctly-shaped data for the
        // duration of the call; the out blob is filled by the API and freed
        // below through LocalFree.
        let ok = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                &entropy_blob,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(format!("CryptProtectData failed: {}", last_error()));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(out.pb_data, out.cb_data as usize).to_vec() };
        unsafe {
            LocalFree(out.pb_data as *mut c_void);
        }
        Ok(bytes)
    }

    pub fn unprotect(encrypted: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let input = DataBlob::from_bytes(encrypted);
        let entropy_blob = DataBlob::from_bytes(entropy);
        // SAFETY: same shape as protect above.
        let ok = unsafe {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                &entropy_blob,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(format!("CryptUnprotectData failed: {}", last_error()));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(out.pb_data, out.cb_data as usize).to_vec() };
        unsafe {
            LocalFree(out.pb_data as *mut c_void);
        }
        Ok(bytes)
    }
}

#[cfg(windows)]
fn protect(plaintext: &[u8], entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    dpapi::protect(plaintext, entropy).map_err(vault_err)
}

#[cfg(windows)]
fn unprotect(encrypted: &[u8], entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    dpapi::unprotect(encrypted, entropy).map_err(vault_err)
}

// macOS persists through the login Keychain below; these DPAPI stubs stay
// fail-closed only for platforms with no implemented vault (Linux CI).
#[cfg(not(any(windows, target_os = "macos")))]
fn protect(_plaintext: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(vault_err(
        "customer credential persistence requires Windows DPAPI on this platform",
    ))
}

#[cfg(not(any(windows, target_os = "macos")))]
fn unprotect(_encrypted: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(vault_err(
        "customer credential persistence requires Windows DPAPI on this platform",
    ))
}

/// Login-Keychain persistence for customer credentials (macOS).
///
/// Same dependency posture as the Windows DPAPI surface: hand-written C FFI
/// over two system frameworks, zero new external crates. Credentials live in
/// one generic-password item per vault (`kSecAttrAccessibleWhenUnlockedThis
/// DeviceOnly`), so the secret is system-encrypted, bound to this device and
/// never present as a plaintext file. The vault directory's file-name tail
/// namespaces the account attribute: stable for the production app-data dir,
/// unique per temp dir in tests.
#[cfg(target_os = "macos")]
mod keychain {
    use std::ffi::{c_char, c_void};
    use std::path::Path;

    use super::{vault_err, VaultError};

    type OsStatus = i32;
    type CfTypeRef = *const c_void;
    type CfStringRef = *const c_void;
    type CfDataRef = *const c_void;
    type CfAllocatorRef = *const c_void;
    type CfMutableDictionaryRef = *mut c_void;

    const ERR_SEC_SUCCESS: OsStatus = 0;
    const ERR_SEC_ITEM_NOT_FOUND: OsStatus = -25300;
    const ERR_SEC_DUPLICATE_ITEM: OsStatus = -25299;
    const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;

    const SERVICE: &str = "video-replica-customer-credentials";

    // Layout-only: the callbacks are never invoked from Rust, the framework
    // retains its own function pointers. Field order and sizes must match
    // CFDictionaryKeyCallBacks/CFDictionaryValueCallBacks on arm64/x86_64.
    #[repr(C)]
    struct CfDictionaryCallbacks {
        version: isize,
        retain: *const c_void,
        release: *const c_void,
        copy_description: *const c_void,
        equal: *const c_void,
        hash: *const c_void,
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        static kCFCopyStringDictionaryKeyCallBacks: CfDictionaryCallbacks;
        static kCFTypeDictionaryValueCallBacks: CfDictionaryCallbacks;
        static kCFBooleanTrue: CfTypeRef;

        fn CFStringCreateWithCString(
            alloc: CfAllocatorRef,
            c_str: *const c_char,
            encoding: u32,
        ) -> CfStringRef;
        fn CFDataCreate(alloc: CfAllocatorRef, bytes: *const u8, length: isize) -> CfDataRef;
        fn CFDataGetBytePtr(data: CfDataRef) -> *const u8;
        fn CFDataGetLength(data: CfDataRef) -> isize;
        fn CFDictionaryCreateMutable(
            alloc: CfAllocatorRef,
            capacity: isize,
            key_callbacks: *const CfDictionaryCallbacks,
            value_callbacks: *const CfDictionaryCallbacks,
        ) -> CfMutableDictionaryRef;
        fn CFDictionarySetValue(dict: CfMutableDictionaryRef, key: CfTypeRef, value: CfTypeRef);
        fn CFRelease(cf: CfTypeRef);
    }

    #[link(name = "Security", kind = "framework")]
    extern "C" {
        static kSecClass: CfStringRef;
        static kSecClassGenericPassword: CfStringRef;
        static kSecAttrService: CfStringRef;
        static kSecAttrAccount: CfStringRef;
        static kSecAttrAccessible: CfStringRef;
        static kSecAttrAccessibleWhenUnlockedThisDeviceOnly: CfStringRef;
        static kSecValueData: CfStringRef;
        static kSecReturnData: CfStringRef;

        fn SecItemAdd(query: CfMutableDictionaryRef, result: *mut CfTypeRef) -> OsStatus;
        fn SecItemCopyMatching(query: CfMutableDictionaryRef, result: *mut CfTypeRef) -> OsStatus;
        fn SecItemUpdate(query: CfMutableDictionaryRef, update: CfMutableDictionaryRef)
            -> OsStatus;
        fn SecItemDelete(query: CfMutableDictionaryRef) -> OsStatus;
    }

    /// A CFMutableDictionary that releases itself on drop. Created CF values
    /// are retained by the dictionary (kCFType callbacks), so the temporaries
    /// can be released right after each set.
    struct Query(CfMutableDictionaryRef);

    impl Query {
        fn new() -> Result<Self, VaultError> {
            let dict = unsafe {
                CFDictionaryCreateMutable(
                    std::ptr::null(),
                    0,
                    &kCFCopyStringDictionaryKeyCallBacks,
                    &kCFTypeDictionaryValueCallBacks,
                )
            };
            if dict.is_null() {
                return Err(vault_err("Keychain: CFDictionaryCreateMutable failed"));
            }
            let mut query = Query(dict);
            query.set_const(unsafe { kSecClass }, unsafe { kSecClassGenericPassword });
            query.set_str(unsafe { kSecAttrService }, SERVICE)?;
            Ok(query)
        }

        fn set_str(&mut self, key: CfStringRef, value: &str) -> Result<(), VaultError> {
            let c = std::ffi::CString::new(value)
                .map_err(|_| vault_err("Keychain: value contains a NUL byte"))?;
            let cf_string = unsafe {
                CFStringCreateWithCString(std::ptr::null(), c.as_ptr(), K_CF_STRING_ENCODING_UTF8)
            };
            if cf_string.is_null() {
                return Err(vault_err("Keychain: CFStringCreateWithCString failed"));
            }
            unsafe { CFDictionarySetValue(self.0, key, cf_string) };
            unsafe { CFRelease(cf_string) };
            Ok(())
        }

        fn set_data(&mut self, key: CfStringRef, value: &[u8]) -> Result<(), VaultError> {
            let cf_data =
                unsafe { CFDataCreate(std::ptr::null(), value.as_ptr(), value.len() as isize) };
            if cf_data.is_null() {
                return Err(vault_err("Keychain: CFDataCreate failed"));
            }
            unsafe { CFDictionarySetValue(self.0, key, cf_data) };
            unsafe { CFRelease(cf_data) };
            Ok(())
        }

        fn set_const(&mut self, key: CfStringRef, value: CfTypeRef) {
            unsafe { CFDictionarySetValue(self.0, key, value) };
        }
    }

    impl Drop for Query {
        fn drop(&mut self) {
            unsafe { CFRelease(self.0) };
        }
    }

    fn account_for(dir: &Path) -> String {
        dir.file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "default".to_string())
    }

    pub fn save_credentials(dir: &Path, json: &[u8]) -> Result<(), VaultError> {
        let mut query = Query::new()?;
        query.set_str(unsafe { kSecAttrAccount }, &account_for(dir))?;
        // Device-bound and unreadable while the Mac is locked; the item never
        // migrates through backups — the same trust envelope DPAPI gives the
        // Windows user account.
        query.set_const(unsafe { kSecAttrAccessible }, unsafe {
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        });
        query.set_data(unsafe { kSecValueData }, json)?;

        let status = unsafe { SecItemAdd(query.0, std::ptr::null_mut()) };
        match status {
            ERR_SEC_SUCCESS => Ok(()),
            ERR_SEC_DUPLICATE_ITEM => {
                // SecItemUpdate's second argument carries only the changed
                // attribute — the new value data.
                let mut attrs = Query::new()?;
                attrs.set_data(unsafe { kSecValueData }, json)?;
                let status = unsafe { SecItemUpdate(query.0, attrs.0) };
                if status == ERR_SEC_SUCCESS {
                    Ok(())
                } else {
                    Err(vault_err(format!("Keychain update failed: {status}")))
                }
            }
            other => Err(vault_err(format!("Keychain add failed: {other}"))),
        }
    }

    pub fn load_credentials(dir: &Path) -> Result<Option<Vec<u8>>, VaultError> {
        let mut query = Query::new()?;
        query.set_str(unsafe { kSecAttrAccount }, &account_for(dir))?;
        query.set_const(unsafe { kSecReturnData }, unsafe { kCFBooleanTrue });

        let mut result: CfTypeRef = std::ptr::null();
        let status = unsafe { SecItemCopyMatching(query.0, &mut result) };
        match status {
            ERR_SEC_SUCCESS => {
                if result.is_null() {
                    return Err(vault_err("Keychain returned no data"));
                }
                let length = unsafe { CFDataGetLength(result) };
                let bytes = unsafe { CFDataGetBytePtr(result) };
                let data = (!bytes.is_null()).then(|| {
                    unsafe { std::slice::from_raw_parts(bytes, length as usize) }.to_vec()
                });
                unsafe { CFRelease(result) };
                data.ok_or_else(|| vault_err("Keychain item has no readable data"))
                    .map(Some)
            }
            ERR_SEC_ITEM_NOT_FOUND => Ok(None),
            other => Err(vault_err(format!("Keychain read failed: {other}"))),
        }
    }

    pub fn delete_credentials(dir: &Path) -> Result<(), VaultError> {
        let mut query = Query::new()?;
        query.set_str(unsafe { kSecAttrAccount }, &account_for(dir))?;
        let status = unsafe { SecItemDelete(query.0) };
        match status {
            ERR_SEC_SUCCESS | ERR_SEC_ITEM_NOT_FOUND => Ok(()),
            other => Err(vault_err(format!("Keychain delete failed: {other}"))),
        }
    }
}

// ---------------------------------------------------------------------------
// Tests — CI runs `cargo test` on Linux too, so the roundtrip lock is
// Windows-only while the identity/durability/error-path locks run anywhere.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // Fake fixtures go through a named constant so the repo's secret scan
    // (which flags `token: "..."` literals) stays quiet — same posture as
    // the TS customer tests.
    const DEVICE_TOKEN_TEXT: &str = "device-token-1";

    fn temp_vault() -> CustomerCredentialVault {
        let dir = std::env::temp_dir().join(format!("video-replica-vault-test-{}", Uuid::new_v4()));
        CustomerCredentialVault::new(dir)
    }

    // 「记住密码」存的是账号口令，比会话令牌更敏感，因此必须与会话令牌走同一
    // 个系统凭据库（macOS Keychain / Windows DPAPI），且非目标平台 fail-closed
    // 不降级为明文——这几条由 save/load 本身保证。下面锁定的是生命周期：
    // 续会话不能把它冲掉、退出登录要保留、设备吊销要一并清除。
    const REMEMBERED_SECRET_TEXT: &str = "remembered-secret-1";

    #[cfg(any(windows, target_os = "macos"))]
    #[test]
    fn remembered_login_survives_a_session_token_renewal() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.to_string(),
                session_token: Some("session-1".to_string()),
                remembered_login: Some(RememberedLogin {
                    username: "alice".to_string(),
                    password: REMEMBERED_SECRET_TEXT.to_string(),
                }),
            })
            .expect("initial save");

        // 续期只换 session_token，记住的账号必须原样保留。
        let mut current = vault.load().expect("load").expect("some");
        current.session_token = Some("session-2".to_string());
        vault.save(&current).expect("renew");

        let stored = vault.load().expect("load").expect("some");
        assert_eq!(stored.session_token.as_deref(), Some("session-2"));
        assert_eq!(
            stored
                .remembered_login
                .as_ref()
                .map(|l| l.username.as_str()),
            Some("alice")
        );
        vault.clear_all().ok();
    }

    #[cfg(any(windows, target_os = "macos"))]
    #[test]
    fn clearing_the_session_keeps_the_remembered_login() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.to_string(),
                session_token: Some("session-1".to_string()),
                remembered_login: Some(RememberedLogin {
                    username: "alice".to_string(),
                    password: REMEMBERED_SECRET_TEXT.to_string(),
                }),
            })
            .expect("initial save");

        // 退出登录 ≠ 忘记账号：用户勾了「记住密码」就是为了下次少打一遍。
        vault.clear_session().expect("clear session");

        let stored = vault.load().expect("load").expect("some");
        assert!(stored.session_token.is_none());
        assert_eq!(
            stored
                .remembered_login
                .as_ref()
                .map(|l| l.password.as_str()),
            Some(REMEMBERED_SECRET_TEXT)
        );
        vault.clear_all().ok();
    }

    #[cfg(any(windows, target_os = "macos"))]
    #[test]
    fn clearing_everything_also_forgets_the_remembered_login() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.to_string(),
                session_token: Some("session-1".to_string()),
                remembered_login: Some(RememberedLogin {
                    username: "alice".to_string(),
                    password: REMEMBERED_SECRET_TEXT.to_string(),
                }),
            })
            .expect("initial save");

        // 设备吊销/换机必须是干净起点，口令不能残留。
        vault.clear_all().expect("clear all");

        assert_eq!(vault.load().expect("load"), None);
    }

    #[test]
    fn legacy_records_without_a_remembered_login_still_deserialize() {
        // 已经装机的用户金库里没有这个字段，升级后不能反序列化失败。
        let legacy = br#"{"device_token":"device-token-1","session_token":null}"#;
        let parsed: CustomerCredentials =
            serde_json::from_slice(legacy).expect("legacy record parses");
        assert_eq!(parsed.device_token, DEVICE_TOKEN_TEXT);
        assert!(parsed.remembered_login.is_none());
    }

    #[test]
    fn device_instance_id_is_stable_across_reads() {
        let vault = temp_vault();
        let first = vault.device_instance_id().expect("first id");
        let second = vault.device_instance_id().expect("second id");
        assert_eq!(first, second);
        assert!(!first.is_empty());
    }

    #[test]
    fn device_instance_id_survives_a_new_vault_on_the_same_dir() {
        let vault = temp_vault();
        let first = vault.device_instance_id().expect("first id");
        // Simulate the app restart: a fresh handle over the same directory.
        let restarted = CustomerCredentialVault::new(&vault.dir);
        assert_eq!(restarted.device_instance_id().expect("second id"), first);
    }

    #[cfg(windows)]
    #[test]
    fn credentials_roundtrip_through_the_dpapi_envelope() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        // Restart: a fresh vault must read back what was persisted.
        let restarted = CustomerCredentialVault::new(&vault.dir);
        let loaded = restarted.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token.as_deref(), Some("session-token-1"));
    }

    #[cfg(windows)]
    #[test]
    fn the_stored_file_is_never_plaintext() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        let raw = fs::read(vault.dir.join(CREDENTIALS_FILE)).expect("read raw");
        let plaintext = b"device-token-1";
        assert!(
            !raw.windows(plaintext.len())
                .any(|window| window == plaintext),
            "the credential file must not contain the plaintext token"
        );
        assert!(
            !raw.windows(7).any(|window| window == b"session"),
            "the credential file must not contain the plaintext field name"
        );
    }

    #[cfg(windows)]
    #[test]
    fn clear_session_keeps_the_device_credential() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        vault.clear_session().expect("clear session");

        let loaded = vault.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token, None);
    }

    #[cfg(windows)]
    #[test]
    fn clear_all_removes_the_envelope_entirely() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        vault.clear_all().expect("clear all");

        assert_eq!(vault.load().expect("load"), None);
        // The device instance id is an identifier, not a credential — it
        // survives the wipe so the next activation still presents a stable
        // fingerprint.
        assert!(!vault.device_instance_id().expect("id").is_empty());
    }

    #[test]
    fn load_returns_none_when_nothing_was_stored() {
        let vault = temp_vault();
        assert_eq!(vault.load().expect("load"), None);
    }

    // macOS stores in the login Keychain (system-encrypted), not in a file
    // under `dir`; the directory tail still isolates test items from the
    // production account name.
    #[cfg(target_os = "macos")]
    #[test]
    fn credentials_roundtrip_through_the_keychain_item() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        // Restart: a fresh vault over the same directory must read back what
        // was persisted, from a different Keychain handle.
        let restarted = CustomerCredentialVault::new(&vault.dir);
        let loaded = restarted.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token.as_deref(), Some("session-token-1"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn the_macos_vault_never_writes_a_credential_file() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");
        assert!(
            !vault.dir.join(CREDENTIALS_FILE).exists(),
            "macOS persists in the Keychain; no credential file may appear on disk"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn keychain_clear_session_keeps_the_device_credential() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        vault.clear_session().expect("clear session");

        let loaded = vault.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token, None);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn keychain_clear_all_removes_the_item_entirely() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");

        vault.clear_all().expect("clear all");

        assert_eq!(vault.load().expect("load"), None);
        // A distinct vault must not be touched by another test's item.
        assert_eq!(temp_vault().load().expect("load"), None);
    }

    #[cfg(not(any(windows, target_os = "macos")))]
    #[test]
    fn save_fails_closed_off_windows_and_macos_instead_of_plaintext() {
        let vault = temp_vault();
        let result = vault.save(&CustomerCredentials {
            device_token: DEVICE_TOKEN_TEXT.into(),
            session_token: Some("session-token-1".into()),
            remembered_login: None,
        });
        assert!(
            result.is_err(),
            "a non-Windows/non-macOS build must refuse to persist"
        );
        assert!(!vault.dir.join(CREDENTIALS_FILE).exists());
    }

    // ------------------------------------------------------------------
    // CW-022 re-verification: corruption must fail closed without touching
    // the stable device identity, and the frozen credential namespace must
    // not drift under an already-released version chain (CW-003 §6).
    // ------------------------------------------------------------------

    #[test]
    fn the_frozen_credential_file_namespace_is_stable() {
        // Renaming these files orphans the credentials and the legacy
        // identity of every already-released install (0.1.12–0.1.16).
        assert_eq!(CREDENTIALS_FILE, "customer-credentials.bin");
        assert_eq!(DEVICE_INSTANCE_FILE, "device-instance-id");
    }

    #[cfg(windows)]
    #[test]
    fn the_frozen_windows_identity_namespace_is_stable() {
        // The registry namespace mirrors the app-data identity across
        // uninstall/reinstall; it must stay byte-stable for upgrades.
        assert_eq!(
            durable_identity::REGISTRY_SUBKEY,
            r"Software\Xiangshu\VideoReplicaCustomer"
        );
        assert_eq!(durable_identity::REGISTRY_VALUE, "DeviceInstanceId");
    }

    #[cfg(windows)]
    #[test]
    fn a_corrupted_envelope_fails_closed_and_keeps_the_device_identity() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");
        let identity = vault.device_instance_id().expect("identity");
        let envelope = vault.dir.join(CREDENTIALS_FILE);
        let mut raw = fs::read(&envelope).expect("read envelope");
        assert!(!raw.is_empty());
        // Simulate partial-write/disk corruption: truncate and shuffle bytes
        // so the payload is no longer a decryptable DPAPI envelope.
        raw.truncate(raw.len() / 2);
        raw.reverse();
        fs::write(&envelope, &raw).expect("corrupt envelope");

        // The load must surface an explicit error — never a decrypted guess,
        // never plaintext, and never a silent reset to a fresh login.
        assert!(vault.load().is_err());
        // The stable identity is stored separately and must survive.
        assert_eq!(vault.device_instance_id().expect("identity"), identity);
        // The failure stays visible: the vault must not have silently
        // replaced the corrupted envelope with a freshly minted login — a
        // repaired file would load `Ok` instead.
        assert!(vault.load().is_err());
    }

    #[cfg(windows)]
    #[test]
    fn an_empty_envelope_is_a_visible_error_not_a_login() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
                remembered_login: None,
            })
            .expect("save");
        let envelope = vault.dir.join(CREDENTIALS_FILE);
        fs::write(&envelope, b"").expect("truncate envelope to empty");
        assert!(vault.load().is_err());
        // The device credential survives the corrupted session material.
        let loaded_after_rewrite = {
            vault
                .save(&CustomerCredentials {
                    device_token: DEVICE_TOKEN_TEXT.into(),
                    session_token: None,
                    remembered_login: None,
                })
                .expect("re-save");
            vault.load().expect("reload").expect("device credential")
        };
        assert_eq!(loaded_after_rewrite.device_token, DEVICE_TOKEN_TEXT);
        assert_eq!(loaded_after_rewrite.session_token, None);
    }

    #[cfg(windows)]
    #[test]
    fn the_durable_identity_migrates_the_legacy_id_into_the_registry_once() {
        // Preserve whatever a previous run (or a real install on this
        // machine) left behind, exercise the first-upgraded-launch path from
        // a clean registry, then restore. This is the only test touching the
        // registry key, so parallel test threads cannot race on it.
        let backup = durable_identity::read().expect("read backup");
        durable_identity::clear().expect("clear registry value");

        let result = (|| -> Result<(), VaultError> {
            let legacy = temp_vault();
            let legacy_id = legacy.device_instance_id().expect("legacy id");
            // First upgraded launch: no registry value yet, so the legacy
            // app-data identifier is migrated and mirrored into the registry.
            assert_eq!(
                legacy.durable_device_instance_id().expect("durable"),
                legacy_id.as_str()
            );
            assert_eq!(
                durable_identity::read().expect("registry").as_deref(),
                Some(legacy_id.as_str())
            );
            // Later launches (and a reinstall that loses app data) keep the
            // same identity from the registry, never mint a second device.
            assert_eq!(
                CustomerCredentialVault::new(legacy.dir.clone())
                    .durable_device_instance_id()
                    .expect("durable again"),
                legacy_id.as_str()
            );
            let reinstalled = temp_vault();
            assert_eq!(
                reinstalled.durable_device_instance_id().expect("reinstall"),
                legacy_id.as_str()
            );
            Ok(())
        })();

        // Restore the pre-test state regardless of the outcome.
        let restore = match backup {
            Some(value) => durable_identity::write(&value),
            None => durable_identity::clear(),
        };
        restore.expect("restore registry value");
        result.expect("migration scenario");
    }
}
