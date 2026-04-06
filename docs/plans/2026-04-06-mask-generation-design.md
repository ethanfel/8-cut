# Mask Generation Design

## Overview

Add per-frame PNG mask generation to the 8-cut export pipeline for SELVA dataset creation. Two methods are supported: Depth Anything V2 (fast, depth-based foreground) and SAM2 (accurate, propagated segmentation). Both run as isolated subprocesses inside a dedicated ML venv.

## UI Changes

### Settings dialog

A "Settings…" button added to the main window top bar opens a `SettingsDialog` (QDialog). It contains a "ML Tools" section with:
- Status label: "Not installed" / "Installed"
- **Install** button (becomes **Reinstall** once venv exists)
- Read-only `QPlainTextEdit` streaming pip install output line-by-line

### Mask generation row

A new row added to the main window below the export row:
- `QComboBox`: "Depth Anything" / "SAM"
- **Generate Masks** button — disabled until venv is installed
- Operates on `self._last_export_path` (set after each successful export)
- Output folder: `<clip_stem>_masks/` next to the video
- Frames named `frame_0000.png`, `frame_0001.png`, …
- Progress streamed to status bar; button disabled during run

## Venv

Path: `~/.8cut/venv/`

Install command (run as subprocess, output streamed to dialog):
```
~/.8cut/venv/bin/pip install torch torchvision transformers opencv-python Pillow segment-anything-2
```

Setup steps:
1. `python -m venv ~/.8cut/venv`
2. `~/.8cut/venv/bin/pip install --upgrade pip`
3. `~/.8cut/venv/bin/pip install torch torchvision transformers opencv-python Pillow segment-anything-2`

Venv existence check: `Path("~/.8cut/venv/bin/python").expanduser().exists()`.

## tools/depth_masks.py

CLI: `python tools/depth_masks.py --input <video.mp4> --output <dir>`

1. Extract frames with OpenCV (`cv2.VideoCapture`)
2. Load `transformers` depth-estimation pipeline: `depth-anything/Depth-Anything-V2-Large-hf`, device=`cuda` if available else `cpu`
3. For each frame:
   - Run depth estimation → float depth array
   - Normalise to 0–255
   - Apply Otsu threshold (`cv2.threshold(..., cv2.THRESH_OTSU)`) → binary mask
   - Save as `frame_NNNN.png`
   - Print `frame N/total` to stdout
4. Exit 0 on success, non-zero on error

## tools/sam_masks.py

CLI: `python tools/sam_masks.py --input <video.mp4> --output <dir>`

1. Extract all frames to a temp directory with OpenCV
2. Load SAM2 video predictor (`sam2.build_sam.build_sam2_video_predictor`), checkpoint auto-downloaded from HuggingFace
3. Init predictor on the frame directory
4. Add point prompt: center pixel of frame 0 as positive point `[[w//2, h//2]]`, label `[1]`
5. Propagate masks across all frames (`propagate_in_video`)
6. For each frame: extract binary mask, save as `frame_NNNN.png`
7. Print `frame N/total` to stdout

## MaskWorker

`MaskWorker(QThread)` mirrors `ExportWorker`:
- `__init__(script: str, input_path: str, output_dir: str, venv_python: str)`
- `run()`: calls `subprocess.Popen([venv_python, script, "--input", input_path, "--output", output_dir])`, reads stdout line-by-line, emits `progress = pyqtSignal(str)`, emits `finished = pyqtSignal()` or `error = pyqtSignal(str)`

## MainWindow wiring

- `self._last_export_path: str = ""` — set in `_on_export_done`
- `_on_generate_masks()`: builds output dir path, creates it, instantiates `MaskWorker` with the selected script
- Venv installed check on startup: if venv exists, enable the Generate Masks button
