use tauri::State;
use std::sync::Mutex;
use crate::mpv::Mpv;

pub struct MpvState(pub Mutex<Mpv>);

#[tauri::command]
pub fn mpv_start(state: State<MpvState>) -> Result<(), String> {
    state.0.lock().unwrap().start()
}

#[tauri::command]
pub fn mpv_stop(state: State<MpvState>) -> Result<(), String> {
    state.0.lock().unwrap().stop();
    Ok(())
}

#[tauri::command]
pub fn mpv_load(state: State<MpvState>, video_url: String, audio_url: String) -> Result<(), String> {
    state.0.lock().unwrap().load_file(&video_url, &audio_url)
}

#[tauri::command]
pub fn mpv_seek(state: State<MpvState>, time: f64) -> Result<(), String> {
    state.0.lock().unwrap().seek(time)
}

#[tauri::command]
pub fn mpv_pause(state: State<MpvState>) -> Result<(), String> {
    state.0.lock().unwrap().pause()
}

#[tauri::command]
pub fn mpv_resume(state: State<MpvState>) -> Result<(), String> {
    state.0.lock().unwrap().resume()
}

#[tauri::command]
pub fn mpv_set_loop(state: State<MpvState>, a: f64, b: f64) -> Result<(), String> {
    state.0.lock().unwrap().set_loop(a, b)
}

#[tauri::command]
pub fn mpv_clear_loop(state: State<MpvState>) -> Result<(), String> {
    state.0.lock().unwrap().clear_loop()
}

#[tauri::command]
pub fn mpv_time_pos(state: State<MpvState>) -> Result<f64, String> {
    state.0.lock().unwrap().time_pos()
}

#[tauri::command]
pub fn mpv_duration(state: State<MpvState>) -> Result<f64, String> {
    state.0.lock().unwrap().get_duration()
}
