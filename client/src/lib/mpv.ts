import { invoke } from "@tauri-apps/api/core";

export async function mpvStart(): Promise<void> {
  return invoke("mpv_start");
}

export async function mpvStop(): Promise<void> {
  return invoke("mpv_stop");
}

export async function mpvLoad(videoUrl: string, audioUrl: string): Promise<void> {
  return invoke("mpv_load", { videoUrl, audioUrl });
}

export async function mpvSeek(time: number): Promise<void> {
  return invoke("mpv_seek", { time });
}

export async function mpvPause(): Promise<void> {
  return invoke("mpv_pause");
}

export async function mpvResume(): Promise<void> {
  return invoke("mpv_resume");
}

export async function mpvSetLoop(a: number, b: number): Promise<void> {
  return invoke("mpv_set_loop", { a, b });
}

export async function mpvClearLoop(): Promise<void> {
  return invoke("mpv_clear_loop");
}

export async function mpvTimePos(): Promise<number> {
  return invoke("mpv_time_pos");
}

export async function mpvDuration(): Promise<number> {
  return invoke("mpv_duration");
}
