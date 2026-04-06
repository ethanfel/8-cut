# Portrait Crop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a portrait crop mode that crops a vertical window from the landscape source, with a ratio dropdown (Off/9:16/4:5/1:1), a crop bar for visual positioning, and video click-to-center.

**Architecture:** A module-level `_portrait_crop_filter` helper builds the ffmpeg `crop=` expression; `build_ffmpeg_command` chains it with the existing scale filter when both are active. `CropBarWidget` is a new thin `QWidget` that paints and repositions the crop window. `MpvWidget` gains a `crop_clicked` signal. `MainWindow` wires everything together and persists state via `QSettings`.

**Tech Stack:** PyQt6 (`QComboBox`, `QPainter`, `pyqtSignal`), ffmpeg `crop` filter expression syntax. No new dependencies.

---

### Task 1: `build_ffmpeg_command` portrait crop (TDD)

**Files:**
- Modify: `main.py` — add `_RATIOS`, `_portrait_crop_filter`, update `build_ffmpeg_command`
- Modify: `tests/test_utils.py` — add 3 new tests

**Step 1: Write failing tests**

Add to the end of `tests/test_utils.py`:

```python
def test_ffmpeg_command_portrait_only():
    cmd = build_ffmpeg_command(
        "/in/video.mp4", 0.0, "/out/clip.mp4",
        portrait_ratio="9:16", crop_center=0.5,
    )
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop" in vf
    assert "9" in vf
    assert "scale" not in vf
    assert cmd[-1] == "/out/clip.mp4"

def test_ffmpeg_command_portrait_and_resize():
    cmd = build_ffmpeg_command(
        "/in/video.mp4", 0.0, "/out/clip.mp4",
        short_side=256, portrait_ratio="9:16", crop_center=0.5,
    )
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop" in vf
    assert "scale" in vf
    # crop must come before scale
    assert vf.index("crop") < vf.index("scale")
    assert cmd[-1] == "/out/clip.mp4"

def test_ffmpeg_command_portrait_off():
    cmd = build_ffmpeg_command("/in/video.mp4", 0.0, "/out/clip.mp4")
    assert "-vf" not in cmd
```

**Step 2: Run to verify they fail**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "portrait" -v 2>&1 | tail -20
```

Expected: all 3 fail — `portrait_ratio` param doesn't exist yet.

**Step 3: Add `_RATIOS` and `_portrait_crop_filter` to `main.py`**

Add after `build_ffmpeg_command` (after line 52, before `_normalize_filename`):

```python
_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "4:5":  (4, 5),
    "1:1":  (1, 1),
}


def _portrait_crop_filter(ratio: str, crop_center: float) -> str:
    """Return an ffmpeg crop= filter expression for the given portrait ratio.

    Uses ffmpeg expression syntax so source dimensions are resolved at runtime.
    Commas inside min()/max() are escaped with \\, to prevent ffmpeg's
    filtergraph parser from treating them as filter-chain separators.
    """
    num, den = _RATIOS[ratio]
    cw = f"ih*{num}/{den}"
    # Clamp x so the crop window never exceeds frame bounds.
    x = f"max(0\\,min((iw-{cw})*{crop_center}\\,iw-{cw}))"
    return f"crop={cw}:ih:{x}:0"
```

**Step 4: Update `build_ffmpeg_command`**

Replace the function signature and body (lines 33-52):

```python
def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
    portrait_ratio: str | None = None,
    crop_center: float = 0.5,
) -> list[str]:
    # -ss before -i: fast input-seeking. Safe here because we always re-encode
    # (libx264/aac), so there is no keyframe-alignment issue from pre-input seek.
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
    ]

    filters: list[str] = []
    if portrait_ratio is not None:
        filters.append(_portrait_crop_filter(portrait_ratio, crop_center))
    if short_side is not None:
        # Scale so the shorter dimension equals short_side.
        # if(lt(iw,ih),...) → portrait output: fix width; landscape: fix height.
        # -2 keeps aspect ratio with even-pixel rounding (libx264 requirement).
        filters.append(
            f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})'"
        )
    if filters:
        cmd += ["-vf", ",".join(filters)]

    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    return cmd
```

**Step 5: Run portrait tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "portrait or resize or ffmpeg" -v 2>&1 | tail -20
```

Expected: all 5 ffmpeg tests pass.

**Step 6: Run full suite**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all 23 tests pass.

**Step 7: Commit**

```bash
cd /media/p5/8-cut && git add main.py tests/test_utils.py && git commit -m "feat: portrait crop filter in build_ffmpeg_command"
```

---

### Task 2: `CropBarWidget`

**Files:**
- Modify: `main.py` — add `CropBarWidget` class before `PlaylistWidget`

**Step 1: Add `CropBarWidget` class**

Insert the following class in `main.py` right before `class PlaylistWidget` (currently around line 331):

```python
class CropBarWidget(QWidget):
    """Thin bar showing the portrait crop window position within the frame width.

    Full bar width = source frame width (100%).
    Highlighted region = selected crop window proportion.
    Click to reposition crop center.
    """
    crop_changed = pyqtSignal(float)  # emits clamped crop center 0.0–1.0

    def __init__(self):
        super().__init__()
        self.setFixedHeight(16)
        self.setMouseTracking(True)
        self._source_ratio: float = 16 / 9   # w/h of source video
        self._portrait_ratio: tuple[int, int] | None = None  # (num, den)
        self._crop_center: float = 0.5

    def set_source_ratio(self, w: int, h: int) -> None:
        self._source_ratio = w / h if h > 0 else 16 / 9
        self.update()

    def set_portrait_ratio(self, ratio: str | None) -> None:
        self._portrait_ratio = _RATIOS[ratio] if ratio else None
        self.update()

    def set_crop_center(self, frac: float) -> None:
        self._crop_center = max(0.0, min(1.0, frac))
        self.update()

    def _crop_window_frac(self) -> float:
        """Crop window width as a fraction of the bar (0–1)."""
        if self._portrait_ratio is None:
            return 1.0
        num, den = self._portrait_ratio
        portrait_ar = num / den          # e.g. 9/16 = 0.5625
        return portrait_ar / self._source_ratio  # fraction of source width

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            w, h = self.width(), self.height()
            p.fillRect(0, 0, w, h, QColor(40, 40, 40))

            if self._portrait_ratio is None:
                return

            win_frac = self._crop_window_frac()
            win_px = int(w * win_frac)
            max_x = w - win_px
            x = int(max_x * self._crop_center)

            # Crop window highlight
            p.fillRect(x, 1, win_px, h - 2, QColor(80, 140, 220, 160))
            # Border
            pen = QPen(QColor(100, 160, 240))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawRect(x, 1, win_px - 1, h - 2)
        finally:
            p.end()

    def mousePressEvent(self, event):
        self._update_from_x(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons():
            self._update_from_x(event.position().x())

    def _update_from_x(self, x: float) -> None:
        if self._portrait_ratio is None:
            return
        w = self.width()
        win_frac = self._crop_window_frac()
        win_px = w * win_frac
        max_x = w - win_px
        if max_x <= 0:
            frac = 0.5
        else:
            # Center the window on the click point
            frac = (x - win_px / 2) / max_x
            frac = max(0.0, min(1.0, frac))
        self.set_crop_center(frac)
        self.crop_changed.emit(self._crop_center)
```

**Step 2: Verify headless import**

```bash
cd /media/p5/8-cut && python -c "from main import CropBarWidget"
```

Expected: no output.

**Step 3: Run all tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all 23 tests pass.

**Step 4: Commit**

```bash
cd /media/p5/8-cut && git add main.py && git commit -m "feat: CropBarWidget for portrait crop positioning"
```

---

### Task 3: Wire MainWindow

**Files:**
- Modify: `main.py` — update imports, `MpvWidget`, `ExportWorker`, `MainWindow`

**Step 1: Add `QComboBox` to QtWidgets imports**

Current line 10–14:
```python
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
)
```

Add `QComboBox`:
```python
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox,
)
```

**Step 2: Add `crop_clicked` signal and `get_video_size` to `MpvWidget`**

`MpvWidget` currently declares one signal: `file_loaded = pyqtSignal()` (line 271).

Add `crop_clicked = pyqtSignal(float)` on the next line:
```python
class MpvWidget(QFrame):
    file_loaded = pyqtSignal()   # emitted (on Qt thread) when a file is ready
    crop_clicked = pyqtSignal(float)  # x fraction 0–1 when user clicks video
```

Add `mousePressEvent` and `get_video_size` methods to `MpvWidget` right before `closeEvent`:

```python
    def get_video_size(self) -> tuple[int, int]:
        if self._player:
            return (self._player.width or 0, self._player.height or 0)
        return (0, 0)

    def mousePressEvent(self, event):
        w = self.width()
        if w > 0:
            self.crop_clicked.emit(event.position().x() / w)
```

**Step 3: Update `ExportWorker` to accept portrait params**

Current `ExportWorker.__init__` signature (line 154):
```python
    def __init__(self, input_path: str, start: float, output_path: str,
                 short_side: int | None = None):
```

Replace the entire `ExportWorker` class with:

```python
class ExportWorker(QThread):
    finished = pyqtSignal(str)   # output path
    error = pyqtSignal(str)      # error message

    def __init__(self, input_path: str, start: float, output_path: str,
                 short_side: int | None = None,
                 portrait_ratio: str | None = None,
                 crop_center: float = 0.5):
        super().__init__()
        self._input = input_path
        self._start = start
        self._output = output_path
        self._short_side = short_side
        self._portrait_ratio = portrait_ratio
        self._crop_center = crop_center

    def run(self):
        cmd = build_ffmpeg_command(
            self._input, self._start, self._output,
            short_side=self._short_side,
            portrait_ratio=self._portrait_ratio,
            crop_center=self._crop_center,
        )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                self.finished.emit(self._output)
            else:
                self.error.emit(result.stderr[-500:])
        except FileNotFoundError:
            self.error.emit("ffmpeg not found — is it installed and on PATH?")
        except Exception as e:
            self.error.emit(str(e))
```

**Step 4: Add portrait state + widgets in `MainWindow.__init__`**

In `MainWindow.__init__`, after the `self._settings` / `self._txt_resize` block (around line 468), add:

```python
        self._crop_center: float = float(
            self._settings.value("crop_center", "0.5")
        )

        self._cmb_portrait = QComboBox()
        self._cmb_portrait.addItems(["Off", "9:16", "4:5", "1:1"])
        saved_ratio = self._settings.value("portrait_ratio", "Off")
        idx = self._cmb_portrait.findText(saved_ratio)
        self._cmb_portrait.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_portrait.currentTextChanged.connect(self._on_portrait_ratio_changed)

        self._crop_bar = CropBarWidget()
        self._crop_bar.set_crop_center(self._crop_center)
        self._crop_bar.set_portrait_ratio(
            None if saved_ratio == "Off" else saved_ratio
        )
        self._crop_bar.crop_changed.connect(self._on_crop_click)
        self._mpv.crop_clicked.connect(self._on_crop_click)
```

**Step 5: Add portrait combo to export row and crop bar to layout**

Replace the current `export_row` block and the right-side layout block:

```python
        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Name:"))
        export_row.addWidget(self._txt_name)
        export_row.addWidget(QLabel("Folder:"))
        export_row.addWidget(self._txt_folder, stretch=1)
        export_row.addWidget(self._btn_folder)
        export_row.addWidget(QLabel("Short side:"))
        export_row.addWidget(self._txt_resize)
        export_row.addWidget(QLabel("Portrait:"))
        export_row.addWidget(self._cmb_portrait)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(top_bar)
        right_layout.addWidget(self._mpv, stretch=1)
        right_layout.addWidget(self._timeline)
        right_layout.addWidget(self._crop_bar)
        right_layout.addLayout(controls)
        right_layout.addLayout(export_row)
```

**Step 6: Add `_on_portrait_ratio_changed` and `_on_crop_click` methods**

Add these two methods after `_refresh_markers` (after line 554):

```python
    def _on_portrait_ratio_changed(self, text: str) -> None:
        ratio = None if text == "Off" else text
        self._crop_bar.set_portrait_ratio(ratio)
        self._crop_bar.setVisible(ratio is not None)
        self._settings.setValue("portrait_ratio", text)

    def _on_crop_click(self, frac: float) -> None:
        ratio = self._cmb_portrait.currentText()
        if ratio == "Off":
            return
        self._crop_center = max(0.0, min(1.0, frac))
        self._settings.setValue("crop_center", str(self._crop_center))
        self._crop_bar.set_crop_center(self._crop_center)
```

**Step 7: Update `_after_load` to set source ratio on crop bar**

Add `self._crop_bar.set_source_ratio(*self._mpv.get_video_size())` at the end of `_after_load`, just before `self._refresh_markers()`:

```python
    def _after_load(self):
        dur = self._mpv.get_duration()
        self._timeline.set_duration(dur)
        self._cursor = 0.0
        self._lbl_duration.setText(f"dur: {format_time(dur)}")
        self._lbl_cursor.setText(f"cursor: {format_time(0.0)}")
        self._btn_play.setEnabled(True)
        self._btn_pause.setEnabled(True)
        self._btn_export.setEnabled(True)

        match = self._db.find_similar(os.path.basename(self._file_path))
        if match:
            self.statusBar().showMessage(f"⚠ Similar to already processed: {match}")
        else:
            self.statusBar().clearMessage()

        self._crop_bar.set_source_ratio(*self._mpv.get_video_size())
        self._refresh_markers()
```

**Step 8: Update `_on_export` to pass portrait params**

Replace the `self._export_worker = ExportWorker(...)` call in `_on_export` with:

```python
        ratio_text = self._cmb_portrait.currentText()
        portrait_ratio = None if ratio_text == "Off" else ratio_text

        self._export_worker = ExportWorker(
            self._file_path, self._cursor, output,
            short_side=short_side,
            portrait_ratio=portrait_ratio,
            crop_center=self._crop_center,
        )
```

**Step 9: Initialize crop bar visibility**

At the end of `__init__`, after `self.setStatusBar(QStatusBar())`, add:

```python
        self._crop_bar.setVisible(saved_ratio != "Off")
```

**Step 10: Verify headless import**

```bash
cd /media/p5/8-cut && python -c "from main import MainWindow"
```

Expected: no output.

**Step 11: Run all tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all 23 tests pass.

**Step 12: Commit**

```bash
cd /media/p5/8-cut && git add main.py && git commit -m "feat: wire portrait crop into MainWindow"
```

---

### Manual smoke test

```bash
python /media/p5/8-cut/main.py
```

- Drop a landscape video (e.g. 16:9)
- Select "9:16" from Portrait dropdown → crop bar appears below timeline
- Click video or drag crop bar → highlighted region moves
- Export → verify output is portrait with `ffprobe -v error -select_streams v:0 -show_entries stream=width,height output.mp4`
- Set Short side to 256 + Portrait 9:16 → output should be portrait 144×256
- Select "Off" → crop bar hides, export is normal landscape
- Relaunch → portrait ratio and crop center are restored
