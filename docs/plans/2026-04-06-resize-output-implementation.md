# Resize Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a user-configurable short-side resize to every exported clip, persisted between relaunches.

**Architecture:** `build_ffmpeg_command` gains an optional `short_side: int | None` parameter that appends a `-vf scale=` filter. `MainWindow` gets a `QLineEdit` in the export row wired to `QSettings` for persistence; `_on_export` parses it and passes the value through to `ExportWorker`.

**Tech Stack:** Python built-ins, PyQt6 (`QSettings`, `QLineEdit`), ffmpeg `-vf scale` filter. No new dependencies.

---

### Task 1: Update `build_ffmpeg_command` (TDD)

**Files:**
- Modify: `main.py` — update `build_ffmpeg_command`
- Modify: `tests/test_utils.py` — update existing test, add new test

**Step 1: Write failing tests**

In `tests/test_utils.py`, the existing `test_ffmpeg_command` (line 29) tests the no-resize case. Rename it and add a resize test below it:

```python
def test_ffmpeg_command_no_resize():
    cmd = build_ffmpeg_command("/in/video.mp4", 12.5, "/out/clip_001.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert "-ss" in cmd
    assert str(12.5) in cmd
    assert "-t" in cmd
    assert "8" in cmd
    assert cmd[-1] == "/out/clip_001.mp4"
    assert "-vf" not in cmd

def test_ffmpeg_command_with_resize():
    cmd = build_ffmpeg_command("/in/video.mp4", 0.0, "/out/clip_001.mp4", short_side=256)
    assert "-vf" in cmd
    vf_value = cmd[cmd.index("-vf") + 1]
    assert "256" in vf_value
    assert "scale" in vf_value
    assert cmd[-1] == "/out/clip_001.mp4"
```

**Step 2: Run to verify they fail**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "ffmpeg" -v 2>&1 | tail -20
```

Expected: `test_ffmpeg_command` (old name) passes but the new tests don't exist yet — after renaming, `test_ffmpeg_command_no_resize` will fail because `-vf` is not in the command (the assert `"-vf" not in cmd` will pass, so actually both new tests should pass on the no-resize one, but `test_ffmpeg_command_with_resize` will fail with TypeError because `short_side` param doesn't exist). That's the expected failure.

**Step 3: Update `build_ffmpeg_command` in `main.py`**

Replace the function (lines 33-44) with:

```python
def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
) -> list[str]:
    # -ss before -i: fast input-seeking. Safe here because we always re-encode
    # (libx264/aac), so there is no keyframe-alignment issue from pre-input seek.
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
    ]
    if short_side is not None:
        # Scale so the shorter dimension equals short_side.
        # if(lt(iw,ih),...) → portrait: fix width; landscape: fix height.
        # -2 keeps aspect ratio with even-pixel rounding (libx264 requirement).
        scale = f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})'"
        cmd += ["-vf", scale]
    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    return cmd
```

**Step 4: Run ffmpeg tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "ffmpeg" -v 2>&1 | tail -20
```

Expected: both ffmpeg tests pass.

**Step 5: Run full suite**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests pass.

**Step 6: Commit**

```bash
cd /media/p5/8-cut && git add main.py tests/test_utils.py && git commit -m "feat: add short_side resize to build_ffmpeg_command"
```

---

### Task 2: Wire QSettings + UI into MainWindow

**Files:**
- Modify: `main.py` — add `QSettings` import, add `_txt_resize` widget, wire persistence, update `_on_export`

**Step 1: Add `QSettings` to imports**

In `main.py`, the QtCore import line is:
```python
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
```

Add `QSettings`:
```python
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QSettings
```

**Step 2: Add `_txt_resize` widget in `MainWindow.__init__`**

After `self._btn_folder = QPushButton("Browse")` (around line 448), add:

```python
        self._settings = QSettings("8cut", "8cut")
        self._txt_resize = QLineEdit()
        self._txt_resize.setPlaceholderText("px (opt.)")
        self._txt_resize.setMaximumWidth(70)
        self._txt_resize.setText(self._settings.value("resize_short_side", ""))
        self._txt_resize.textChanged.connect(
            lambda v: self._settings.setValue("resize_short_side", v)
        )
```

**Step 3: Add the resize field to the export row**

The export row currently (lines 468-475) is:
```python
        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Name:"))
        export_row.addWidget(self._txt_name)
        export_row.addWidget(QLabel("Folder:"))
        export_row.addWidget(self._txt_folder, stretch=1)
        export_row.addWidget(self._btn_folder)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)
```

Replace with:
```python
        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Name:"))
        export_row.addWidget(self._txt_name)
        export_row.addWidget(QLabel("Folder:"))
        export_row.addWidget(self._txt_folder, stretch=1)
        export_row.addWidget(self._btn_folder)
        export_row.addWidget(QLabel("Short side:"))
        export_row.addWidget(self._txt_resize)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)
```

**Step 4: Update `_on_export` to parse the resize field and pass `short_side`**

The current `_on_export` (line 571) creates `ExportWorker` like this:
```python
        self._export_worker = ExportWorker(self._file_path, self._cursor, output)
```

`ExportWorker.__init__` calls `build_ffmpeg_command(input_path, start, output_path)` internally — wait, actually check how ExportWorker works. Read the file to confirm.

`ExportWorker.run` calls `build_ffmpeg_command(self._input, self._start, self._output)`. So we need to pass `short_side` through `ExportWorker` too.

Update `ExportWorker.__init__` to accept `short_side: int | None = None` and store it, then pass it to `build_ffmpeg_command` in `run`:

```python
class ExportWorker(QThread):
    finished = pyqtSignal(str)   # output path
    error = pyqtSignal(str)      # error message

    def __init__(self, input_path: str, start: float, output_path: str,
                 short_side: int | None = None):
        super().__init__()
        self._input = input_path
        self._start = start
        self._output = output_path
        self._short_side = short_side

    def run(self):
        cmd = build_ffmpeg_command(self._input, self._start, self._output,
                                   self._short_side)
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

Then in `_on_export`, parse the resize field and pass it:

```python
    def _on_export(self):
        if not self._file_path:
            return
        if self._export_worker and self._export_worker.isRunning():
            self.statusBar().showMessage("Export already running…")
            return

        output = build_export_path(
            self._txt_folder.text(),
            self._txt_name.text() or "clip",
            self._export_counter,
        )

        raw = self._txt_resize.text().strip()
        try:
            short_side = int(raw) if raw else None
            if short_side is not None and short_side <= 0:
                short_side = None
        except ValueError:
            short_side = None

        self._btn_export.setEnabled(False)
        self.statusBar().showMessage(f"Exporting {os.path.basename(output)}…")

        self._export_worker = ExportWorker(self._file_path, self._cursor, output,
                                           short_side)
        self._export_worker.finished.connect(self._on_export_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.start()
```

**Step 5: Verify headless import**

```bash
cd /media/p5/8-cut && python -c "from main import MainWindow"
```

Expected: no output.

**Step 6: Run all tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests pass.

**Step 7: Commit**

```bash
cd /media/p5/8-cut && git add main.py && git commit -m "feat: resize short-side field with QSettings persistence"
```

---

### Manual smoke test

```bash
python /media/p5/8-cut/main.py
```

- Leave "Short side" blank → export at native resolution
- Type `256` in "Short side" → export; verify output with `ffprobe output.mp4` that the shorter dimension is 256
- Relaunch the app → `256` is still in the field
- Type garbage (`abc`) → export still works, no resize applied
