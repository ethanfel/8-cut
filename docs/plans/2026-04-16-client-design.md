# 8-cut Client Design

## Goal

Build a Tauri + Svelte desktop client that connects to the 8-cut server API for remote video editing. Full feature parity with the Qt app. Targets Linux first, then Mac.

## Architecture

```
Tauri app (Rust shell + Svelte webview)
├── mpv sidecar (bundled binary)
│   ├── plays video: http://server/api/stream/{path}?quality=low
│   ├── plays audio: http://server/api/audio/{path}
│   └── controlled via JSON IPC socket
├── Svelte UI
│   ├── File browser
│   ├── Canvas timeline (markers, cursor, play region)
│   ├── Canvas crop overlay
│   ├── Export controls + WebSocket progress
│   └── Settings panel (profile, subprofiles, quality)
└── Rust backend
    ├── Spawn/manage mpv process + IPC
    ├── Proxy server API calls (avoid CORS)
    └── Tauri commands exposed to Svelte frontend
```

## Playback

mpv runs as a sidecar process, controlled via JSON IPC socket. Two streams:
- Video: `http://server/api/stream/{path}?root={root}&quality={quality}` (transcoded, no audio)
- Audio: `http://server/api/audio/{path}?root={root}` (full quality WAV)

mpv's `--audio-file=` flag syncs both streams with frame-accurate seeking.

Quality presets: potato (480p), low (720p), medium (1080p), high (original).

## Features

### File management
- Browse server video roots (`GET /api/roots`, `GET /api/files`)
- Hide/unhide files per profile (`POST/DELETE /api/hidden/{filename}`)
- Sort by name/size, filter hidden

### Playback
- Play/pause/resume from pause point
- AB-loop with current spread/clips settings
- Play region adapts to spread changes without restarting
- Quality selector

### Timeline (Canvas)
- Cursor position, markers, play position indicator
- Click to seek, drag cursor
- Lock mode: cursor locked to marker, double-click jumps to end of clip span
- Autoclip: when paused, auto-adjust clip count to fit pause position

### Crop & keyframes
- Portrait ratio selector (9:16, 4:5, 1:1, off)
- Crop center slider with live canvas overlay
- Crop keyframes at arbitrary timeline positions
- Subject tracking (triggered server-side)
- Random portrait/square toggles

### Export
- Configurable: clips, spread, short side, format (MP4/WebP sequence)
- Label + category annotation
- Encoder selection (libx264 / h264_nvenc)
- Subprofiles with folder suffix routing
- Number keys 1-9 for subprofile quick export, E for main
- WebSocket progress (`WS /ws/export`), per-clip completion
- Delete/re-export from marker context menu

### Profiles
- Profile switcher, markers reload per profile
- Subprofile management (add/remove)

### Settings
- Server URL (configurable)
- Default quality preset
- All settings persisted client-side via Tauri store

## Server API endpoints used

```
GET    /api/roots
GET    /api/files?root={root}
GET    /api/video/{path}?root={root}
GET    /api/stream/{path}?root={root}&quality={quality}
GET    /api/audio/{path}?root={root}
GET    /api/cache/status/{path}?root={root}
GET    /api/markers/{filename}?profile={profile}
GET    /api/profiles
GET    /api/labels
POST   /api/export
GET    /api/export/{job_id}
DELETE /api/export?output_path={path}
POST   /api/hidden/{filename}?profile={profile}
DELETE /api/hidden/{filename}?profile={profile}
GET    /api/hidden?profile={profile}
WS     /ws/export
```

## Project structure

```
client/
├── src-tauri/
│   ├── src/
│   │   ├── main.rs          (Tauri entry, app setup)
│   │   ├── mpv.rs           (mpv sidecar spawn + IPC)
│   │   ├── commands.rs      (Tauri commands for Svelte)
│   │   └── lib.rs
│   ├── Cargo.toml
│   └── tauri.conf.json
├── src/
│   ├── App.svelte
│   ├── lib/
│   │   ├── api.ts           (server API client)
│   │   ├── mpv.ts           (mpv IPC bridge via Tauri commands)
│   │   ├── ws.ts            (WebSocket export progress)
│   │   └── stores.ts        (Svelte stores: files, markers, settings)
│   ├── components/
│   │   ├── FileBrowser.svelte
│   │   ├── Timeline.svelte
│   │   ├── CropOverlay.svelte
│   │   ├── ExportPanel.svelte
│   │   ├── SettingsPanel.svelte
│   │   └── ProfileBar.svelte
│   └── main.ts
├── package.json
└── vite.config.ts
```

## Implementation order

1. Scaffold Tauri + Svelte project
2. mpv sidecar: spawn, IPC, basic play/pause/seek
3. API client module + server connection
4. File browser component
5. Video playback: load file → stream URL → mpv
6. Canvas timeline: cursor, seek, markers
7. Export panel + WebSocket progress
8. Crop overlay + keyframes
9. Lock mode, autoclip, play region
10. Profiles, subprofiles, hidden files
11. Keyboard shortcuts
12. Settings persistence
13. Package for Linux (.deb / .AppImage)
14. Package for Mac (.dmg)
