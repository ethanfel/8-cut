# ComfyUI-8cut Node Pack Design

Date: 2026-04-19

## Goal

Port 8-cut's video scanning, training, review, and export workflow to a ComfyUI node pack. The primary motivation is **remote access** — ComfyUI's web UI allows browser-based operation over the network, and HTML5 `<video>` handles streaming compression natively. No tensor-based image pipeline; videos stay as file paths throughout.

## Architecture

### Approach

Monolithic Review Node + simple pipeline nodes. One central **VideoReview** node embeds the full interactive player/timeline/region table as a large DOM widget. Other nodes (Scan, Train, Export) are headless pipeline nodes that pass lightweight metadata.

### Core reuse

The entire `8-cut/core/` package is Qt-free and reusable as-is:
- `core/audio_scan.py` — `scan_video()`, `train_classifier()`, `load_classifier()`
- `core/db.py` — `ProcessedDB` (SQLite, all scan/training/export persistence)
- `core/ffmpeg.py` — `build_ffmpeg_command()` (clip export)
- `core/tracking.py` — YOLO-based subject tracking
- `core/paths.py` — path helpers, `format_time()`

No porting required — these are imported directly.

---

## Node Pack Structure

```
ComfyUI-8cut/
  __init__.py                    # NODE_CLASS_MAPPINGS, WEB_DIRECTORY
  core/                          # symlink or copy of 8-cut/core/
  data/
    8cut.db                      # separate SQLite DB (can copy from ~/.8cut.db)
  models/                        # trained classifiers (.joblib)
  nodes/
    load_video.py
    audio_scan.py
    video_review.py
    train_model.py
    export_clips.py
  server_routes.py               # custom API routes
  web/
    js/
      video_review.js            # timeline + player + scan panel widget
```

---

## Custom Types

No tensors anywhere in the pipeline. All data flows as lightweight metadata:

| Type | Python value | Purpose |
|------|-------------|---------|
| `VIDEO_PATH` | `str` (absolute path) | Video file reference |
| `SCAN_REGIONS` | `list[dict]` with start/end/score/model/disabled | Scan output / review edits |
| `SCAN_MODEL` | `str` (path to .joblib) | Trained classifier |

---

## Nodes

### LoadVideo

| | |
|---|---|
| **Input** | `video_path` (STRING, file browser), `profile` (STRING combo from DB profiles) |
| **Output** | `VIDEO_PATH`, `filename` (STRING) |
| **Logic** | Validates path exists, returns it. Populates profile combo via API route. |

### AudioScan

| | |
|---|---|
| **Input** | `VIDEO_PATH`, `SCAN_MODEL`, `threshold` (FLOAT 0-1), `hop` (FLOAT) |
| **Output** | `SCAN_REGIONS` |
| **Logic** | Calls `core.audio_scan.scan_video()` directly. Progress via `PromptServer.send_sync("progress", ...)`. |

### VideoReview (interactive, blocking)

| | |
|---|---|
| **Input** | `VIDEO_PATH`, `SCAN_REGIONS` (optional) |
| **Output** | `SCAN_REGIONS` (edited) |
| **OUTPUT_NODE** | `True` |
| **Logic** | Execution pauses here. User interacts via the widget. Clicks "Continue" to pass edited regions downstream. |

The widget layout:

```
+-------------------------------------+
|  [video player (HTML5 <video>)]     |
|  +- timeline with scan regions ----+|
|  |  cursor + region drag/resize    ||
|  +---------------------------------+|
|  +- model tabs [EAT_LARGE][HuBERT]+|
|  | Time   | End    | Score         ||
|  | 1:23   | 1:31   | 0.92          ||
|  | 3:45   | 3:53   | 0.87          ||
|  | [Add Negative] [Export] [Continue]|
|  +---------------------------------+|
+-------------------------------------+
```

Widget size: ~640x500px minimum, resizable via LiteGraph.

**Blocking mechanism**: The node's `run()` method blocks on a server-side event/queue. The frontend signals completion via `POST /8cut/review_done/{node_id}`, which unblocks `run()` and returns the edited `SCAN_REGIONS`.

### TrainModel

| | |
|---|---|
| **Input** | `profile` (STRING combo), `positive_folder` (STRING combo), `negative_folder` (STRING combo, optional), `embed_model` (STRING combo from `_EMBED_MODELS`), `use_hard_negatives` (BOOL) |
| **Output** | `SCAN_MODEL` |
| **Logic** | Queries `db.get_training_data()` to assemble `video_infos`, calls `core.audio_scan.train_classifier()`. Saves to `models/{profile}_{embed_model}.joblib` with version rotation. Progress via ComfyUI progress bar. |

### ExportClips

| | |
|---|---|
| **Input** | `VIDEO_PATH`, `SCAN_REGIONS`, `output_folder` (STRING), `short_side` (INT), `format` (combo MP4/WEBM), `spread` (FLOAT), `clip_count` (INT), `fuse_gap` (FLOAT) |
| **Output** | exported file paths (list) |
| **Logic** | Region fusion via `_build_export_spans()`, then `core.ffmpeg.build_ffmpeg_command()` per clip. Records each clip in DB via `db.add()`. |

### Typical workflow

```
[LoadVideo] --> [AudioScan] --> [VideoReview] --> [ExportClips]
                    ^
              [TrainModel]
```

### Training loop (hard negatives round-trip)

1. Scan with existing model -> regions in VideoReview
2. Review -> mark false positives as negatives (DB)
3. Train -> new model uses hard negatives
4. Rescan -> better results
5. Repeat

---

## API Routes

### Video serving

| Route | Method | Purpose |
|-------|--------|---------|
| `/8cut/video` | GET | Serve raw video file via `web.FileResponse`. Query param: `path`. Browser decodes mp4/h264 natively — key for remote streaming. |
| `/8cut/video_transcode` | GET | Fallback: transcode to webm on-the-fly via ffmpeg `StreamResponse` for browser-incompatible formats (some MKV, odd codecs). |

### Region editing (from VideoReview widget)

| Route | Method | Purpose |
|-------|--------|---------|
| `/8cut/toggle_region` | POST | `toggle_scan_result_disabled()` |
| `/8cut/resize_region` | POST | `update_scan_result()` |
| `/8cut/delete_region` | POST | `delete_scan_result()` |
| `/8cut/add_negatives` | POST | `add_hard_negatives()` |
| `/8cut/scan_versions` | GET | `get_scan_versions()` |
| `/8cut/review_done/{node_id}` | POST | Unblock the VideoReview node's `run()`, pass final regions |

### Data queries (for combo widget population)

| Route | Method | Purpose |
|-------|--------|---------|
| `/8cut/profiles` | GET | `db.get_profiles()` |
| `/8cut/export_folders` | GET | `db.get_export_folders()` |
| `/8cut/models` | GET | List available `.joblib` models |

---

## Frontend JS Widget (`web/js/video_review.js`)

Registered via `app.registerExtension()`. Hooks into the VideoReview node's `onNodeCreated` and `onExecuted` callbacks.

### Components

1. **Video player** — HTML5 `<video>` element, src pointed at `/8cut/video?path=...`
2. **Timeline** — `<canvas>` overlay below the video. Renders:
   - Scan region rectangles (color-coded by score, red for negatives, gray for disabled)
   - Cursor line (click to seek)
   - Drag handles on region edges (resize)
   - Waveform (optional, fetched via separate route)
3. **Region table** — HTML table with model tabs. Click row to seek. Columns: Time, End, Score.
4. **Action buttons** — Add Negative, Export, Continue
5. **Version combo** — dropdown to switch scan history versions

### Interaction flow

- Widget activates when `onExecuted` fires with scan regions
- User clicks/drags timeline, edits regions, marks negatives
- Each edit hits an API route (immediate DB persistence)
- "Continue" sends `POST /8cut/review_done/{node_id}` with final region state
- Node's `run()` unblocks, passes `SCAN_REGIONS` downstream

---

## DB

Separate SQLite DB at `ComfyUI-8cut/data/8cut.db`. Uses the existing `ProcessedDB` class unchanged — same schema, same migration code. Users can copy their existing `~/.8cut.db` to carry over scan history, training data, and hard negatives.

---

## Dependencies

Same as 8-cut's `requirements.txt` minus PyQt6/python-mpv:
- `torch`, `torchaudio`, `torchvision` (from CUDA index)
- `transformers>=4.30,<5.0`, `timm>=0.9`
- `librosa`, `scikit-learn`, `joblib`, `soundfile`, `numpy`
- `ultralytics` (YOLO tracking)

ComfyUI already provides torch. The node pack's install script just needs the audio/ML extras.

---

## Implementation Priority

1. **Node pack skeleton** — structure, `__init__.py`, custom types, API routes for video serving
2. **LoadVideo + AudioScan** — headless nodes, no widget needed yet
3. **VideoReview widget (minimal)** — video player + static region display + Continue button
4. **VideoReview interactivity** — timeline click/drag, region editing, negative marking
5. **TrainModel + ExportClips** — complete the pipeline
6. **Polish** — version history, waveform overlay, transcode fallback
