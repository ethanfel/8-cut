# Audio Similarity Scanning — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Scan a video's audio track to find segments matching a reference sound profile, displayed as highlighted regions on the timeline.

**Architecture:** New `core/audio_scan.py` module extracts MFCC features from reference clips and slides an 8s window across the target video's audio, scoring each position via cosine similarity. A `ScanWorker` QThread runs the scan in the background, and results are drawn as semi-transparent rectangles on the existing Timeline widget.

**Tech Stack:** Python 3, librosa 0.11, numpy, PyQt6

---

### Task 1: Core audio_scan module — build_profile

**Files:**
- Create: `core/audio_scan.py`
- Create: `tests/test_audio_scan.py`

**Step 1: Write the tests**

```python
# tests/test_audio_scan.py
import tempfile, os
import numpy as np
from core.audio_scan import build_profile, _extract_mfcc


def _make_wav(path: str, duration: float = 8.0, sr: int = 22050):
    """Create a short sine-wave WAV file for testing."""
    import soundfile as sf
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(path, audio, sr)


def test_extract_mfcc_returns_1d_vector():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        vec = _extract_mfcc(f.name)
        assert vec.shape == (20,)
        assert not np.isnan(vec).any()
    finally:
        os.unlink(f.name)


def test_build_profile_single_clip():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        profile = build_profile([f.name])
        assert "mean_vector" in profile
        assert "clip_vectors" in profile
        assert profile["mean_vector"].shape == (20,)
        assert len(profile["clip_vectors"]) == 1
    finally:
        os.unlink(f.name)


def test_build_profile_multiple_clips():
    paths = []
    try:
        for i in range(3):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            freq = 440 + i * 200
            import soundfile as sf
            t = np.linspace(0, 8.0, 22050 * 8, endpoint=False)
            sf.write(f.name, 0.5 * np.sin(2 * np.pi * freq * t), 22050)
            paths.append(f.name)
            f.close()

        profile = build_profile(paths)
        assert len(profile["clip_vectors"]) == 3
        assert profile["mean_vector"].shape == (20,)
    finally:
        for p in paths:
            os.unlink(p)


def test_build_profile_skips_missing_files():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        _make_wav(f.name)
    try:
        profile = build_profile([f.name, "/no/such/file.wav"])
        assert len(profile["clip_vectors"]) == 1
    finally:
        os.unlink(f.name)


def test_build_profile_empty_returns_none():
    result = build_profile([])
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.audio_scan'`

**Step 3: Write the implementation**

```python
# core/audio_scan.py
"""Audio similarity scanning — MFCC-based profile matching."""

import numpy as np
import librosa

from .paths import _log

_N_MFCC = 20
_SR = 22050


def _extract_mfcc(path: str, sr: int = _SR) -> np.ndarray:
    """Load audio from a file and return a mean MFCC vector (20-dim)."""
    y, _ = librosa.load(path, sr=sr, mono=True)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=_N_MFCC)
    return mfcc.mean(axis=1)  # average over time → (20,)


def build_profile(clip_paths: list[str]) -> dict | None:
    """Extract MFCCs from reference clips.

    Returns dict with:
      - mean_vector: averaged MFCC across all clips (20,)
      - clip_vectors: list of individual MFCC vectors
    Returns None if no clips could be loaded.
    """
    vectors = []
    for p in clip_paths:
        try:
            vec = _extract_mfcc(p)
            vectors.append(vec)
        except Exception as e:
            _log(f"audio_scan: skip {p}: {e}")
    if not vectors:
        return None
    arr = np.stack(vectors)
    return {
        "mean_vector": arr.mean(axis=0),
        "clip_vectors": vectors,
    }
```

**Step 4: Run tests to verify they pass**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py -v`
Expected: all 5 PASS

**Step 5: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: add audio_scan module with build_profile"
```

---

### Task 2: Core audio_scan module — scan_video

**Files:**
- Modify: `core/audio_scan.py`
- Modify: `tests/test_audio_scan.py`

**Step 1: Write the tests**

Add to `tests/test_audio_scan.py`:

```python
from core.audio_scan import scan_video


def test_scan_video_finds_matching_region():
    """A video made of the same sine wave as the reference should match."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        _make_wav(vid.name, duration=20.0)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.5, hop=1.0)
        assert len(regions) > 0
        for start, end, score in regions:
            assert abs((end - start) - 8.0) < 1e-9
            assert score >= 0.5
            assert score >= 0.5
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_nearest_mode():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        _make_wav(ref.name, duration=8.0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        _make_wav(vid.name, duration=20.0)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="nearest", threshold=0.5, hop=1.0)
        assert len(regions) > 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)


def test_scan_video_high_threshold_no_match():
    """Different frequencies with very high threshold should not match."""
    import soundfile as sf
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        t = np.linspace(0, 8.0, 22050 * 8, endpoint=False)
        sf.write(ref.name, 0.5 * np.sin(2 * np.pi * 440 * t), 22050)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as vid:
        # White noise — very different from sine wave
        sf.write(vid.name, np.random.randn(22050 * 20).astype(np.float32) * 0.1, 22050)
    try:
        profile = build_profile([ref.name])
        regions = scan_video(vid.name, profile, mode="average", threshold=0.99, hop=1.0)
        assert len(regions) == 0
    finally:
        os.unlink(ref.name)
        os.unlink(vid.name)
```

**Step 2: Run tests to verify they fail**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py::test_scan_video_finds_matching_region -v`
Expected: FAIL with `ImportError: cannot import name 'scan_video'`

**Step 3: Write the implementation**

Add to `core/audio_scan.py`:

```python
def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors.

    Returns value in [-1, 1]. Negative means anti-correlated (very
    dissimilar). For threshold filtering this is fine — negative scores
    never exceed the threshold. Scores near 0 may be uncorrelated or
    weakly anti-correlated.
    """
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def scan_video(
    video_path: str,
    profile: dict,
    mode: str = "average",
    threshold: float = 0.7,
    hop: float = 1.0,
    window: float = 8.0,
    cancel_flag: object = None,
) -> list[tuple[float, float, float]]:
    """Slide a window across the video audio and score against the profile.

    Args:
        video_path: path to video/audio file
        profile: dict from build_profile()
        mode: "average" (compare to mean) or "nearest" (max over all clips)
        threshold: minimum cosine similarity to include
        hop: step size in seconds
        window: window size in seconds (default 8s)
        cancel_flag: object with _cancel bool attribute; checked each iteration

    Returns:
        list of (start_time, end_time, score) for regions above threshold
    """
    _log(f"audio_scan: loading {video_path}")
    y, sr = librosa.load(video_path, sr=_SR, mono=True)
    duration = len(y) / sr
    _log(f"audio_scan: {duration:.1f}s loaded, scanning with hop={hop}s")

    win_samples = int(window * sr)
    hop_samples = int(hop * sr)

    results = []
    pos = 0
    while pos + win_samples <= len(y):
        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            _log("audio_scan: cancelled")
            return results

        chunk = y[pos : pos + win_samples]
        mfcc = librosa.feature.mfcc(y=chunk, sr=sr, n_mfcc=_N_MFCC)
        vec = mfcc.mean(axis=1)

        if mode == "nearest":
            score = max(
                _cosine_similarity(vec, cv) for cv in profile["clip_vectors"]
            )
        else:  # average
            score = _cosine_similarity(vec, profile["mean_vector"])

        if score >= threshold:
            start_t = pos / sr
            results.append((start_t, start_t + window, score))

        pos += hop_samples

    _log(f"audio_scan: {len(results)} regions above threshold {threshold}")
    return results
```

**Step 4: Run tests to verify they pass**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py -v`
Expected: all 8 PASS

**Step 5: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: add scan_video with average and nearest modes"
```

---

### Task 3: Timeline — draw scan regions

**Files:**
- Modify: `main.py` (Timeline class, around lines 209-260 and 300-375)

**Step 1: Add scan region storage to Timeline.__init__**

In `main.py`, find the Timeline class `__init__` method (around line 198). After `self._markers` initialization (line 209), add:

```python
self._scan_regions: list[tuple[float, float, float]] = []  # (start, end, score)
```

**Step 2: Add set_scan_regions method**

After the `set_markers` method (line 249-252), add:

```python
def set_scan_regions(self, regions: list[tuple[float, float, float]]) -> None:
    """regions: list of (start_time, end_time, score)"""
    self._scan_regions = regions
    self.update()

def clear_scan_regions(self) -> None:
    self._scan_regions = []
    self.update()
```

**Step 3: Draw scan regions in paintEvent**

In `paintEvent` (starts around line 282), find the marker drawing section (line 363, comment `# ── export markers`). BEFORE that section, add:

```python
# ── scan regions ──────────────────────────────────────────────
if self._scan_regions and self._duration > 0:
    for (start, end, score) in self._scan_regions:
        x1 = int(start / self._duration * w)
        x2 = int(end / self._duration * w)
        alpha = int(40 + score * 80)  # 40–120 opacity
        p.fillRect(x1, rh, x2 - x1, h - rh, QColor(100, 200, 255, alpha))
```

**Step 4: Verify manually**

Run: `cd /media/p5/8-cut && python main.py`
Expected: app starts without errors. No scan regions visible yet (none set).

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: timeline scan region rendering"
```

---

### Task 4: ScanWorker QThread

**Files:**
- Modify: `main.py` (add ScanWorker class, after ExportWorker around line 165)

**Step 1: Add the ScanWorker class**

After the `ExportWorker` class (ends around line 165), add:

```python
class ScanWorker(QThread):
    """Runs audio similarity scan off the main thread."""
    finished = pyqtSignal(list)   # emits list of (start, end, score)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)    # status message

    def __init__(self, video_path: str, clip_paths: list[str],
                 mode: str = "average", threshold: float = 0.7):
        super().__init__()
        self._video_path = video_path
        self._clip_paths = clip_paths
        self._mode = mode
        self._threshold = threshold
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self):
        from core.audio_scan import build_profile, scan_video
        try:
            self.progress.emit(f"Building profile from {len(self._clip_paths)} clips...")
            profile = build_profile(self._clip_paths)
            if self._cancel:
                return
            if profile is None:
                self.error.emit("No valid reference clips found")
                return
            self.progress.emit("Scanning audio...")
            regions = scan_video(
                self._video_path, profile,
                mode=self._mode, threshold=self._threshold,
                cancel_flag=self,
            )
            if not self._cancel:
                self.finished.emit(regions)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))
```

**Step 2: Verify import works**

Run: `cd /media/p5/8-cut && python -c "from main import ScanWorker; print('ok')"`
Expected: `ok`

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add ScanWorker QThread for background scanning"
```

---

### Task 5: DB helper — get_all_export_paths

**Files:**
- Modify: `core/db.py`
- Modify: `tests/test_audio_scan.py`

**Step 1: Write the test**

Add to `tests/test_audio_scan.py`:

```python
def test_db_get_all_export_paths():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        from core.db import ProcessedDB
        db = ProcessedDB(path)
        db.add("a.mp4", 10.0, "/out/a_001.mp4", profile="test")
        db.add("b.mp4", 20.0, "/out/b_001.mp4", profile="test")
        db.add("c.mp4", 30.0, "/out/c_001.mp4", profile="other")
        paths = db.get_all_export_paths("test")
        assert set(paths) == {"/out/a_001.mp4", "/out/b_001.mp4"}
    finally:
        os.unlink(path)
```

**Step 2: Run test to verify it fails**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py::test_db_get_all_export_paths -v`
Expected: FAIL with `AttributeError: 'ProcessedDB' object has no attribute 'get_all_export_paths'`

**Step 3: Write the implementation**

Add to `core/db.py`, after the `get_markers` method. Note: no lock needed — follows
the codebase convention where read-only methods don't acquire the lock.

```python
def get_all_export_paths(self, profile: str = "default") -> list[str]:
    """Return all unique output_path values for a given profile."""
    if not self._enabled:
        return []
    rows = self._con.execute(
        "SELECT DISTINCT output_path FROM processed WHERE profile = ?",
        (profile,),
    ).fetchall()
    return [r[0] for r in rows]
```

**Step 4: Run test to verify it passes**

Run: `cd /media/p5/8-cut && python -m pytest tests/test_audio_scan.py::test_db_get_all_export_paths -v`
Expected: PASS

**Step 5: Commit**

```bash
git add core/db.py tests/test_audio_scan.py
git commit -m "feat: add get_all_export_paths to ProcessedDB"
```

---

### Task 6: UI controls for audio scanning

**Files:**
- Modify: `main.py` (MainWindow class — control creation ~1490-1575, layout ~1620-1640)

**Step 1: Add scan control widgets**

In the MainWindow `__init__`, find the control creation section. After `self._chk_track` (around line 1501), add:

```python
# ── audio scan controls ──────────────────────────────────────
self._btn_scan = QPushButton("Scan")
self._btn_scan.setToolTip("Scan current video for audio segments matching reference clips")
self._btn_scan.clicked.connect(self._start_scan)

self._sld_threshold = QDoubleSpinBox()
self._sld_threshold.setRange(0.0, 1.0)
self._sld_threshold.setSingleStep(0.05)
self._sld_threshold.setValue(0.7)
self._sld_threshold.setPrefix("Thr: ")
self._sld_threshold.setToolTip("Similarity threshold (0=match everything, 1=exact match)")

self._cmb_scan_mode = QComboBox()
self._cmb_scan_mode.addItems(["Average", "Nearest"])
self._cmb_scan_mode.setToolTip("Average: compare to mean profile\nNearest: compare to closest clip")

self._cmb_scan_ref = QComboBox()
self._cmb_scan_ref.addItems(["Current Profile", "Custom Folder"])
self._cmb_scan_ref.currentIndexChanged.connect(self._on_scan_ref_changed)
self._scan_folder: str = ""

self._scan_worker: ScanWorker | None = None
```

**Step 2: Add controls to settings_row layout**

Find the `settings_row` assembly (around line 1620). Before `settings_row.addStretch()` (around line 1635), add:

```python
settings_row.addWidget(self._btn_scan)
settings_row.addWidget(self._sld_threshold)
settings_row.addWidget(self._cmb_scan_mode)
settings_row.addWidget(self._cmb_scan_ref)
```

**Step 3: Add handler methods**

Add these methods to MainWindow (after `_jump_to_next_marker` around line 2410):

```python
def _on_scan_ref_changed(self, index: int) -> None:
    if index == 1:  # Custom Folder
        folder = QFileDialog.getExistingDirectory(self, "Select reference clip folder")
        if folder:
            self._scan_folder = folder
        else:
            self._cmb_scan_ref.setCurrentIndex(0)

def _cleanup_scan_worker(self) -> None:
    """Disconnect signals and schedule deletion of old scan worker."""
    if self._scan_worker is not None:
        try:
            self._scan_worker.finished.disconnect()
            self._scan_worker.error.disconnect()
            self._scan_worker.progress.disconnect()
        except TypeError:
            pass  # already disconnected
        self._scan_worker.deleteLater()
        self._scan_worker = None

def _start_scan(self) -> None:
    if not self._file_path:
        self._show_status("No video loaded")
        return
    if self._scan_worker and self._scan_worker.isRunning():
        self._show_status("Scan already running")
        return

    # Clean up previous worker
    self._cleanup_scan_worker()

    # Collect reference clip paths
    if self._cmb_scan_ref.currentIndex() == 0:
        # Current profile — all exports across all files in this profile
        clip_paths = [p for p in self._db.get_all_export_paths(self._profile)
                      if os.path.exists(p)]
    else:
        # Custom folder
        if not self._scan_folder:
            self._show_status("No reference folder selected")
            return
        exts = (".mp4", ".mkv", ".avi", ".mov", ".wav", ".mp3", ".flac")
        clip_paths = [
            os.path.join(self._scan_folder, f)
            for f in sorted(os.listdir(self._scan_folder))
            if f.lower().endswith(exts)
        ]

    if not clip_paths:
        self._show_status("No reference clips found")
        return

    mode = self._cmb_scan_mode.currentText().lower()
    threshold = self._sld_threshold.value()

    self._btn_scan.setEnabled(False)
    self._scan_file_path = self._file_path  # remember which file we're scanning
    self._show_status(f"Scanning with {len(clip_paths)} reference clips...")

    self._scan_worker = ScanWorker(self._file_path, clip_paths, mode, threshold)
    self._scan_worker.finished.connect(self._on_scan_done)
    self._scan_worker.error.connect(self._on_scan_error)
    self._scan_worker.progress.connect(self._show_status)
    self._scan_worker.start()

def _on_scan_done(self, regions: list) -> None:
    self._btn_scan.setEnabled(True)
    # Ignore stale results if the user switched files during scan
    if self._file_path != getattr(self, '_scan_file_path', None):
        return
    self._timeline.set_scan_regions(regions)
    self._show_status(f"Scan complete: {len(regions)} matching regions")

def _on_scan_error(self, msg: str) -> None:
    self._btn_scan.setEnabled(True)
    self._show_status(f"Scan error: {msg}")
```

**Step 4: Verify manually**

Run: `cd /media/p5/8-cut && python main.py`
Expected: Scan button, threshold spinner, mode dropdown, and reference source dropdown visible in the settings row. Clicking Scan with no file loaded shows "No video loaded" in status.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: add scan UI controls and start_scan handler"
```

---

### Task 7: Keyboard shortcut — jump to next scan region

**Files:**
- Modify: `main.py`

**Step 1: Add the keyboard shortcut**

Find the shortcut definitions (around line 1728, where `QShortcut(QKeySequence("M"), ...)` is defined). Add after it:

```python
QShortcut(QKeySequence("S"), self, context=ctx).activated.connect(self._jump_to_next_scan_region)
```

**Step 2: Add the jump method**

After `_on_scan_error` (or after `_jump_to_next_marker`), add:

```python
def _jump_to_next_scan_region(self) -> None:
    regions = sorted(self._timeline._scan_regions, key=lambda r: r[0])
    if not regions:
        return
    for (start, _end, _score) in regions:
        if start > self._cursor + 0.1:
            self._step_cursor(start - self._cursor)
            return
    # Wrap to first region
    self._step_cursor(regions[0][0] - self._cursor)
```

**Step 3: Update help text**

Find the help/shortcuts tooltip (around line 1757). Add a row:

```python
"<tr><td><b>S</b></td><td>Jump to next scan region</td></tr>"
```

**Step 4: Clear scan regions and cancel running scan on file change**

Find `_load_file` method (around line 1931). After the existing marker/state resets, add:

```python
self._timeline.clear_scan_regions()
if self._scan_worker and self._scan_worker.isRunning():
    self._scan_worker.cancel()
self._cleanup_scan_worker()
self._btn_scan.setEnabled(True)
```

**Step 5: Verify manually**

Run: `cd /media/p5/8-cut && python main.py`
Expected: S key does nothing when no scan regions exist. After a scan, S jumps through matched regions.

**Step 6: Commit**

```bash
git add main.py
git commit -m "feat: add S shortcut and clear scan on file change"
```

---

### Task 8: Final integration test

**Step 1: End-to-end manual test**

1. Open the app: `cd /media/p5/8-cut && python main.py`
2. Load a video file
3. Export a few clips (these become the reference)
4. Set reference source to "Current Profile"
5. Click "Scan"
6. Verify: status shows progress messages, then "Scan complete: N matching regions"
7. Verify: cyan-tinted regions appear on the timeline
8. Press S to jump through scan regions
9. Change threshold and re-scan — verify different number of regions
10. Switch mode to "Nearest" and re-scan
11. Switch reference to "Custom Folder", pick a folder with clips
12. Re-scan and verify results

**Step 2: Run all tests**

Run: `cd /media/p5/8-cut && python -m pytest tests/ -v`
Expected: all tests PASS

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat: audio similarity scanning complete"
```
