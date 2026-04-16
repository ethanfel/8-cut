# Server API Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract shared logic from main.py into a `core/` package, then build the FastAPI server that serves video files, manages the DB, and runs exports.

**Architecture:** Shared logic (DB, ffmpeg, paths, annotations, tracking) moves to `core/`. Both `main.py` (Qt app) and `server/` import from `core/`. The server adds HTTP video streaming with transcode cache, REST endpoints, and WebSocket export progress.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, SQLite, ffmpeg

---

### Task 1: Create core/ package — paths and helpers

**Files:**
- Create: `core/__init__.py`
- Create: `core/paths.py`

**Step 1: Create core/__init__.py**

```python
# empty — package marker
```

**Step 2: Create core/paths.py**

Extract from main.py lines 36-74: `_frozen_path`, `_bin`, `_log`, `build_export_path`, `build_sequence_dir`, `format_time`.

```python
import os
import sys
from datetime import datetime
from pathlib import Path


def _frozen_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _bin(name: str) -> str:
    p = _frozen_path() / name
    if p.exists():
        return str(p)
    return name


def _log(*args) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[8-cut {ts}]", *args, file=sys.stderr)


def build_export_path(folder: str, basename: str, counter: int, sub: int | None = None) -> str:
    group = f"{basename}_{counter:03d}"
    name = f"{group}_{sub}" if sub is not None else group
    return os.path.join(folder, group, name + ".mp4")


def build_sequence_dir(folder: str, basename: str, counter: int, sub: int | None = None) -> str:
    group = f"{basename}_{counter:03d}"
    name = f"{group}_{sub}" if sub is not None else group
    return os.path.join(folder, group, name)


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60 * 10) / 10
    return f"{m}:{s:04.1f}"
```

**Step 3: Commit**

```bash
git add core/
git commit -m "feat: create core/paths module with shared path helpers"
```

---

### Task 2: Create core/ffmpeg.py

**Files:**
- Create: `core/ffmpeg.py`

**Step 1: Create core/ffmpeg.py**

Extract from main.py lines 77-112 and 244-289: `_RATIOS`, `_portrait_crop_filter`, `resolve_keyframe`, `apply_keyframes_to_jobs`, `build_ffmpeg_command`, `build_audio_extract_command`, `detect_hw_encoders`. (Lines 115-188 are also ffmpeg-related. Lines 191-241 are annotations — extracted separately in Task 4.)

```python
import os
import re
import subprocess

from .paths import _bin, _log


_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "4:5":  (4, 5),
    "1:1":  (1, 1),
}


def _portrait_crop_filter(ratio: str, crop_center: float) -> str:
    num, den = _RATIOS[ratio]
    cw = f"ih*{num}/{den}"
    x = f"max(0\\,min((iw-{cw})*{crop_center}\\,iw-{cw}))"
    return f"crop={cw}:ih:{x}:0"


def resolve_keyframe(
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    t: float,
    tolerance: float = 0.05,
) -> tuple[float, float, str | None, bool, bool] | None:
    result = None
    for kf in keyframes:
        if kf[0] <= t + tolerance:
            result = kf
        else:
            break
    return result


def apply_keyframes_to_jobs(
    jobs: list[tuple[float, str, str | None, float]],
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    base_center: float,
    base_ratio: str | None,
    base_rand_p: bool,
    base_rand_s: bool,
) -> list[tuple[float, str, str | None, float, bool, bool]]:
    result = []
    for s, o, _r, _c in jobs:
        kf = resolve_keyframe(keyframes, s)
        if kf is not None:
            _, center, ratio, rp, rs = kf
        else:
            center, ratio, rp, rs = base_center, base_ratio, base_rand_p, base_rand_s
        result.append((s, o, ratio, center, rp, rs))
    return result


def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
    portrait_ratio: str | None = None,
    crop_center: float = 0.5,
    image_sequence: bool = False,
    encoder: str = "libx264",
) -> list[str]:
    use_hw_vaapi = encoder == "h264_vaapi" and not image_sequence
    cmd = [_bin("ffmpeg"), "-y"]
    if use_hw_vaapi:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                "-vaapi_device", "/dev/dri/renderD128"]
    cmd += ["-threads", "0", "-ss", str(start), "-i", input_path, "-t", "8"]
    filters: list[str] = []
    if portrait_ratio is not None:
        filters.append(_portrait_crop_filter(portrait_ratio, crop_center))
    if short_side is not None:
        filters.append(
            f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})':flags=lanczos"
        )
    if use_hw_vaapi:
        if filters:
            filters.insert(0, "hwdownload")
            filters.insert(1, "format=nv12")
        filters.append("format=nv12")
        filters.append("hwupload")
    if filters:
        cmd += ["-vf", ",".join(filters)]
    if image_sequence:
        cmd += ["-an", "-c:v", "libwebp", "-quality", "92", "-compression_level", "1",
                os.path.join(output_path, "frame_%04d.webp")]
    else:
        cmd += ["-c:v", encoder, "-c:a", "pcm_s16le", output_path]
    return cmd


def build_audio_extract_command(input_path: str, start: float, sequence_dir: str) -> list[str]:
    audio_path = sequence_dir + ".wav"
    return [_bin("ffmpeg"), "-y", "-ss", str(start), "-i", input_path,
            "-t", "8", "-vn", "-c:a", "pcm_s16le", audio_path]


def detect_hw_encoders() -> list[str]:
    _HW_ENCODERS = ["h264_nvenc", "h264_vaapi", "h264_qsv", "h264_amf", "h264_videotoolbox"]
    try:
        result = subprocess.run(
            [_bin("ffmpeg"), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        output = result.stdout
    except Exception:
        return []
    available = []
    for enc in _HW_ENCODERS:
        if re.search(rf'\b{enc}\b', output):
            available.append(enc)
    if available:
        _log(f"HW encoders detected: {', '.join(available)}")
    else:
        _log("No HW encoders detected — GPU export unavailable")
    return available
```

**Step 2: Commit**

```bash
git add core/ffmpeg.py
git commit -m "feat: create core/ffmpeg module with ffmpeg helpers"
```

---

### Task 3: Create core/db.py

**Files:**
- Create: `core/db.py`

**Step 1: Create core/db.py**

Extract the entire `ProcessedDB` class from main.py lines 398-626. Import `_log` from `core.paths`.

```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .paths import _log


class ProcessedDB:
    _SCHEMA_VERSION = 3

    def __init__(self, db_path: str | None = None):
        # ... exact copy of existing class ...
```

Copy the full class body verbatim — all methods unchanged.

**Step 2: Commit**

```bash
git add core/db.py
git commit -m "feat: create core/db module with ProcessedDB"
```

---

### Task 4: Create core/annotations.py

**Files:**
- Create: `core/annotations.py`

**Step 1: Create core/annotations.py**

Extract from main.py lines 191-241: `build_annotation_json_path`, `remove_clip_annotation`, `upsert_clip_annotation`.

```python
import json
import os


def build_annotation_json_path(folder: str) -> str:
    return os.path.join(folder, "dataset.json")


def remove_clip_annotation(folder: str, clip_path: str) -> None:
    json_path = build_annotation_json_path(folder)
    if not os.path.exists(json_path):
        return
    abs_path = os.path.abspath(clip_path)
    with open(json_path, "r", encoding="utf-8") as f:
        try:
            entries = json.load(f)
        except (json.JSONDecodeError, ValueError):
            return
    entries = [e for e in entries if e.get("path") != abs_path]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upsert_clip_annotation(folder: str, clip_path: str, label: str) -> None:
    if not label.strip():
        return
    os.makedirs(folder, exist_ok=True)
    json_path = build_annotation_json_path(folder)
    entries: list[dict] = []
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except (json.JSONDecodeError, ValueError):
                entries = []
    abs_path = os.path.abspath(clip_path)
    entry: dict = {"path": abs_path, "label": label}
    for i, e in enumerate(entries):
        if e.get("path") == abs_path:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")
```

**Step 2: Commit**

```bash
git add core/annotations.py
git commit -m "feat: create core/annotations module"
```

---

### Task 5: Create core/export.py

**Files:**
- Create: `core/export.py`

**Step 1: Create core/export.py**

A plain-threading version of `ExportWorker` (no QThread dependency). Used by the server. The Qt app continues using its own QThread-based worker.

```python
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .ffmpeg import build_ffmpeg_command, build_audio_extract_command
from .paths import _bin, _log


class ExportRunner:
    """Run ffmpeg export jobs in a background thread pool.

    Callbacks:
        on_clip_done(path: str)
        on_all_done()
        on_error(msg: str)
    """

    def __init__(
        self,
        input_path: str,
        jobs: list[tuple[float, str, str | None, float]],
        short_side: int | None = None,
        image_sequence: bool = False,
        max_workers: int | None = None,
        encoder: str = "libx264",
        on_clip_done: Callable[[str], None] | None = None,
        on_all_done: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._input = input_path
        self._jobs = jobs
        self._short_side = short_side
        self._image_sequence = image_sequence
        self._max_workers = max_workers
        self._encoder = encoder
        self._on_clip_done = on_clip_done
        self._on_all_done = on_all_done
        self._on_error = on_error
        self._cancel = False
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel = True
        with self._procs_lock:
            for proc in self._procs:
                try:
                    proc.kill()
                except OSError:
                    pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_one(self, start: float, output: str,
                 portrait_ratio: str | None, crop_center: float) -> str:
        if self._cancel:
            raise RuntimeError("cancelled")
        if self._image_sequence:
            os.makedirs(output, exist_ok=True)
        cmd = build_ffmpeg_command(
            self._input, start, output,
            short_side=self._short_side,
            portrait_ratio=portrait_ratio,
            crop_center=crop_center,
            image_sequence=self._image_sequence,
            encoder=self._encoder,
        )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._procs_lock:
            self._procs.append(proc)
        try:
            _, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("ffmpeg timed out")
        finally:
            with self._procs_lock:
                self._procs.remove(proc)
        if self._cancel:
            raise RuntimeError("cancelled")
        if proc.returncode != 0:
            msg = stderr.decode(errors='replace')[-500:] if stderr else "ffmpeg failed"
            raise RuntimeError(msg)
        if self._image_sequence:
            audio_cmd = build_audio_extract_command(self._input, start, output)
            subprocess.run(audio_cmd, capture_output=True, text=True, timeout=60)
        return output

    def _run(self):
        cap = self._max_workers or (os.cpu_count() or 2)
        workers = min(len(self._jobs), cap)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._run_one, s, o, pr, cc): o
                    for s, o, pr, cc in self._jobs
                }
                for fut in as_completed(futures):
                    if self._cancel:
                        break
                    try:
                        path = fut.result()
                        if self._on_clip_done:
                            self._on_clip_done(path)
                    except Exception as e:
                        if "cancelled" not in str(e) and self._on_error:
                            self._on_error(str(e))
        except Exception as e:
            if self._on_error:
                self._on_error(str(e))
            return
        if self._cancel:
            return
        if self._on_all_done:
            self._on_all_done()
```

**Step 2: Commit**

```bash
git add core/export.py
git commit -m "feat: create core/export module with ExportRunner"
```

---

### Task 6: Create core/tracking.py

**Files:**
- Create: `core/tracking.py`

**Step 1: Create core/tracking.py**

Extract from main.py lines 294-395: YOLO tracking functions.

```python
import os
import subprocess
import tempfile

from .paths import _bin, _log

_yolo_model = None


def _get_yolo():
    global _yolo_model
    if _yolo_model is None:
        try:
            from ultralytics import YOLO
            _yolo_model = YOLO("yolov8n.pt")
            _log("YOLO model loaded")
        except ImportError:
            _log("ultralytics not installed — tracking disabled")
            return None
        except Exception as e:
            _log(f"YOLO load failed: {e}")
            return None
    return _yolo_model


def extract_frame_cv(video_path: str, time: float):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = [_bin("ffmpeg"), "-y", "-ss", str(time), "-i", video_path,
               "-frames:v", "1", tmp]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            return None
        return cv2.imread(tmp)
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def detect_subject_center(
    video_path: str, time: float, target_cls: int | None, last_x: float, last_y: float,
) -> tuple[int | None, float, float] | None:
    model = _get_yolo()
    if model is None:
        return None
    frame = extract_frame_cv(video_path, time)
    if frame is None:
        return None
    results = model(frame, verbose=False)
    if not results or len(results[0].boxes) == 0:
        return None
    h, w = frame.shape[:2]
    dets = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cls = int(box.cls[0])
        cx = (x1 + x2) / 2 / w
        cy = (y1 + y2) / 2 / h
        dets.append((cls, cx, cy))
    def score(d):
        cls_penalty = 0 if (target_cls is None or d[0] == target_cls) else 1.0
        dist = (d[1] - last_x) ** 2 + (d[2] - last_y) ** 2
        return cls_penalty + dist
    best = min(dets, key=score)
    return best


def track_centers_for_jobs(
    video_path: str, cursor: float, crop_center: float,
    starts: list[float],
) -> list[float]:
    ref = detect_subject_center(video_path, cursor, None, crop_center, 0.5)
    if ref is None:
        _log("Tracking: no detection at cursor, using fixed center")
        return [crop_center] * len(starts)
    target_cls, last_x, last_y = ref
    _log(f"Tracking: target class={target_cls} at ({last_x:.2f}, {last_y:.2f})")
    centers = []
    for t in starts:
        det = detect_subject_center(video_path, t, target_cls, last_x, last_y)
        if det is not None:
            _, cx, cy = det
            _log(f"  t={t:.2f}s → center={cx:.3f}")
            centers.append(cx)
            last_x, last_y = cx, cy
        else:
            _log(f"  t={t:.2f}s → lost, reusing {last_x:.3f}")
            centers.append(last_x)
    return centers
```

**Step 2: Commit**

```bash
git add core/tracking.py
git commit -m "feat: create core/tracking module with YOLO subject tracking"
```

---

### Task 7: Update main.py to import from core/

**Files:**
- Modify: `main.py`

**Step 1: Replace function definitions with imports**

At the top of main.py, after the existing stdlib imports (line 17), add:

```python
from core.paths import _bin, _log, build_export_path, build_sequence_dir, format_time
from core.ffmpeg import (
    _RATIOS, resolve_keyframe, apply_keyframes_to_jobs,
    build_ffmpeg_command, build_audio_extract_command, detect_hw_encoders,
)
from core.db import ProcessedDB
from core.annotations import remove_clip_annotation, upsert_clip_annotation
from core.tracking import track_centers_for_jobs
```

**Step 2: Delete the extracted function definitions and dead imports**

Remove definitions from main.py:
- Lines 36-74: `_frozen_path`, `_bin`, `_log`, `build_export_path`, `build_sequence_dir`, `format_time`
- Lines 77-188: `resolve_keyframe`, `apply_keyframes_to_jobs`, `build_ffmpeg_command`, `build_audio_extract_command`
- Lines 191-241: annotation functions (`build_annotation_json_path`, `remove_clip_annotation`, `upsert_clip_annotation`)
- Lines 244-289: `detect_hw_encoders`, `_RATIOS`, `_portrait_crop_filter`
- Lines 294-395: tracking functions (`_yolo_model`, `_get_yolo`, `extract_frame_cv`, `detect_subject_center`, `track_centers_for_jobs`)
- Lines 398-626: `ProcessedDB` class

Remove now-dead stdlib imports from the top of main.py:
- `re` (only used in `detect_hw_encoders`)
- `json` (only used in annotation functions)
- `sqlite3` (only used in `ProcessedDB`)
- `tempfile` (only used in `extract_frame_cv`)
- `datetime`, `timezone` from the datetime import (only used in `_log` and `ProcessedDB`)

Keep in main.py:
- `_SELVA_CATEGORIES` (UI constant, line 291)
- `_RATIOS` reference — imported from core.ffmpeg
- `ExportWorker` (QThread-based, stays in main.py — the server uses `core.export.ExportRunner` instead)
- `_DBWorker` and `FrameGrabber` (QThread-based, stay in main.py)

**Step 3: Verify Qt app still works**

```bash
python main.py
```

Open a video, export a clip, check markers — verify nothing broke.

**Step 4: Commit**

```bash
git add main.py
git commit -m "refactor: import shared logic from core/ instead of inline definitions"
```

---

### Task 8: Create server/config.py

**Files:**
- Create: `server/__init__.py` (empty package marker)
- Create: `server/config.py`

**Step 1: Create `server/__init__.py`**

```python
# empty — package marker
```

**Step 2: Create config**

```python
import os
from pathlib import Path


MEDIA_DIRS: list[str] = [
    d.strip() for d in os.environ.get("MEDIA_DIRS", str(Path.home())).split(",") if d.strip()
]
EXPORT_DIR: str = os.environ.get("EXPORT_DIR", str(Path.home() / "8cut-exports"))
DB_PATH: str = os.environ.get("DB_PATH", str(Path.home() / ".8cut.db"))
CACHE_DIR: str = os.environ.get("CACHE_DIR", str(Path.home() / ".8cut-cache"))
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv"}

QUALITY_PRESETS = {
    "potato": {"height": 480, "bitrate": "500k"},
    "low":    {"height": 720, "bitrate": "2M"},
    "medium": {"height": 1080, "bitrate": "5M"},
    "high":   {"height": 0, "bitrate": "10M"},  # 0 = original resolution
}
```

**Step 2: Commit**

```bash
git add server/
git commit -m "feat: create server/config with env var settings and quality presets"
```

---

### Task 9: Create server/app.py — FastAPI skeleton + file listing

**Files:**
- Create: `server/app.py`
- Create: `server/routes/__init__.py`
- Create: `server/routes/files.py`

**Step 1: Create FastAPI app**

`server/app.py`:
```python
from fastapi import FastAPI
from .routes import files, stream, markers, export, hidden

app = FastAPI(title="8-cut Server")
app.include_router(files.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(markers.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(hidden.router, prefix="/api")
```

**Step 2: Create file listing route**

`server/routes/files.py`:
```python
import os
from fastapi import APIRouter, Query
from ..config import MEDIA_DIRS, VIDEO_EXTENSIONS

router = APIRouter()


def _scan_videos(root: str) -> list[dict]:
    results = []
    for dirpath, _, filenames in os.walk(root):
        for f in sorted(filenames):
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                results.append({
                    "name": f,
                    "path": rel,
                    "root": root,
                    "size": os.path.getsize(full),
                })
    return results


@router.get("/files")
def list_files(root: str | None = Query(None)):
    dirs = [root] if root and root in MEDIA_DIRS else MEDIA_DIRS
    files = []
    for d in dirs:
        files.extend(_scan_videos(d))
    return files


@router.get("/roots")
def list_roots():
    return MEDIA_DIRS
```

**Step 3: Create `server/routes/__init__.py`**

```python
# empty — package marker
```

**Step 4: Create stub routers** so app.py imports don't fail. Each file gets a minimal router — later tasks fill in the real endpoints.

`server/routes/stream.py`:
```python
from fastapi import APIRouter
router = APIRouter()
```

`server/routes/markers.py`:
```python
from fastapi import APIRouter
router = APIRouter()
```

`server/routes/export.py`:
```python
from fastapi import APIRouter
router = APIRouter()
```

`server/routes/hidden.py`:
```python
from fastapi import APIRouter
router = APIRouter()
```

**Step 5: Commit**

```bash
git add server/
git commit -m "feat: add FastAPI app with file listing endpoint"
```

---

### Task 10: Create server/routes/stream.py — video serving + transcode cache

**Files:**
- Create: `server/cache.py`
- Create: `server/routes/stream.py`

**Step 1: Create cache manager**

`server/cache.py` handles:
- Computing cache paths from source file hash + quality
- Checking cache status
- Launching background ffmpeg transcodes
- Tracking in-progress jobs

**Step 2: Create stream routes**

```
GET /api/video/{path}  — raw file, range requests
GET /api/stream/{path}?quality=low — cached transcode, range requests (202 if not ready)
GET /api/audio/{path}  — cached audio extraction, range requests (202 if not ready)
GET /api/cache/status/{path} — cache status for all qualities
```

**Step 3: Commit**

```bash
git add server/cache.py server/routes/stream.py
git commit -m "feat: add video streaming with transcode cache and audio extraction"
```

---

### Task 11: Create server/routes/markers.py — DB endpoints

**Files:**
- Create: `server/routes/markers.py`

**Step 1: Create markers/profiles/labels routes**

```
GET  /api/markers/{filename}?profile=default
GET  /api/profiles
GET  /api/labels
```

Uses `ProcessedDB` singleton from `core.db`.

**Step 2: Commit**

```bash
git add server/routes/markers.py
git commit -m "feat: add markers, profiles, and labels API endpoints"
```

---

### Task 12: Create server/routes/export.py + WebSocket

**Files:**
- Create: `server/routes/export.py`
- Create: `server/ws.py`

**Step 1: Create export routes + WS**

```
POST   /api/export        — start export job
GET    /api/export/{id}   — check job status
DELETE /api/export/{path} — delete export from DB + disk
WS     /ws/export         — real-time progress
```

Uses `ExportRunner` from `core.export`.

**Step 2: Commit**

```bash
git add server/routes/export.py server/ws.py
git commit -m "feat: add export endpoint with WebSocket progress"
```

---

### Task 13: Create server/routes/hidden.py

**Files:**
- Create: `server/routes/hidden.py`

**Step 1: Create hidden file routes**

```
POST   /api/hidden/{filename}?profile=default
DELETE /api/hidden/{filename}?profile=default
GET    /api/hidden?profile=default
```

**Step 2: Commit**

```bash
git add server/routes/hidden.py
git commit -m "feat: add hidden files API endpoints"
```

---

### Task 14: Create Dockerfile + docker-compose.yml

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

**Step 1: Create Dockerfile**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY core/ core/
COPY server/ server/
# Note: ultralytics + opencv-python needed only if subject tracking is used.
# Add them here if tracking is required on the server.
RUN pip install --no-cache-dir fastapi uvicorn
EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 2: Create docker-compose.yml**

```yaml
services:
  8cut:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - /path/to/videos:/videos:ro
      - /path/to/exports:/exports
      - 8cut-data:/data
    environment:
      MEDIA_DIRS: /videos
      EXPORT_DIR: /exports
      DB_PATH: /data/8cut.db
      CACHE_DIR: /data/cache

volumes:
  8cut-data:
```

**Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "feat: add Dockerfile and docker-compose for server deployment"
```
