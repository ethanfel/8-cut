# 8-cut Server API Design

## Goal

Run 8-cut as a FastAPI server on Unraid (Docker) so a Tauri desktop client on Mac can edit remotely over WireGuard — no file transfers, no auth.

## Architecture

```
Unraid (Docker container):
  FastAPI + ffmpeg + SQLite
  ├── /api/files         list videos from mounted volumes
  ├── /api/stream/{path} transcoded video (cached, no audio)
  ├── /api/audio/{path}  full-quality audio (cached, passthrough)
  ├── /api/video/{path}  raw file (for reference/download)
  ├── /api/markers       CRUD markers per profile
  ├── /api/profiles      list/create profiles
  ├── /api/export        trigger + manage exports
  ├── /api/labels        label history
  ├── /api/hidden        hidden file management
  └── ws://…/ws/export   real-time export progress

Mac (Tauri + Svelte + libmpv):
  ├── mpv plays stream URL (video) + audio URL separately
  ├── Canvas timeline + crop overlay + keyframes
  ├── Full UI: profiles, subprofiles, settings
  └── Stateless — all state lives on server
```

## Docker mounts

| Mount       | Purpose                        | Env var      |
|-------------|--------------------------------|--------------|
| `/videos`   | Source video files (read-only)  | `MEDIA_DIRS` |
| `/exports`  | Export output                  | `EXPORT_DIR` |
| `/data`     | SQLite DB + transcode cache    | `DB_PATH`, `CACHE_DIR` |

`MEDIA_DIRS` supports multiple paths: `/videos1,/videos2`.

## Video streaming with transcode cache

The client needs low-bitrate video for scrubbing over the network but full-quality audio for accurate editing.

**Flow:**
1. Client requests `/api/stream/{path}?quality=low`
2. Server checks cache: `{CACHE_DIR}/{quality}/{hash}.mp4`
3. If cached → serve with range requests (instant seeking)
4. If not → start background ffmpeg transcode, return `202 Accepted` with job ID
5. Client polls or gets WebSocket notification when ready
6. Audio: `/api/audio/{path}` extracts audio (passthrough, fast) to cache on first request

**Quality presets:**

| Preset   | Resolution | Bitrate  |
|----------|-----------|----------|
| `potato` | 480p      | ~500 Kbps |
| `low`    | 720p      | ~2 Mbps  |
| `medium` | 1080p     | ~5 Mbps  |
| `high`   | original  | ~10 Mbps |

Each quality level cached separately. Client can switch quality — mpv reloads the URL.

**mpv on client:**
```
video = http://server/api/stream/file.mp4?quality=low
audio = http://server/api/audio/file.mp4
```
mpv's `--audio-file=` flag plays both in sync with frame-accurate seeking.

## API endpoints

### Files
```
GET /api/files?root={root}
  → [{path, name, size, duration?, markers_count}]

GET /api/video/{path}
  → raw file with range requests

GET /api/stream/{path}?quality=low|medium|high|potato
  → cached transcoded video (no audio), range requests
  → 202 if transcode in progress

GET /api/audio/{path}
  → cached full-quality audio, range requests
  → 202 if extraction in progress

GET /api/cache/status/{path}
  → {qualities: {potato: "ready", low: "transcoding", ...}, audio: "ready"}
```

### Markers & profiles
```
GET    /api/markers/{filename}?profile=default
  → [{start_time, marker_number, output_path}]

GET    /api/profiles
  → ["default", "intense", ...]

GET    /api/labels
  → ["dog barking", "rain", ...]
```

### Export
```
POST   /api/export
  body: {input_path, cursor, folder_suffix?, name, clips, spread,
         short_side?, portrait_ratio?, crop_center, format,
         label?, category?, profile, crop_keyframes?,
         rand_portrait?, rand_square?, track_subject?}
  → {job_id}

GET    /api/export/{job_id}
  → {status, completed, total, outputs: [...]}

DELETE /api/export/{output_path}
  → delete from DB + disk

WS     /ws/export
  → server pushes: {type: "clip_done", path: "..."} | {type: "all_done"} | {type: "error", msg: "..."}
```

### Hidden files
```
POST   /api/hidden/{filename}?profile=default
DELETE /api/hidden/{filename}?profile=default
GET    /api/hidden?profile=default
  → ["file1.mp4", "file2.mp4"]
```

## Code reuse from main.py

**Extracted to shared module (used by both server and Qt app):**
- `ProcessedDB` — SQLite operations
- `build_ffmpeg_command` — ffmpeg command construction
- `build_audio_extract_command`
- `build_export_path` / `build_sequence_dir`
- `detect_hw_encoders`
- `upsert_clip_annotation` / `remove_clip_annotation`
- `apply_keyframes_to_jobs` / `resolve_keyframe`
- `track_centers_for_jobs` (subject tracking)

**Server-specific (new):**
- FastAPI app + route handlers
- Transcode cache manager
- Export worker (plain threading, replaces QThread-based ExportWorker)
- File listing / media root scanning
- WebSocket export progress broadcaster

**Tauri client (new, Svelte):**
- mpv integration via Tauri plugin or sidecar
- Canvas-based timeline widget
- Canvas-based crop overlay
- All UI controls
- API client module

## Dockerfile

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/ .
RUN pip install --no-cache-dir fastapi uvicorn
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Project structure

```
8-cut/
├── main.py              (existing Qt app, unchanged)
├── core/                (shared logic, extracted from main.py)
│   ├── __init__.py
│   ├── db.py            (ProcessedDB)
│   ├── ffmpeg.py        (build commands, detect encoders)
│   ├── export.py        (ExportWorker — plain threading)
│   ├── paths.py         (build_export_path, build_sequence_dir)
│   └── annotations.py   (dataset.json helpers)
├── server/
│   ├── app.py           (FastAPI app)
│   ├── routes/
│   │   ├── files.py
│   │   ├── stream.py
│   │   ├── markers.py
│   │   ├── export.py
│   │   └── hidden.py
│   ├── cache.py         (transcode cache manager)
│   ├── ws.py            (WebSocket handler)
│   └── config.py        (env vars, settings)
├── client/              (Tauri + Svelte — future)
│   └── ...
├── Dockerfile
└── docker-compose.yml
```

## Implementation order

1. Extract shared logic from main.py → `core/`
2. Update main.py to import from `core/` (verify Qt app still works)
3. Build FastAPI server with file listing + video serving
4. Add transcode cache + audio extraction
5. Add markers/profiles/labels/hidden API
6. Add export endpoint + WebSocket progress
7. Dockerfile + docker-compose
8. (Later) Tauri client
