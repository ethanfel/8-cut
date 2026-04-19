# ComfyUI-8cut Node Pack Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a ComfyUI node pack that ports 8-cut's video scan/review/train/export workflow to a browser-based interface, using file paths instead of image tensors.

**Architecture:** 5 nodes (LoadVideo, AudioScan, VideoReview, TrainModel, ExportClips) passing custom types (`VIDEO_PATH`, `SCAN_REGIONS`, `SCAN_MODEL`). The interactive VideoReview node uses ComfyUI's `ExecutionBlocker` for a two-pass flow: first pass sends data to the frontend widget, second pass (after user clicks Continue) passes edited regions downstream. The `8-cut/core/` package is reused unchanged.

**Tech Stack:** ComfyUI (LiteGraph.js frontend, aiohttp server), Python 3.12, HTML5 `<video>`, Canvas API for timeline, existing 8-cut core (torch, librosa, scikit-learn, ffmpeg)

**Design doc:** `docs/plans/2026-04-19-comfyui-node-pack-design.md`

---

### Task 1: Node pack skeleton and video serving

**Files:**
- Create: `ComfyUI-8cut/__init__.py`
- Create: `ComfyUI-8cut/nodes/__init__.py`
- Create: `ComfyUI-8cut/nodes/load_video.py`
- Create: `ComfyUI-8cut/server_routes.py`
- Symlink: `ComfyUI-8cut/core/` → `8-cut/core/`

**Step 1: Create directory structure**

```bash
mkdir -p ComfyUI-8cut/nodes ComfyUI-8cut/data ComfyUI-8cut/models ComfyUI-8cut/web/js
```

**Step 2: Symlink core package**

```bash
ln -s /media/p5/8-cut/core ComfyUI-8cut/core
```

**Step 3: Create `server_routes.py` — video serving API**

```python
"""Custom API routes for ComfyUI-8cut."""

import os
import json
from aiohttp import web

import server as comfy_server

routes = comfy_server.PromptServer.instance.routes


@routes.get("/8cut/video")
async def serve_video(request):
    """Serve a video file for HTML5 <video> playback."""
    path = request.rel_url.query.get("path", "")
    if not path or not os.path.isfile(path):
        return web.Response(status=404, text="File not found")
    return web.FileResponse(path=path)
```

**Step 4: Create `nodes/load_video.py`**

```python
"""LoadVideo node — validates a video path and passes it downstream."""

import os


class LoadVideo:
    """Load a video file by path for the 8-cut pipeline."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("VIDEO_PATH", "STRING")
    RETURN_NAMES = ("video", "filename")
    FUNCTION = "run"
    CATEGORY = "8cut"

    def run(self, video_path):
        if not video_path or not os.path.isfile(video_path):
            raise ValueError(f"Video not found: {video_path}")
        filename = os.path.basename(video_path)
        return (video_path, filename)
```

**Step 5: Create `nodes/__init__.py`**

```python
from .load_video import LoadVideo

NODE_CLASS_MAPPINGS = {
    "8cut_LoadVideo": LoadVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "8cut_LoadVideo": "Load Video (8cut)",
}
```

**Step 6: Create top-level `__init__.py`**

```python
"""ComfyUI-8cut — tensor-free video scanning workflow."""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# Import server routes (registers API endpoints as side effect)
from . import server_routes  # noqa: F401

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
```

**Step 7: Test manually**

Install the node pack by symlinking into ComfyUI's `custom_nodes/`:

```bash
ln -s /path/to/ComfyUI-8cut /path/to/ComfyUI/custom_nodes/ComfyUI-8cut
```

Start ComfyUI, verify:
- "Load Video (8cut)" appears in the node menu under "8cut" category
- Node accepts a video path string and outputs VIDEO_PATH + filename
- `GET /8cut/video?path=/path/to/video.mp4` serves the file

**Step 8: Commit**

```bash
git add ComfyUI-8cut/
git commit -m "feat: ComfyUI-8cut node pack skeleton with LoadVideo and video serving"
```

---

### Task 2: AudioScan node

**Files:**
- Create: `ComfyUI-8cut/nodes/audio_scan.py`
- Modify: `ComfyUI-8cut/nodes/__init__.py`

**Step 1: Create `nodes/audio_scan.py`**

```python
"""AudioScan node — scan a video for target audio events."""

import os
import sys

# Ensure core package is importable
_pack_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pack_dir not in sys.path:
    sys.path.insert(0, _pack_dir)

from core.audio_scan import scan_video, load_classifier, _EMBED_MODELS

import server as comfy_server


class AudioScan:
    """Scan a video using a trained audio classifier."""

    @classmethod
    def INPUT_TYPES(cls):
        models_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models",
        )
        model_files = []
        if os.path.isdir(models_dir):
            model_files = sorted(
                f for f in os.listdir(models_dir) if f.endswith(".joblib")
            )
        return {
            "required": {
                "video": ("VIDEO_PATH",),
                "model_file": (model_files if model_files else ["(none)"],),
                "threshold": ("FLOAT", {
                    "default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01,
                }),
                "hop": ("FLOAT", {
                    "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.5,
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("SCAN_REGIONS",)
    RETURN_NAMES = ("regions",)
    FUNCTION = "run"
    CATEGORY = "8cut"

    def run(self, video, model_file, threshold, hop, unique_id=None):
        if model_file == "(none)":
            raise ValueError("No model selected. Train a model first.")

        models_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models",
        )
        model_path = os.path.join(models_dir, model_file)
        model = load_classifier(model_path)

        # Progress callback
        prompt_server = comfy_server.PromptServer.instance

        def progress_cb(current, total):
            if unique_id is not None:
                prompt_server.send_sync("progress", {
                    "value": current, "max": total,
                    "node": unique_id,
                })

        regions = scan_video(
            video, model, threshold=threshold, hop=hop,
            progress_cb=progress_cb,
        )

        # Convert to list of dicts for SCAN_REGIONS type
        result = [
            {"start": s, "end": e, "score": sc, "disabled": False}
            for s, e, sc in regions
        ]

        embed_model = model.get("embed_model", "UNKNOWN")
        return ({"model": embed_model, "regions": result},)
```

**Step 2: Register in `nodes/__init__.py`**

Add to imports and mappings:

```python
from .load_video import LoadVideo
from .audio_scan import AudioScan

NODE_CLASS_MAPPINGS = {
    "8cut_LoadVideo": LoadVideo,
    "8cut_AudioScan": AudioScan,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "8cut_LoadVideo": "Load Video (8cut)",
    "8cut_AudioScan": "Audio Scan (8cut)",
}
```

**Step 3: Test manually**

- Connect LoadVideo → AudioScan
- Copy a trained `.joblib` model to `ComfyUI-8cut/models/`
- Run the workflow — verify scan completes, progress bar shows, regions output is populated

**Step 4: Commit**

```bash
git add ComfyUI-8cut/nodes/audio_scan.py ComfyUI-8cut/nodes/__init__.py
git commit -m "feat: AudioScan node wrapping core.audio_scan.scan_video"
```

---

### Task 3: VideoReview node — Python side (ExecutionBlocker pattern)

**Files:**
- Create: `ComfyUI-8cut/nodes/video_review.py`
- Modify: `ComfyUI-8cut/nodes/__init__.py`
- Modify: `ComfyUI-8cut/server_routes.py`

**Step 1: Add review routes to `server_routes.py`**

```python
import asyncio

# In-memory store for review completion signals
_review_events: dict[str, asyncio.Event] = {}
_review_results: dict[str, dict] = {}


@routes.post("/8cut/review_done/{node_id}")
async def review_done(request):
    """Frontend signals that the user finished reviewing."""
    node_id = request.match_info["node_id"]
    data = await request.json()
    _review_results[node_id] = data
    event = _review_events.get(node_id)
    if event:
        event.set()
    return web.json_response({"ok": True})


@routes.get("/8cut/scan_versions")
async def scan_versions(request):
    """Return scan version history for a file/profile/model."""
    from core.db import ProcessedDB
    db_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "8cut.db"
    )
    db = ProcessedDB(db_path)
    filename = request.rel_url.query.get("filename", "")
    profile = request.rel_url.query.get("profile", "default")
    model = request.rel_url.query.get("model", "")
    versions = db.get_scan_versions(filename, profile, model)
    return web.json_response(versions)


@routes.post("/8cut/toggle_region")
async def toggle_region(request):
    """Toggle disabled state of a scan result row."""
    from core.db import ProcessedDB
    db_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "8cut.db"
    )
    db = ProcessedDB(db_path)
    data = await request.json()
    db.toggle_scan_result_disabled(data["row_id"], data["disabled"])
    return web.json_response({"ok": True})


@routes.post("/8cut/add_negatives")
async def add_negatives(request):
    """Mark timestamps as hard negatives."""
    from core.db import ProcessedDB
    db_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "8cut.db"
    )
    db = ProcessedDB(db_path)
    data = await request.json()
    db.add_hard_negatives(
        data["filename"], data["profile"], data["times"],
        source_path=data.get("source_path", ""),
        source_model=data.get("source_model", ""),
    )
    return web.json_response({"ok": True})
```

**Step 2: Create `nodes/video_review.py`**

```python
"""VideoReview node — interactive video review with ExecutionBlocker pattern."""

import json
import server as comfy_server
from comfy_execution.graph_utils import ExecutionBlocker


class VideoReview:
    """Interactive video review — pauses execution for user interaction.

    First pass: displays video + scan regions in the widget, blocks downstream.
    Second pass: after user clicks Continue, passes edited regions downstream.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO_PATH",),
            },
            "optional": {
                "regions": ("SCAN_REGIONS",),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "confirmed": ("BOOLEAN", {"default": False}),
                "edited_regions": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("SCAN_REGIONS",)
    RETURN_NAMES = ("regions",)
    OUTPUT_NODE = True
    FUNCTION = "run"
    CATEGORY = "8cut"

    def run(self, video, regions=None, unique_id=None,
            confirmed=False, edited_regions=""):
        if confirmed and edited_regions:
            # Second pass — user clicked Continue, pass data downstream
            return (json.loads(edited_regions),)

        # First pass — send data to frontend widget, block downstream
        ui_data = {
            "video_path": video,
            "regions": regions if regions else {"model": "", "regions": []},
            "node_id": unique_id or "",
        }
        return {
            "ui": ui_data,
            "result": (ExecutionBlocker("Waiting for review..."),),
        }
```

**Step 3: Register in `nodes/__init__.py`**

Add VideoReview to imports and both mappings dicts.

**Step 4: Test manually**

- Connect LoadVideo → AudioScan → VideoReview
- Run workflow — verify AudioScan completes, VideoReview shows "Waiting for review..." status (no widget yet)
- Verify no server freeze — other ComfyUI operations should still work

**Step 5: Commit**

```bash
git add ComfyUI-8cut/
git commit -m "feat: VideoReview node with ExecutionBlocker two-pass pattern"
```

---

### Task 4: VideoReview frontend — video player and static region display

**Files:**
- Create: `ComfyUI-8cut/web/js/video_review.js`

This is the core frontend work. Start with playback + read-only region display.

**Step 1: Create `web/js/video_review.js` — extension registration and video player**

```javascript
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

function chainCallback(object, property, callback) {
    if (object[property]) {
        const original = object[property];
        object[property] = function () {
            const r = original.apply(this, arguments);
            callback.apply(this, arguments);
            return r;
        };
    } else {
        object[property] = callback;
    }
}

app.registerExtension({
    name: "8cut.VideoReview",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData?.name !== "8cut_VideoReview") return;

        // On node creation — add DOM widget with video + timeline + table
        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            const container = document.createElement("div");
            container.style.cssText = "width:100%;min-height:480px;background:#1a1a1a;display:flex;flex-direction:column;";

            // Video player
            const video = document.createElement("video");
            video.controls = true;
            video.loop = true;
            video.style.cssText = "width:100%;max-height:300px;background:#000;";
            container.appendChild(video);

            // Timeline canvas
            const timeline = document.createElement("canvas");
            timeline.height = 60;
            timeline.style.cssText = "width:100%;height:60px;cursor:crosshair;background:#202020;";
            container.appendChild(timeline);

            // Region table container
            const tableWrap = document.createElement("div");
            tableWrap.style.cssText = "flex:1;overflow-y:auto;max-height:200px;";
            const table = document.createElement("table");
            table.style.cssText = "width:100%;border-collapse:collapse;color:#ccc;font-size:12px;";
            table.innerHTML = "<thead><tr><th>Time</th><th>End</th><th>Score</th></tr></thead><tbody></tbody>";
            tableWrap.appendChild(table);
            container.appendChild(tableWrap);

            // Continue button
            const btnRow = document.createElement("div");
            btnRow.style.cssText = "display:flex;gap:8px;padding:4px;";
            const btnContinue = document.createElement("button");
            btnContinue.textContent = "Continue ▶";
            btnContinue.style.cssText = "padding:6px 16px;background:#2d7d2d;color:#fff;border:none;border-radius:4px;cursor:pointer;";
            btnRow.appendChild(btnContinue);
            container.appendChild(btnRow);

            // Store references
            this._8cut = { video, timeline, table, btnContinue, regions: null, videoPath: "" };

            // Add as DOM widget
            this.addDOMWidget("video_review", "preview", container, {
                serialize: false,
                getMinHeight: () => 480,
            });

            // Timeline click → seek
            timeline.addEventListener("click", (e) => {
                if (!video.duration) return;
                const rect = timeline.getBoundingClientRect();
                const frac = (e.clientX - rect.left) / rect.width;
                video.currentTime = frac * video.duration;
            });

            // Timeline rendering
            const renderTimeline = () => {
                const ctx = timeline.getContext("2d");
                const w = timeline.width = timeline.clientWidth;
                const h = timeline.height;
                ctx.fillStyle = "#202020";
                ctx.fillRect(0, 0, w, h);

                if (!video.duration) return;

                // Draw regions
                const data = this._8cut.regions;
                if (data && data.regions) {
                    for (const r of data.regions) {
                        const x1 = (r.start / video.duration) * w;
                        const x2 = (r.end / video.duration) * w;
                        const alpha = 0.3 + r.score * 0.5;
                        ctx.fillStyle = r.disabled
                            ? `rgba(100,100,100,${alpha})`
                            : `rgba(100,200,255,${alpha})`;
                        ctx.fillRect(x1, 0, x2 - x1, h);
                    }
                }

                // Cursor
                const cx = (video.currentTime / video.duration) * w;
                ctx.strokeStyle = "#fff";
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(cx, 0);
                ctx.lineTo(cx, h);
                ctx.stroke();
            };

            video.addEventListener("timeupdate", renderTimeline);
            new ResizeObserver(renderTimeline).observe(timeline);

            // Continue button handler
            btnContinue.addEventListener("click", () => {
                // Re-queue the prompt with confirmed=true
                const regions = this._8cut.regions || { model: "", regions: [] };
                // Set widget values for re-execution
                this.widgets?.forEach(w => {
                    if (w.name === "confirmed") w.value = true;
                    if (w.name === "edited_regions") w.value = JSON.stringify(regions);
                });
                app.queuePrompt();
            });
        });

        // On executed — receive data from Python node
        chainCallback(nodeType.prototype, "onExecuted", function (message) {
            if (!this._8cut || !message) return;
            const { video_path, regions, node_id } = message;
            this._8cut.videoPath = video_path;
            this._8cut.regions = regions;
            this._8cut.nodeId = node_id;

            // Set video source
            this._8cut.video.src = api.apiURL(
                "/8cut/video?path=" + encodeURIComponent(video_path)
            );

            // Populate table
            const tbody = this._8cut.table.querySelector("tbody");
            tbody.innerHTML = "";
            if (regions && regions.regions) {
                for (const r of regions.regions) {
                    const tr = document.createElement("tr");
                    tr.style.cursor = "pointer";
                    tr.innerHTML = `<td>${formatTime(r.start)}</td><td>${formatTime(r.end)}</td><td>${r.score.toFixed(2)}</td>`;
                    tr.addEventListener("click", () => {
                        this._8cut.video.currentTime = r.start;
                    });
                    if (r.disabled) tr.style.color = "#666";
                    tbody.appendChild(tr);
                }
            }

            // Reset confirmed state for next run
            this.widgets?.forEach(w => {
                if (w.name === "confirmed") w.value = false;
            });
        });
    },
});

function formatTime(secs) {
    const m = Math.floor(secs / 60);
    const s = (secs % 60).toFixed(1);
    return `${m}:${s.padStart(4, "0")}`;
}
```

**Step 2: Test manually**

- Run the LoadVideo → AudioScan → VideoReview workflow
- Verify: video plays in the node widget, timeline shows colored regions, table lists results
- Click a table row → video seeks to that time
- Click timeline → video seeks
- Click Continue → prompt re-queues (downstream nodes would execute if connected)

**Step 3: Commit**

```bash
git add ComfyUI-8cut/web/
git commit -m "feat: VideoReview frontend — video player, timeline, region table"
```

---

### Task 5: VideoReview interactivity — region editing, negatives, disable

**Files:**
- Modify: `ComfyUI-8cut/web/js/video_review.js`

**Step 1: Add region disable toggle (D key or double-click)**

Add to the `onNodeCreated` callback, after table population in `onExecuted`:

- Double-click a table row → toggle `r.disabled`, update row color, redraw timeline
- Store toggle in local state (the `regions` object)

**Step 2: Add negative marking button**

Add "Add Negative" button to `btnRow`. On click:
- Collect selected table rows
- Send `POST /8cut/add_negatives` with their timestamps
- Mark rows red in the table
- Update local regions state

**Step 3: Add region edge dragging on timeline**

In the timeline canvas:
- `mousedown` near a region edge (within 5px) → start drag, track which region + which edge
- `mousemove` → update region start or end, redraw
- `mouseup` → finalize, send `POST /8cut/resize_region` to persist

**Step 4: Add version history dropdown**

- Add a `<select>` element above the table
- On node execution, fetch versions via `GET /8cut/scan_versions?filename=...&profile=...&model=...`
- On change, fetch that version's results and repopulate table + timeline

**Step 5: Test manually**

- Disable a region → gray in timeline + table
- Mark negative → red highlight, verify via DB
- Drag region edge → resizes, persists after Continue
- Switch version → table + timeline update

**Step 6: Commit**

```bash
git add ComfyUI-8cut/web/
git commit -m "feat: VideoReview interactivity — disable, negatives, drag resize, versions"
```

---

### Task 6: TrainModel node

**Files:**
- Create: `ComfyUI-8cut/nodes/train_model.py`
- Modify: `ComfyUI-8cut/nodes/__init__.py`
- Modify: `ComfyUI-8cut/server_routes.py`

**Step 1: Add data query routes to `server_routes.py`**

```python
@routes.get("/8cut/profiles")
async def get_profiles(request):
    from core.db import ProcessedDB
    db_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "8cut.db"
    )
    db = ProcessedDB(db_path)
    return web.json_response(db.get_profiles())


@routes.get("/8cut/export_folders")
async def get_export_folders(request):
    from core.db import ProcessedDB
    db_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "8cut.db"
    )
    db = ProcessedDB(db_path)
    profile = request.rel_url.query.get("profile", "default")
    return web.json_response(db.get_export_folders(profile))


@routes.get("/8cut/models")
async def list_models(request):
    models_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "models"
    )
    if not os.path.isdir(models_dir):
        return web.json_response([])
    files = sorted(f for f in os.listdir(models_dir) if f.endswith(".joblib"))
    return web.json_response(files)
```

**Step 2: Create `nodes/train_model.py`**

```python
"""TrainModel node — train an audio classifier from labeled data."""

import os
import sys

_pack_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pack_dir not in sys.path:
    sys.path.insert(0, _pack_dir)

from core.audio_scan import train_classifier, _EMBED_MODELS
from core.db import ProcessedDB

import server as comfy_server


class TrainModel:
    """Train an audio event classifier from exported clips."""

    @classmethod
    def INPUT_TYPES(cls):
        db_path = os.path.join(_pack_dir, "data", "8cut.db")
        db = ProcessedDB(db_path)
        profiles = db.get_profiles() or ["default"]
        embed_models = list(_EMBED_MODELS.keys())
        # Folders will be populated dynamically via frontend JS
        return {
            "required": {
                "profile": (profiles,),
                "positive_folder": ("STRING", {"default": ""}),
                "embed_model": (embed_models, {"default": "EAT_LARGE"}),
                "use_hard_negatives": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "negative_folder": ("STRING", {"default": ""}),
                "video_dir": ("STRING", {"default": ""}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("SCAN_MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "run"
    CATEGORY = "8cut"

    def run(self, profile, positive_folder, embed_model,
            use_hard_negatives, negative_folder="", video_dir="",
            unique_id=None):
        db_path = os.path.join(_pack_dir, "data", "8cut.db")
        db = ProcessedDB(db_path)

        video_infos = db.get_training_data(
            profile, positive_folder,
            negative_folder=negative_folder,
            fallback_video_dir=video_dir,
            use_hard_negatives=use_hard_negatives,
        )

        if not video_infos:
            raise ValueError(
                f"No training data found for profile '{profile}', "
                f"folder '{positive_folder}'"
            )

        model_name = f"{profile}_{embed_model}"
        model_path = os.path.join(_pack_dir, "models", f"{model_name}.joblib")
        os.makedirs(os.path.dirname(model_path), exist_ok=True)

        prompt_server = comfy_server.PromptServer.instance

        def progress_cb(current, total):
            if unique_id is not None:
                prompt_server.send_sync("progress", {
                    "value": current, "max": total,
                    "node": unique_id,
                })

        train_classifier(
            video_infos,
            model_path=model_path,
            embed_model=embed_model,
            progress_cb=progress_cb,
        )

        return (model_path,)
```

**Step 3: Register in `nodes/__init__.py`**

Add TrainModel to imports and both mappings dicts.

**Step 4: Test manually**

- Copy existing `8cut.db` to `ComfyUI-8cut/data/`
- Add TrainModel node, select profile + positive folder + embed model
- Run — verify training completes, `.joblib` saved to `models/`
- Connect output to AudioScan's model input — verify scan works with the trained model

**Step 5: Commit**

```bash
git add ComfyUI-8cut/
git commit -m "feat: TrainModel node wrapping core.audio_scan.train_classifier"
```

---

### Task 7: ExportClips node

**Files:**
- Create: `ComfyUI-8cut/nodes/export_clips.py`
- Modify: `ComfyUI-8cut/nodes/__init__.py`

**Step 1: Create `nodes/export_clips.py`**

```python
"""ExportClips node — export video clips from scan regions."""

import os
import sys
import subprocess

_pack_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pack_dir not in sys.path:
    sys.path.insert(0, _pack_dir)

from core.ffmpeg import build_ffmpeg_command
from core.paths import build_export_path, _bin

import server as comfy_server


def _build_export_spans(regions, fuse_gap=30.0, spread=3.0, min_dur=8.0):
    """Merge nearby regions into spans and place clips at spread intervals."""
    if not regions:
        return []
    sorted_r = sorted(regions, key=lambda r: r["start"])
    spans = []
    s, e = sorted_r[0]["start"], sorted_r[0]["end"]
    for r in sorted_r[1:]:
        if r["start"] - e <= fuse_gap:
            e = max(e, r["end"])
        else:
            spans.append((s, e))
            s, e = r["start"], r["end"]
    spans.append((s, e))

    groups = []
    step = max(spread, 1.0)
    for s, e in spans:
        dur = e - s
        if dur < min_dur:
            continue
        positions = []
        t = s
        while t + min_dur <= e:
            positions.append(t)
            t += step
        if positions:
            groups.append(positions)
    return groups


class ExportClips:
    """Export video clips from reviewed scan regions."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO_PATH",),
                "regions": ("SCAN_REGIONS",),
                "output_folder": ("STRING", {"default": "/tmp/8cut_export"}),
                "spread": ("FLOAT", {
                    "default": 3.0, "min": 1.0, "max": 30.0, "step": 0.5,
                }),
                "clip_count": ("INT", {
                    "default": 3, "min": 1, "max": 20,
                }),
                "fuse_gap": ("FLOAT", {
                    "default": 30.0, "min": 1.0, "max": 120.0, "step": 1.0,
                }),
            },
            "optional": {
                "short_side": ("INT", {
                    "default": 512, "min": 128, "max": 2160, "step": 8,
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("export_folder",)
    OUTPUT_NODE = True
    FUNCTION = "run"
    CATEGORY = "8cut"

    def run(self, video, regions, output_folder, spread, clip_count,
            fuse_gap, short_side=512, unique_id=None):
        # Filter to enabled regions only
        active = [r for r in regions.get("regions", []) if not r.get("disabled")]
        if not active:
            return {"ui": {"text": "No active regions to export"}, "result": (output_folder,)}

        groups = _build_export_spans(active, fuse_gap=fuse_gap, spread=spread)
        if not groups:
            return {"ui": {"text": "No spans long enough to export"}, "result": (output_folder,)}

        os.makedirs(output_folder, exist_ok=True)
        prompt_server = comfy_server.PromptServer.instance
        exported = []
        total_clips = sum(min(len(g), clip_count) for g in groups)
        done = 0

        for gi, group in enumerate(groups):
            for ci, start in enumerate(group[:clip_count]):
                out_name = f"clip_{gi:03d}_{ci:03d}.mp4"
                out_path = os.path.join(output_folder, out_name)
                cmd = build_ffmpeg_command(
                    video, start, out_path,
                    short_side=short_side,
                )
                subprocess.run(cmd, capture_output=True, timeout=120)
                exported.append(out_path)
                done += 1
                if unique_id is not None:
                    prompt_server.send_sync("progress", {
                        "value": done, "max": total_clips,
                        "node": unique_id,
                    })

        return {
            "ui": {"text": f"Exported {len(exported)} clips to {output_folder}"},
            "result": (output_folder,),
        }
```

**Step 2: Register in `nodes/__init__.py`**

Add ExportClips to imports and both mappings dicts.

**Step 3: Test manually**

- Full pipeline: LoadVideo → AudioScan → VideoReview → ExportClips
- Run scan, review (click Continue), verify clips appear in output folder
- Check ffmpeg output — correct resolution, timestamps

**Step 4: Commit**

```bash
git add ComfyUI-8cut/
git commit -m "feat: ExportClips node with region fusion and ffmpeg export"
```

---

### Task 8: DB initialization and profile management

**Files:**
- Modify: `ComfyUI-8cut/server_routes.py`
- Create: `ComfyUI-8cut/utils.py` (shared DB path helper)

**Step 1: Create `utils.py` — shared helpers**

Extract the repeated DB path construction:

```python
"""Shared utilities for ComfyUI-8cut."""

import os

PACK_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PACK_DIR, "data")
MODELS_DIR = os.path.join(PACK_DIR, "models")
DB_PATH = os.path.join(DATA_DIR, "8cut.db")


def get_db():
    """Return a ProcessedDB instance for the node pack's database."""
    from core.db import ProcessedDB
    os.makedirs(DATA_DIR, exist_ok=True)
    return ProcessedDB(DB_PATH)
```

**Step 2: Refactor all DB usage**

Replace all inline `db_path = os.path.join(...)` / `ProcessedDB(db_path)` calls in `server_routes.py` and node files to use `from utils import get_db, MODELS_DIR`.

**Step 3: Test manually**

- Delete `data/8cut.db`, start ComfyUI — verify DB is auto-created
- Copy existing DB — verify it works with migration

**Step 4: Commit**

```bash
git add ComfyUI-8cut/
git commit -m "refactor: extract shared DB/path helpers into utils.py"
```

---

### Task 9: End-to-end integration test

**Step 1: Full workflow test**

Execute the complete pipeline manually:

1. Place a video file accessible to the server
2. Copy a trained model to `models/` (or train one via TrainModel node)
3. Build workflow: LoadVideo → AudioScan → VideoReview → ExportClips
4. Run — verify scan completes with progress
5. In VideoReview: play video, click timeline, review regions
6. Disable a region, mark a negative
7. Click Continue
8. Verify ExportClips produces correct clips (disabled regions excluded)

**Step 2: Training round-trip test**

1. Export some clips via ExportClips (writes to DB)
2. Run TrainModel with the exported data
3. Rescan with the new model
4. Verify improved results

**Step 3: Remote access test**

1. Access ComfyUI from a different machine via browser
2. Verify video streams and plays smoothly
3. Verify all interactions work over the network

**Step 4: Push**

```bash
git push
```
