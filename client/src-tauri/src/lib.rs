mod customer_credentials;
mod publish_accounts;
mod video_downloads;
mod viral_cache;
mod viral_decrypt;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(video_downloads::VideoDownloads::default())
        .manage(publish_accounts::PublishAccounts::default())
        .invoke_handler(tauri::generate_handler![
            publish_accounts::list_local_publish_accounts,
            publish_accounts::start_local_publish_login,
            publish_accounts::check_local_publish_login,
            publish_accounts::focus_local_publish_login,
            publish_accounts::cancel_local_publish_login,
            publish_accounts::open_local_publish_account,
            publish_accounts::remove_local_publish_account,
            publish_accounts::export_local_publish_account_state,
            customer_credentials::customer_device_instance_id,
            customer_credentials::customer_save_credentials,
            customer_credentials::customer_load_credentials,
            customer_credentials::customer_clear_session_token,
            customer_credentials::customer_clear_all_credentials,
            customer_credentials::customer_save_remembered_login,
            customer_credentials::customer_clear_remembered_login,
            video_downloads::choose_video_download,
            video_downloads::start_video_download,
            video_downloads::cancel_video_download,
            video_downloads::get_video_download_status,
            video_downloads::open_video_download_folder,
            viral_cache::viral_cache_ensure,
            viral_cache::viral_cache_status,
            viral_cache::viral_cache_list,
            viral_cache::viral_cache_delete,
            viral_cache::viral_cache_clear,
            viral_cache::viral_cache_open_folder,
        ])
        .setup(|app| {
            // 缓存根目录落在应用数据目录下，必须在 App 就绪后才能解析，
            // 因此用 manage 而不是在 builder 链上直接构造。
            app.manage(viral_cache::ViralCache::from_app(app.handle())?);
            let main = app
                .config()
                .app
                .windows
                .iter()
                .find(|config| config.label == "main")
                .ok_or("main window configuration is missing")?;
            tauri::WebviewWindowBuilder::from_config(app, main)?
                .on_download(video_downloads::on_download)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build desktop application")
        .run(|app, event| {
            if let tauri::RunEvent::WindowEvent {
                label,
                event: tauri::WindowEvent::Destroyed,
                ..
            } = event
            {
                if label == "main" {
                    publish_accounts::close_all_windows(app);
                }
            }
        });
}
