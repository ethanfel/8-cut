mod mpv;
mod commands;

use commands::MpvState;
use mpv::Mpv;
use std::sync::Mutex;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(MpvState(Mutex::new(Mpv::new())))
        .invoke_handler(tauri::generate_handler![
            commands::mpv_start,
            commands::mpv_stop,
            commands::mpv_load,
            commands::mpv_seek,
            commands::mpv_pause,
            commands::mpv_resume,
            commands::mpv_set_loop,
            commands::mpv_clear_loop,
            commands::mpv_time_pos,
            commands::mpv_duration,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
