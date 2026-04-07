# 8-cut

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/ethanfel/8-cut/blob/master/LICENSE)

**Source:** https://github.com/ethanfel/8-cut

A desktop tool for cutting 8-second clips from video files, designed for building [SELVA](https://github.com/google-deepmind/selva) datasets.

## Overview

8-cut lets you scrub through a video, mark a cut point, and export a fixed 8-second clip with one keypress. It tracks every export in a local SQLite database so you can resume a session or switch between resolution variants of the same source without duplicating work.

All clips are exactly 8 seconds — this is a hard constraint of the SELVA format.

## Features

- **Frame-accurate scrubbing** — click or drag the timeline; arrow keys and J/K/L for frame-by-frame stepping
- **Keyboard shortcuts** — J/L step one frame, Shift+J/L step one second, Space/P play/pause, K pause and return to cursor, E export, M jump to next marker
- **Two export formats** — H.264/AAC MP4 or lossless WebP image sequence (frames + `.wav` audio extracted alongside)
- **Portrait crop** — crop to 9:16, 4:5, or 1:1 before export; adjustable horizontal crop position
- **Resize** — scale short side to a fixed pixel size (e.g. 256)
- **Export history** — timeline markers show previously exported clips; fuzzy filename matching detects resolution variants of the same file (e.g. `_2160p` vs `_1080p`)
- **Mask generation** — generate binary foreground masks per-frame using SAM2 (segmentation) or Depth Anything V2 (depth-based), via a bundled venv
- **Playlist** — drag-and-drop multiple files; duplicates are ignored

## Requirements

- Python 3.11+
- `ffmpeg` in `PATH`
- PyQt6
- python-mpv (requires libmpv)

```
pip install -r requirements.txt
```

For mask generation tools, additional dependencies (PyTorch, transformers, segment-anything-2, opencv) are installed into `~/.8cut/venv/` via the Settings dialog.

### Platform notes

**Linux** — install libmpv via your package manager (`apt install libmpv-dev` / `pacman -S mpv`).

**macOS** — install libmpv via Homebrew: `brew install mpv`.

**Windows** — `python-mpv` requires `mpv-2.dll` in `PATH` or in the same directory as `main.py`. Download it from the [mpv Windows builds](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/) page (pick the latest `mpv-dev-x86_64-*.7z`, extract `mpv-2.dll`). Also ensure `ffmpeg.exe` is in `PATH` (e.g. via [winget](https://winget.run/): `winget install ffmpeg`).

## Usage

```
python main.py
```

Drop a video onto the playlist or use the file picker. Scrub to your cut point, set the output folder and clip name, then press **Export** (or `E`).

### Export formats

| Format | Output |
|--------|--------|
| MP4 | `<folder>/<name>_NNN.mp4` — H.264 video + AAC audio |
| WebP sequence | `<folder>/<name>_NNN/frame_%04d.webp` — lossless WebP frames + `<name>_NNN.wav` PCM audio |

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `←` / `J` | Step back 1 frame |
| `→` / `L` | Step forward 1 frame |
| `Shift+←` / `Shift+J` | Step back 1 second |
| `Shift+→` / `Shift+L` | Step forward 1 second |
| `Space` / `P` | Toggle play/pause |
| `K` | Pause and snap video to cursor |
| `E` | Export clip |
| `M` | Jump to next export marker (wraps) |

Arrow keys and J/K/L are ignored when a text field has focus.

### Mask generation tools

Two standalone scripts live in `tools/`. They are run by the app via a managed venv but can also be called directly:

```
python tools/sam_masks.py  --input clip.mp4 --output masks_dir/
python tools/depth_masks.py --input clip.mp4 --output masks_dir/
```

Both output one binary PNG per frame (`frame_0000.png`, …) where white = foreground.

- **SAM2** (`sam_masks.py`) — uses `facebook/sam2-hiera-large`; center-point prompt propagated across all frames
- **Depth Anything V2** (`depth_masks.py`) — uses `depth-anything/Depth-Anything-V2-Large-hf`; Otsu threshold on the depth map

## Database

Export history is stored in `~/.8cut.db` (SQLite). The database records filename, start time, and output path for every clip. When you open a file, 8-cut checks whether a similar filename has been processed before (stripping resolution tags like `_2160p`, `_1080p`, codec tags, etc.) and pre-populates the timeline with existing markers.

## Testing

```
pytest tests/ -v
```

38 unit tests covering path builders, ffmpeg command generation, time formatting, and the processed-clips database.

## License

[GNU General Public License v3.0](https://github.com/ethanfel/8-cut/blob/master/LICENSE)
