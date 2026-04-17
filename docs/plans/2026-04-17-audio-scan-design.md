# Audio Similarity Scanning — Design

**Goal:** Scan a video's audio track and highlight segments that match the sound profile of existing reference clips, so the user can quickly find similar moments without scrubbing manually.

**Runs in:** Python/Qt client (`main.py`), not the server.

---

## Core Module: `core/audio_scan.py`

New module alongside `core/tracking.py`. Two main functions:

- `build_profile(clip_paths: list[str]) -> dict` — extracts MFCCs (20 coefficients) from each clip using `librosa`, returns a profile containing both the averaged vector and individual clip vectors.
- `scan_video(video_path: str, profile: dict, mode: str, threshold: float, hop: float) -> list[tuple[float, float, float]]` — slides an 8s window across the video's audio, returns `(start_time, end_time, score)` tuples for segments above threshold.

### Feature Extraction

- Audio loaded via `librosa.load()` (handles video files directly, mono, 22050Hz).
- MFCCs: `librosa.feature.mfcc(n_mfcc=20)`, averaged over time axis to produce a single vector per window/clip.
- Similarity: cosine similarity (`numpy` dot product on L2-normalized vectors).

### Matching Modes

- **Average mode:** Compare each window to the mean of all reference MFCC vectors. Fast, good when references are homogeneous.
- **Nearest mode:** Compare each window to every reference vector, take the max score. Better when references have variety within the style.

### Parameters

- `threshold` (float, 0.0–1.0): minimum cosine similarity to include a segment. Default 0.7.
- `hop` (float, seconds): step size for the sliding window. Default 1.0s.
- Window size fixed at 8s to match reference clip length.

---

## UI Integration in `main.py`

### Controls

Added near the existing tracking checkbox area:

- **"Scan" button** — triggers audio scan on current video.
- **Threshold slider** (0.0–1.0, step 0.05) — controls match strictness.
- **Mode combobox** — "Average" / "Nearest".
- **Reference source combobox** — "Current Profile" / "Custom Folder" (shows folder picker when "Custom Folder" selected).

### Scan Workflow

1. User clicks Scan.
2. Reference clips collected: either all export `output_path` values from the current profile (via DB) or all audio/video files in a custom folder.
3. Scan runs in a `QThread` so UI stays responsive.
4. On completion, results sent to Timeline widget via signal.

### Timeline Display

- New `set_scan_regions(regions: list[tuple[float, float, float]])` method on Timeline.
- Drawn as semi-transparent colored rectangles behind existing markers.
- Color intensity proportional to score (brighter = higher match).
- Cleared on file change or re-scan.

### Keyboard Shortcut

- `S` — jump cursor to the next scan region (similar to `M` for next marker).

---

## Data Flow

```
Reference clips (DB export paths or folder)
    |
librosa.load() each -> MFCC vectors (20-dim)
    |
Profile: { mean_vector, clip_vectors[] }
    |
Current video -> librosa.load() full audio (mono 22050Hz)
    |
Sliding 8s window (hop=1s) -> MFCC per window
    |
Cosine similarity vs profile -> score per position
    |
Threshold filter -> [(start, end, score), ...]
    |
Timeline: semi-transparent highlight regions
```

## Performance

- 2-hour video at 22050Hz mono ~ 380MB memory.
- MFCC extraction + sliding window: ~10-30s.
- QThread keeps UI responsive.

## What This Does NOT Do

- No DB schema changes — scan results are ephemeral (visual only).
- No auto-export — user decides what to cut.
- No server integration — runs entirely in the Python client.
- No GPU/ML model dependency — just librosa + numpy.
