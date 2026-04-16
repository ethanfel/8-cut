# 8-cut Client Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Tauri + Svelte desktop client with full feature parity to the Qt app, connecting to the 8-cut server API.

**Architecture:** Tauri (Rust) manages an mpv sidecar process via JSON IPC. Svelte renders the UI in a webview. All data comes from the server REST API. Export progress arrives over WebSocket.

**Tech Stack:** Tauri v2, Svelte 5, TypeScript, Vite, Rust, mpv (sidecar via IPC)

---

### Task 1: Install Rust toolchain

**Step 1: Install rustup + stable toolchain**

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
rustc --version
cargo --version
```

**Step 2: Install Tauri CLI and system dependencies**

```bash
cargo install tauri-cli
# Tauri v2 Linux dependencies
sudo pacman -S --needed webkit2gtk-4.1 base-devel curl wget file openssl appmenu-gtk-module gtk3 libappindicator-gtk3 librsvg patchelf
```

**Step 3: Commit nothing** — toolchain install only.

---

### Task 2: Scaffold Tauri + Svelte project

**Files:**
- Create: `client/` (entire scaffold)

**Step 1: Create the project**

```bash
cd /media/p5/8-cut
pnpm create tauri-app client --template svelte-ts --manager pnpm
cd client
pnpm install
```

**Step 2: Verify it builds and opens**

```bash
cd /media/p5/8-cut/client
pnpm tauri dev
```

Expected: A blank Tauri window opens with the default Svelte template.

**Step 3: Clean up template**

Replace `client/src/App.svelte`:

```svelte
<script lang="ts">
</script>

<main>
  <h1>8-cut</h1>
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1e1e1e;
    color: #e0e0e0;
  }
  main {
    padding: 8px;
    height: 100vh;
    box-sizing: border-box;
  }
</style>
```

**Step 4: Commit**

```bash
git add client/
git commit -m "feat: scaffold Tauri + Svelte client"
```

---

### Task 3: API client module

**Files:**
- Create: `client/src/lib/api.ts`

**Step 1: Create the API client**

```typescript
const DEFAULT_SERVER = "http://192.168.1.51:8000";

let serverUrl = DEFAULT_SERVER;

export function setServer(url: string) {
  serverUrl = url.replace(/\/+$/, "");
}

export function getServer(): string {
  return serverUrl;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// --- Files ---

export interface VideoFile {
  name: string;
  path: string;
  root: string;
  size: number;
}

export function getRoots(): Promise<string[]> {
  return get("/api/roots");
}

export function getFiles(root?: string): Promise<VideoFile[]> {
  const q = root ? `?root=${encodeURIComponent(root)}` : "";
  return get(`/api/files${q}`);
}

export function streamUrl(path: string, root: string, quality: string): string {
  return `${serverUrl}/api/stream/${encodeURIComponent(path)}?root=${encodeURIComponent(root)}&quality=${quality}`;
}

export function audioUrl(path: string, root: string): string {
  return `${serverUrl}/api/audio/${encodeURIComponent(path)}?root=${encodeURIComponent(root)}`;
}

export function cacheStatus(path: string, root: string): Promise<Record<string, string>> {
  return get(`/api/cache/status/${encodeURIComponent(path)}?root=${encodeURIComponent(root)}`);
}

// --- Markers & Profiles ---

export interface Marker {
  start_time: number;
  marker_number: number;
  output_path: string;
}

export function getMarkers(filename: string, profile: string = "default"): Promise<Marker[]> {
  return get(`/api/markers/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function getProfiles(): Promise<string[]> {
  return get("/api/profiles");
}

export function getLabels(): Promise<string[]> {
  return get("/api/labels");
}

// --- Export ---

export interface ExportRequest {
  input_path: string;
  cursor: number;
  name: string;
  clips?: number;
  spread?: number;
  short_side?: number | null;
  portrait_ratio?: string | null;
  crop_center?: number;
  format?: string;
  label?: string;
  category?: string;
  profile?: string;
  folder_suffix?: string;
  encoder?: string;
}

export function startExport(req: ExportRequest): Promise<{ job_id: string }> {
  return post("/api/export", req);
}

export function getExportStatus(jobId: string): Promise<{
  status: string;
  total: number;
  completed: number;
  outputs: string[];
  error?: string;
}> {
  return get(`/api/export/${jobId}`);
}

export function deleteExport(outputPath: string): Promise<{ deleted: string }> {
  return del(`/api/export?output_path=${encodeURIComponent(outputPath)}`);
}

// --- Hidden ---

export function hideFile(filename: string, profile: string = "default"): Promise<unknown> {
  return post(`/api/hidden/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function unhideFile(filename: string, profile: string = "default"): Promise<unknown> {
  return del(`/api/hidden/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function getHidden(profile: string = "default"): Promise<string[]> {
  return get(`/api/hidden?profile=${encodeURIComponent(profile)}`);
}
```

**Step 2: Commit**

```bash
git add client/src/lib/api.ts
git commit -m "feat: add server API client module"
```

---

### Task 4: Svelte stores

**Files:**
- Create: `client/src/lib/stores.ts`

**Step 1: Create reactive stores**

```typescript
import { writable, derived } from "svelte/store";
import type { VideoFile, Marker } from "./api";

// --- Connection ---
export const serverUrl = writable("http://192.168.1.51:8000");

// --- Files ---
export const roots = writable<string[]>([]);
export const files = writable<VideoFile[]>([]);
export const hiddenFiles = writable<Set<string>>(new Set());
export const currentFile = writable<VideoFile | null>(null);
export const hideExported = writable(false);
export const showHidden = writable(false);

// --- Playback ---
export const duration = writable(0);
export const cursor = writable(0);
export const playPos = writable<number | null>(null);
export const playing = writable(false);
export const quality = writable("low");

// --- Timeline ---
export const markers = writable<Marker[]>([]);
export const locked = writable(false);

// --- Export settings ---
export const clips = writable(3);
export const spread = writable(3.0);
export const shortSide = writable<number | null>(512);
export const portraitRatio = writable<string | null>(null);
export const cropCenter = writable(0.5);
export const format = writable("MP4");
export const hwEncode = writable(false);
export const label = writable("");
export const category = writable("");
export const clipName = writable("");
export const exportFolder = writable("");
export const encoder = writable("libx264");
export const trackSubject = writable(false);
export const randPortrait = writable(false);
export const randSquare = writable(false);

// --- Profiles ---
export const profile = writable("default");
export const subprofiles = writable<string[]>([]);

// --- Export progress ---
export const exportStatus = writable<string>("idle"); // idle | running | done | error
export const exportCompleted = writable(0);
export const exportTotal = writable(0);

// --- Derived ---
export const clipSpan = derived(
  [clips, spread],
  ([$clips, $spread]) => 8.0 + ($clips - 1) * $spread
);

export const visibleFiles = derived(
  [files, hiddenFiles, hideExported, showHidden, markers],
  ([$files, $hidden, $hideExported, $showHidden, $markers]) => {
    const exportedNames = new Set($markers.map(m => m.output_path));
    return $files.filter(f => {
      if (!$showHidden && $hidden.has(f.name)) return false;
      // hideExported filtering would need per-file marker lookup
      return true;
    });
  }
);
```

**Step 2: Commit**

```bash
git add client/src/lib/stores.ts
git commit -m "feat: add Svelte stores for app state"
```

---

### Task 5: WebSocket export progress

**Files:**
- Create: `client/src/lib/ws.ts`

**Step 1: Create WebSocket client**

```typescript
import { getServer } from "./api";
import { exportStatus, exportCompleted } from "./stores";

let socket: WebSocket | null = null;

export function connectExportWs() {
  const wsUrl = getServer().replace(/^http/, "ws") + "/ws/export";
  socket = new WebSocket(wsUrl);

  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "clip_done":
        exportCompleted.update(n => n + 1);
        break;
      case "all_done":
        exportStatus.set("done");
        break;
      case "error":
        exportStatus.set("error");
        console.error("Export error:", msg.msg);
        break;
    }
  };

  socket.onclose = () => {
    // Reconnect after 2s
    setTimeout(connectExportWs, 2000);
  };
}

export function disconnectExportWs() {
  if (socket) {
    socket.onclose = null; // prevent reconnect
    socket.close();
    socket = null;
  }
}
```

**Step 2: Commit**

```bash
git add client/src/lib/ws.ts
git commit -m "feat: add WebSocket client for export progress"
```

---

### Task 6: mpv sidecar — Rust backend

**Files:**
- Create: `client/src-tauri/src/mpv.rs`
- Modify: `client/src-tauri/src/main.rs`
- Modify: `client/src-tauri/src/lib.rs`

**Step 1: Create mpv.rs**

This module spawns mpv with `--input-ipc-server`, then sends JSON IPC commands over the Unix socket.

```rust
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command};
use std::sync::Mutex;
use serde_json::{json, Value};

pub struct Mpv {
    process: Option<Child>,
    socket: Option<UnixStream>,
    socket_path: String,
}

impl Mpv {
    pub fn new() -> Self {
        let socket_path = format!("/tmp/8cut-mpv-{}", std::process::id());
        Mpv {
            process: None,
            socket: None,
            socket_path,
        }
    }

    pub fn start(&mut self) -> Result<(), String> {
        // Kill existing
        self.stop();

        let child = Command::new("mpv")
            .args([
                "--idle=yes",
                "--force-window=no",
                "--vo=null",
                "--keep-open=yes",
                &format!("--input-ipc-server={}", self.socket_path),
            ])
            .spawn()
            .map_err(|e| format!("Failed to start mpv: {e}"))?;

        self.process = Some(child);

        // Wait for socket
        for _ in 0..50 {
            std::thread::sleep(std::time::Duration::from_millis(100));
            if let Ok(stream) = UnixStream::connect(&self.socket_path) {
                stream.set_nonblocking(false).ok();
                self.socket = Some(stream);
                return Ok(());
            }
        }
        Err("Timeout waiting for mpv IPC socket".into())
    }

    pub fn stop(&mut self) {
        if let Some(ref mut child) = self.process {
            child.kill().ok();
            child.wait().ok();
        }
        self.process = None;
        self.socket = None;
        std::fs::remove_file(&self.socket_path).ok();
    }

    pub fn command(&mut self, args: &[&str]) -> Result<(), String> {
        let socket = self.socket.as_mut().ok_or("mpv not running")?;
        let cmd = json!({ "command": args });
        let mut msg = serde_json::to_string(&cmd).unwrap();
        msg.push('\n');
        socket.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;

        // Read response
        let mut reader = BufReader::new(socket.try_clone().map_err(|e| e.to_string())?);
        let mut line = String::new();
        reader.read_line(&mut line).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn set_property(&mut self, name: &str, value: Value) -> Result<(), String> {
        let socket = self.socket.as_mut().ok_or("mpv not running")?;
        let cmd = json!({ "command": ["set_property", name, value] });
        let mut msg = serde_json::to_string(&cmd).unwrap();
        msg.push('\n');
        socket.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;

        let mut reader = BufReader::new(socket.try_clone().map_err(|e| e.to_string())?);
        let mut line = String::new();
        reader.read_line(&mut line).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn get_property(&mut self, name: &str) -> Result<Value, String> {
        let socket = self.socket.as_mut().ok_or("mpv not running")?;
        let cmd = json!({ "command": ["get_property", name] });
        let mut msg = serde_json::to_string(&cmd).unwrap();
        msg.push('\n');
        socket.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;

        let mut reader = BufReader::new(socket.try_clone().map_err(|e| e.to_string())?);
        let mut line = String::new();
        reader.read_line(&mut line).map_err(|e| e.to_string())?;
        let resp: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
        Ok(resp.get("data").cloned().unwrap_or(Value::Null))
    }

    pub fn load_file(&mut self, video_url: &str, audio_url: &str) -> Result<(), String> {
        self.command(&["loadfile", video_url])?;
        self.set_property("audio-files", json!(audio_url))?;
        Ok(())
    }

    pub fn seek(&mut self, time: f64) -> Result<(), String> {
        self.command(&["seek", &time.to_string(), "absolute"])
    }

    pub fn pause(&mut self) -> Result<(), String> {
        self.set_property("pause", json!(true))
    }

    pub fn resume(&mut self) -> Result<(), String> {
        self.set_property("pause", json!(false))
    }

    pub fn set_loop(&mut self, a: f64, b: f64) -> Result<(), String> {
        self.set_property("ab-loop-a", json!(a))?;
        self.set_property("ab-loop-b", json!(b))
    }

    pub fn clear_loop(&mut self) -> Result<(), String> {
        self.set_property("ab-loop-a", json!("no"))?;
        self.set_property("ab-loop-b", json!("no"))
    }

    pub fn time_pos(&mut self) -> Result<f64, String> {
        let val = self.get_property("time-pos")?;
        val.as_f64().ok_or("time-pos not a number".into())
    }

    pub fn get_duration(&mut self) -> Result<f64, String> {
        let val = self.get_property("duration")?;
        val.as_f64().ok_or("duration not a number".into())
    }
}

impl Drop for Mpv {
    fn drop(&mut self) {
        self.stop();
    }
}
```

**Step 2: Create Tauri commands in commands.rs**

Create `client/src-tauri/src/commands.rs`:

```rust
use tauri::State;
use std::sync::Mutex;
use serde_json::Value;
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
```

**Step 3: Wire up main.rs / lib.rs**

`client/src-tauri/src/lib.rs`:

```rust
mod mpv;
mod commands;

use commands::MpvState;
use mpv::Mpv;
use std::sync::Mutex;

pub fn run() {
    tauri::Builder::default()
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
```

`client/src-tauri/src/main.rs`:

```rust
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    client_lib::run();
}
```

Add `serde_json` to `client/src-tauri/Cargo.toml` dependencies:

```toml
[dependencies]
serde_json = "1"
serde = { version = "1", features = ["derive"] }
tauri = { version = "2", features = [] }
tauri-build = { version = "2", features = [] }
```

**Step 4: Verify it compiles**

```bash
cd /media/p5/8-cut/client
pnpm tauri build --debug 2>&1 | tail -5
```

**Step 5: Commit**

```bash
git add client/src-tauri/
git commit -m "feat: add mpv sidecar IPC and Tauri commands"
```

---

### Task 7: mpv TypeScript bridge

**Files:**
- Create: `client/src/lib/mpv.ts`

**Step 1: Create the bridge**

```typescript
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
```

**Step 2: Commit**

```bash
git add client/src/lib/mpv.ts
git commit -m "feat: add mpv TypeScript bridge"
```

---

### Task 8: File browser component

**Files:**
- Create: `client/src/components/FileBrowser.svelte`

**Step 1: Create file browser**

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { getFiles, getRoots, getHidden, getMarkers, hideFile, unhideFile } from "$lib/api";
  import {
    files, roots, hiddenFiles, currentFile, hideExported, showHidden,
    profile, markers
  } from "$lib/stores";

  let selectedRoot = "";

  onMount(async () => {
    $roots = await getRoots();
    if ($roots.length) {
      selectedRoot = $roots[0];
      await loadFiles();
    }
  });

  async function loadFiles() {
    $files = await getFiles(selectedRoot);
    const hidden = await getHidden($profile);
    $hiddenFiles = new Set(hidden);
  }

  async function selectFile(file: typeof $files[0]) {
    $currentFile = file;
    $markers = await getMarkers(file.name, $profile);
  }

  function formatSize(bytes: number): string {
    if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + " GB";
    if (bytes > 1e6) return (bytes / 1e6).toFixed(0) + " MB";
    return (bytes / 1e3).toFixed(0) + " KB";
  }

  $: filteredFiles = $files.filter(f => {
    if (!$showHidden && $hiddenFiles.has(f.name)) return false;
    return true;
  });
</script>

<div class="file-browser">
  <div class="controls">
    <select bind:value={selectedRoot} on:change={loadFiles}>
      {#each $roots as root}
        <option value={root}>{root}</option>
      {/each}
    </select>
    <label><input type="checkbox" bind:checked={$showHidden} /> Hidden</label>
  </div>
  <ul class="file-list">
    {#each filteredFiles as file}
      <li
        class:selected={$currentFile?.path === file.path}
        on:click={() => selectFile(file)}
        on:contextmenu|preventDefault={() => {
          if ($hiddenFiles.has(file.name)) {
            unhideFile(file.name, $profile).then(loadFiles);
          } else {
            hideFile(file.name, $profile).then(loadFiles);
          }
        }}
      >
        <span class="name">{file.name}</span>
        <span class="size">{formatSize(file.size)}</span>
      </li>
    {/each}
  </ul>
</div>

<style>
  .file-browser {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-width: 200px;
  }
  .controls {
    display: flex;
    gap: 4px;
    padding: 4px;
    align-items: center;
  }
  .controls select {
    flex: 1;
    background: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 2px;
  }
  .file-list {
    list-style: none;
    padding: 0;
    margin: 0;
    overflow-y: auto;
    flex: 1;
  }
  .file-list li {
    padding: 4px 8px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }
  .file-list li:hover { background: #333; }
  .file-list li.selected { background: #0066cc; }
  .size { color: #888; font-size: 11px; }
</style>
```

**Step 2: Commit**

```bash
git add client/src/components/FileBrowser.svelte
git commit -m "feat: add file browser component"
```

---

### Task 9: Timeline component

**Files:**
- Create: `client/src/components/Timeline.svelte`

**Step 1: Create canvas-based timeline**

```svelte
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    duration, cursor, playPos, markers, clips, spread, locked, clipSpan
  } from "$lib/stores";

  export let onCursorChange: (time: number) => void = () => {};
  export let onSeek: (time: number) => void = () => {};
  export let onMarkerClick: (marker: { start_time: number; output_path: string }) => void = () => {};
  export let onMarkerDelete: (outputPath: string) => void = () => {};

  let canvas: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D;
  let dragging = false;

  const HEIGHT = 160;

  function timeToX(t: number): number {
    if ($duration <= 0) return 0;
    return (t / $duration) * canvas.width;
  }

  function xToTime(x: number): number {
    if ($duration <= 0) return 0;
    return Math.max(0, Math.min($duration, (x / canvas.width) * $duration));
  }

  function draw() {
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Background
    ctx.fillStyle = "#1a1a1a";
    ctx.fillRect(0, 0, w, h);

    // Clip span region
    if ($duration > 0) {
      const x0 = timeToX($cursor);
      const x1 = timeToX($cursor + $clipSpan);
      ctx.fillStyle = "rgba(0, 100, 200, 0.15)";
      ctx.fillRect(x0, 0, x1 - x0, h);
    }

    // Markers
    for (const m of $markers) {
      const x = timeToX(m.start_time);
      ctx.fillStyle = "#22aa44";
      ctx.fillRect(x - 1, 0, 3, h);
    }

    // Cursor
    if ($duration > 0) {
      const cx = timeToX($cursor);
      ctx.fillStyle = "#ff4444";
      ctx.fillRect(cx - 1, 0, 3, h);
    }

    // Play position
    if ($playPos !== null && $duration > 0) {
      const px = timeToX($playPos);
      ctx.fillStyle = "#ffaa00";
      ctx.fillRect(px - 1, 0, 2, h);
    }

    // Time labels
    if ($duration > 0) {
      ctx.fillStyle = "#888";
      ctx.font = "11px monospace";
      const step = Math.max(10, Math.pow(10, Math.floor(Math.log10($duration / 5))));
      for (let t = 0; t <= $duration; t += step) {
        const x = timeToX(t);
        ctx.fillText(formatTime(t), x + 2, h - 4);
        ctx.fillRect(x, h - 16, 1, 16);
      }
    }
  }

  function formatTime(s: number): string {
    const m = Math.floor(s / 60);
    const sec = (Math.floor(s % 60 * 10) / 10).toFixed(1);
    return `${m}:${sec.padStart(4, "0")}`;
  }

  function handleMouseDown(e: MouseEvent) {
    if ($locked) return;
    dragging = true;
    const time = xToTime(e.offsetX);
    $cursor = time;
    onCursorChange(time);
  }

  function handleMouseMove(e: MouseEvent) {
    if (!dragging || $locked) return;
    const time = xToTime(e.offsetX);
    $cursor = time;
    onCursorChange(time);
  }

  function handleMouseUp() {
    dragging = false;
  }

  function handleDblClick(e: MouseEvent) {
    const time = xToTime(e.offsetX);
    // Check if near a marker
    for (const m of $markers) {
      const mx = timeToX(m.start_time);
      if (Math.abs(e.offsetX - mx) < 8) {
        onMarkerClick(m);
        return;
      }
    }
    onSeek(time);
  }

  function handleContextMenu(e: MouseEvent) {
    const time = xToTime(e.offsetX);
    for (const m of $markers) {
      const mx = timeToX(m.start_time);
      if (Math.abs(e.offsetX - mx) < 8) {
        onMarkerDelete(m.output_path);
        return;
      }
    }
  }

  // Redraw on any state change
  $: if (canvas && ctx) {
    void $duration, $cursor, $playPos, $markers, $clips, $spread, $clipSpan;
    draw();
  }

  onMount(() => {
    ctx = canvas.getContext("2d")!;
    const obs = new ResizeObserver(() => {
      canvas.width = canvas.clientWidth;
      canvas.height = HEIGHT;
      draw();
    });
    obs.observe(canvas);
    return () => obs.disconnect();
  });
</script>

<canvas
  bind:this={canvas}
  style="width:100%;height:{HEIGHT}px"
  on:mousedown={handleMouseDown}
  on:mousemove={handleMouseMove}
  on:mouseup={handleMouseUp}
  on:mouseleave={handleMouseUp}
  on:dblclick={handleDblClick}
  on:contextmenu|preventDefault={handleContextMenu}
/>

<style>
  canvas {
    display: block;
    background: #1a1a1a;
    cursor: crosshair;
  }
</style>
```

**Step 2: Commit**

```bash
git add client/src/components/Timeline.svelte
git commit -m "feat: add canvas-based timeline component"
```

---

### Task 10: Export panel component

**Files:**
- Create: `client/src/components/ExportPanel.svelte`

**Step 1: Create the export controls**

```svelte
<script lang="ts">
  import { startExport } from "$lib/api";
  import {
    currentFile, cursor, clips, spread, shortSide, portraitRatio,
    cropCenter, format, label, category, clipName, profile,
    encoder, hwEncode, trackSubject, randPortrait, randSquare,
    exportStatus, exportCompleted, exportTotal, subprofiles
  } from "$lib/stores";

  const CATEGORIES = ["", "Human", "Animal", "Vehicle", "Tool", "Music", "Nature", "Sport", "Other"];
  const RATIOS = ["Off", "9:16", "4:5", "1:1"];

  async function doExport(folderSuffix: string = "") {
    if (!$currentFile) return;
    $exportStatus = "running";
    $exportCompleted = 0;
    $exportTotal = $clips;

    const req = {
      input_path: `${$currentFile.root}${$currentFile.path}`,
      cursor: $cursor,
      name: $clipName || $currentFile.name.replace(/\.[^.]+$/, ""),
      clips: $clips,
      spread: $spread,
      short_side: $shortSide,
      portrait_ratio: $portraitRatio,
      crop_center: $cropCenter,
      format: $format,
      label: $label,
      category: $category,
      profile: $profile,
      folder_suffix: folderSuffix,
      encoder: $hwEncode ? "h264_nvenc" : "libx264",
    };

    try {
      await startExport(req);
    } catch (e) {
      $exportStatus = "error";
      console.error(e);
    }
  }
</script>

<div class="export-panel">
  <div class="row">
    <button on:click={() => doExport()} disabled={$exportStatus === "running"}>
      Export{#if $exportStatus === "running"} ({$exportCompleted}/{$exportTotal}){/if}
    </button>
    {#each $subprofiles as sub, i}
      <button on:click={() => doExport(sub)} title="Export {sub}">
        {sub}
      </button>
    {/each}
  </div>

  <div class="row">
    <label>Clips <input type="number" bind:value={$clips} min="1" max="99" /></label>
    <label>Spread <input type="number" bind:value={$spread} min="2" max="8" step="0.5" /></label>
    <label>Size <input type="number" bind:value={$shortSide} min="0" max="4320" step="64" /></label>
    <label>Ratio
      <select bind:value={$portraitRatio}>
        {#each RATIOS as r}
          <option value={r === "Off" ? null : r}>{r}</option>
        {/each}
      </select>
    </label>
  </div>

  <div class="row">
    <label>Label <input type="text" bind:value={$label} /></label>
    <label>Category
      <select bind:value={$category}>
        {#each CATEGORIES as c}
          <option value={c}>{c || "—"}</option>
        {/each}
      </select>
    </label>
    <label>Format
      <select bind:value={$format}>
        <option>MP4</option>
        <option>WebP sequence</option>
      </select>
    </label>
    <label><input type="checkbox" bind:checked={$hwEncode} /> GPU</label>
  </div>
</div>

<style>
  .export-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 4px;
    font-size: 12px;
  }
  .row {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  label { display: flex; align-items: center; gap: 2px; }
  input[type="number"] { width: 50px; background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  input[type="text"] { width: 120px; background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  button { background: #0066cc; color: white; border: none; padding: 4px 12px; cursor: pointer; }
  button:disabled { background: #444; }
</style>
```

**Step 2: Commit**

```bash
git add client/src/components/ExportPanel.svelte
git commit -m "feat: add export panel component"
```

---

### Task 11: Profile bar component

**Files:**
- Create: `client/src/components/ProfileBar.svelte`

**Step 1: Create profile bar**

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { getProfiles } from "$lib/api";
  import { profile, subprofiles } from "$lib/stores";

  let profiles: string[] = [];

  onMount(async () => {
    profiles = await getProfiles();
    if (profiles.length && !profiles.includes($profile)) {
      $profile = profiles[0];
    }
  });

  function addSubprofile() {
    const name = prompt("Subprofile suffix:");
    if (name && !$subprofiles.includes(name)) {
      $subprofiles = [...$subprofiles, name];
    }
  }

  function removeSubprofile(name: string) {
    $subprofiles = $subprofiles.filter(s => s !== name);
  }
</script>

<div class="profile-bar">
  <select bind:value={$profile}>
    {#each profiles as p}
      <option value={p}>{p}</option>
    {/each}
  </select>

  <span class="subs">
    {#each $subprofiles as sub}
      <span class="sub-tag" on:contextmenu|preventDefault={() => removeSubprofile(sub)}>
        {sub}
      </span>
    {/each}
    <button on:click={addSubprofile}>+</button>
  </span>
</div>

<style>
  .profile-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px;
    font-size: 12px;
  }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  .subs { display: flex; gap: 4px; align-items: center; }
  .sub-tag {
    background: #444;
    padding: 2px 6px;
    border-radius: 3px;
    cursor: context-menu;
    font-size: 11px;
  }
  button { background: #333; color: #e0e0e0; border: 1px solid #555; padding: 1px 6px; cursor: pointer; }
</style>
```

**Step 2: Commit**

```bash
git add client/src/components/ProfileBar.svelte
git commit -m "feat: add profile bar component"
```

---

### Task 12: Main App layout — wire everything together

**Files:**
- Modify: `client/src/App.svelte`

**Step 1: Compose the main layout**

```svelte
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import FileBrowser from "./components/FileBrowser.svelte";
  import Timeline from "./components/Timeline.svelte";
  import ExportPanel from "./components/ExportPanel.svelte";
  import ProfileBar from "./components/ProfileBar.svelte";
  import { mpvStart, mpvLoad, mpvSeek, mpvPause, mpvResume, mpvSetLoop, mpvClearLoop, mpvTimePos, mpvDuration } from "$lib/mpv";
  import { streamUrl, audioUrl, deleteExport, getMarkers } from "$lib/api";
  import { connectExportWs } from "$lib/ws";
  import {
    currentFile, cursor, duration, playPos, playing, quality,
    clips, spread, locked, markers, profile, clipSpan
  } from "$lib/stores";

  let pollInterval: ReturnType<typeof setInterval>;

  onMount(async () => {
    await mpvStart();
    connectExportWs();

    // Poll mpv for time position
    pollInterval = setInterval(async () => {
      if ($playing) {
        try {
          $playPos = await mpvTimePos();
        } catch { /* mpv not ready */ }
      }
    }, 50);
  });

  onDestroy(() => {
    clearInterval(pollInterval);
  });

  // Load file into mpv when currentFile changes
  $: if ($currentFile) {
    const vUrl = streamUrl($currentFile.path, $currentFile.root, $quality);
    const aUrl = audioUrl($currentFile.path, $currentFile.root);
    mpvLoad(vUrl, aUrl).then(async () => {
      // Wait for mpv to report duration
      await new Promise(r => setTimeout(r, 500));
      try { $duration = await mpvDuration(); } catch {}
    });
  }

  async function handleCursorChange(time: number) {
    await mpvSeek(time);
  }

  async function handlePlay() {
    const a = $cursor;
    const b = $cursor + $clipSpan;
    await mpvSeek(a);
    await mpvSetLoop(a, b);
    await mpvResume();
    $playing = true;
  }

  async function handlePause() {
    await mpvPause();
    await mpvClearLoop();
    $playing = false;
  }

  async function handleMarkerClick(m: { start_time: number; output_path: string }) {
    if ($locked) {
      // Jump cursor to marker end
      const span = 8.0 + ($clips - 1) * $spread;
      $cursor = m.start_time + span;
      await mpvSeek($cursor);
    } else {
      $cursor = m.start_time;
      await mpvSeek(m.start_time);
    }
  }

  async function handleMarkerDelete(outputPath: string) {
    await deleteExport(outputPath);
    if ($currentFile) {
      $markers = await getMarkers($currentFile.name, $profile);
    }
  }
</script>

<main>
  <div class="layout">
    <div class="sidebar">
      <FileBrowser />
    </div>
    <div class="content">
      <ProfileBar />
      <div class="player-area">
        <div class="video-placeholder">
          {#if $currentFile}
            <p>{$currentFile.name}</p>
          {:else}
            <p>Select a file</p>
          {/if}
        </div>
      </div>
      <Timeline
        onCursorChange={handleCursorChange}
        onMarkerClick={handleMarkerClick}
        onMarkerDelete={handleMarkerDelete}
      />
      <div class="transport">
        <button on:click={handlePlay} disabled={!$currentFile}>▶</button>
        <button on:click={handlePause}>⏸</button>
        <button on:click={() => $locked = !$locked}>
          {$locked ? "🔒" : "🔓"}
        </button>
        <span class="time">
          {#if $duration > 0}
            {($cursor / 60).toFixed(0)}:{($cursor % 60).toFixed(1).padStart(4, "0")}
            / {($duration / 60).toFixed(0)}:{($duration % 60).toFixed(1).padStart(4, "0")}
          {/if}
        </span>
        <select bind:value={$quality} style="margin-left:auto">
          <option value="potato">480p</option>
          <option value="low">720p</option>
          <option value="medium">1080p</option>
          <option value="high">Original</option>
        </select>
      </div>
      <ExportPanel />
    </div>
  </div>
</main>

<style>
  main { height: 100vh; overflow: hidden; }
  .layout {
    display: flex;
    height: 100%;
  }
  .sidebar {
    width: 220px;
    border-right: 1px solid #333;
    overflow: hidden;
  }
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .player-area {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    min-height: 200px;
  }
  .video-placeholder {
    color: #666;
    text-align: center;
  }
  .transport {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    background: #222;
  }
  .transport button {
    background: #333;
    color: #e0e0e0;
    border: 1px solid #555;
    padding: 4px 10px;
    cursor: pointer;
  }
  .time {
    font-family: monospace;
    font-size: 13px;
  }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
</style>
```

**Step 2: Verify**

```bash
cd /media/p5/8-cut/client
pnpm tauri dev
```

Expected: Window opens with sidebar file browser, player area, timeline, transport bar, and export panel. Selecting a file triggers mpv load + stream.

**Step 3: Commit**

```bash
git add client/src/App.svelte
git commit -m "feat: wire up main app layout with all components"
```

---

### Task 13: Keyboard shortcuts

**Files:**
- Modify: `client/src/App.svelte`

**Step 1: Add global keydown handler**

Add to the `<script>` in App.svelte:

```typescript
function handleKeydown(e: KeyboardEvent) {
  // Ignore when typing in inputs
  const tag = (e.target as HTMLElement).tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

  switch (e.key) {
    case " ":
      e.preventDefault();
      $playing ? handlePause() : handlePlay();
      break;
    case "e":
    case "E":
      doMainExport();
      break;
    case "ArrowLeft":
      $cursor = Math.max(0, $cursor - 1);
      handleCursorChange($cursor);
      break;
    case "ArrowRight":
      $cursor = Math.min($duration, $cursor + 1);
      handleCursorChange($cursor);
      break;
  }

  // Number keys 1-9 for subprofile export
  const num = parseInt(e.key);
  if (num >= 1 && num <= 9) {
    const idx = num - 1;
    if (idx < $subprofiles.length) {
      doSubprofileExport($subprofiles[idx]);
    }
  }
}
```

Add `<svelte:window on:keydown={handleKeydown} />` to the template.

**Step 2: Commit**

```bash
git add client/src/App.svelte
git commit -m "feat: add keyboard shortcuts"
```

---

### Task 14: Settings persistence

**Files:**
- Create: `client/src/lib/settings.ts`

**Step 1: Create settings save/load using localStorage**

```typescript
import {
  serverUrl, quality, clips, spread, shortSide, portraitRatio,
  format, hwEncode, profile, subprofiles, clipName, exportFolder
} from "./stores";
import { get } from "svelte/store";

const KEY = "8cut-settings";

interface Settings {
  serverUrl: string;
  quality: string;
  clips: number;
  spread: number;
  shortSide: number | null;
  portraitRatio: string | null;
  format: string;
  hwEncode: boolean;
  profile: string;
  subprofiles: string[];
}

export function saveSettings() {
  const data: Settings = {
    serverUrl: get(serverUrl),
    quality: get(quality),
    clips: get(clips),
    spread: get(spread),
    shortSide: get(shortSide),
    portraitRatio: get(portraitRatio),
    format: get(format),
    hwEncode: get(hwEncode),
    profile: get(profile),
    subprofiles: get(subprofiles),
  };
  localStorage.setItem(KEY, JSON.stringify(data));
}

export function loadSettings() {
  const raw = localStorage.getItem(KEY);
  if (!raw) return;
  try {
    const data: Settings = JSON.parse(raw);
    serverUrl.set(data.serverUrl);
    quality.set(data.quality);
    clips.set(data.clips);
    spread.set(data.spread);
    shortSide.set(data.shortSide);
    portraitRatio.set(data.portraitRatio);
    format.set(data.format);
    hwEncode.set(data.hwEncode);
    profile.set(data.profile);
    subprofiles.set(data.subprofiles);
  } catch {}
}
```

Call `loadSettings()` in `App.svelte` `onMount`, and subscribe to stores to auto-save:

```typescript
import { loadSettings, saveSettings } from "$lib/settings";

onMount(() => {
  loadSettings();
  // Auto-save on changes
  const unsubs = [
    quality.subscribe(() => saveSettings()),
    clips.subscribe(() => saveSettings()),
    spread.subscribe(() => saveSettings()),
    profile.subscribe(() => saveSettings()),
    subprofiles.subscribe(() => saveSettings()),
  ];
  return () => unsubs.forEach(u => u());
});
```

**Step 2: Commit**

```bash
git add client/src/lib/settings.ts client/src/App.svelte
git commit -m "feat: add settings persistence via localStorage"
```

---

### Task 15: Package for Linux

**Step 1: Configure tauri.conf.json bundle settings**

In `client/src-tauri/tauri.conf.json`, ensure the bundle section includes:

```json
{
  "bundle": {
    "active": true,
    "targets": ["deb", "appimage"],
    "identifier": "com.8cut.client",
    "icon": []
  }
}
```

**Step 2: Build**

```bash
cd /media/p5/8-cut/client
pnpm tauri build
```

Expected: Produces `.deb` and `.AppImage` in `client/src-tauri/target/release/bundle/`.

**Step 3: Commit**

```bash
git add client/src-tauri/tauri.conf.json
git commit -m "feat: configure Linux packaging (deb + AppImage)"
```

---
