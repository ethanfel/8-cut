# Image Sequence Export Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add lossless WebP image sequence export as an alternative to MP4 via a format dropdown next to the Export button.

**Architecture:** `build_ffmpeg_command` gains an `image_sequence` flag that swaps the codec to libwebp lossless and uses a `frame_%04d.webp` output pattern. A new `build_sequence_dir` function mirrors `build_export_path` but returns a directory path. `ExportWorker` creates the directory before running ffmpeg when in sequence mode. A `QComboBox` in the export row selects the format; selection is persisted via QSettings.

**Tech Stack:** PyQt6, ffmpeg (libwebp), Python 3.11+

---

### Task 1: Add `build_sequence_dir` and extend `build_ffmpeg_command`

**Files:**
- Modify: `main.py:21-63`
- Test: `tests/test_utils.py`

**Step 1: Write the failing tests**

Add to `tests/test_utils.py`:

```python
from main import build_sequence_dir

def test_build_sequence_dir_basic():
    assert build_sequence_dir("/out", "clip", 1) == "/out/clip_001"

def test_build_sequence_dir_counter():
    assert build_sequence_dir("/out", "clip", 42) == "/out/clip_042"

def test_ffmpeg_command_image_sequence():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True)
    assert "-vcodec" in cmd
    assert "libwebp" in cmd[cmd.index("-vcodec") + 1]
    assert "-lossless" in cmd
    assert "1" in cmd[cmd.index("-lossless") + 1]
    assert "-compression_level" in cmd
    assert "4" in cmd[cmd.index("-compression_level") + 1]
    assert cmd[-1] == "/out/seq_001/frame_%04d.webp"

def test_ffmpeg_command_image_sequence_with_resize():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True, short_side=256)
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale" in vf
    assert cmd[-1] == "/out/seq_001/frame_%04d.webp"

def test_ffmpeg_command_image_sequence_no_audio():
    cmd = build_ffmpeg_command("/in/v.mp4", 0.0, "/out/seq_001", image_sequence=True)
    assert "-c:a" not in cmd
    assert "aac" not in cmd
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_utils.py::test_build_sequence_dir_basic tests/test_utils.py::test_ffmpeg_command_image_sequence -v
```

Expected: FAIL with `ImportError: cannot import name 'build_sequence_dir'`

**Step 3: Implement `build_sequence_dir` (add after `build_export_path` at line 24)**

```python
def build_sequence_dir(folder: str, basename: str, counter: int) -> str:
    return os.path.join(folder, f"{basename}_{counter:03d}")
```

**Step 4: Extend `build_ffmpeg_command` signature and body**

Replace lines 34-63 with:

```python
def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
    portrait_ratio: str | None = None,
    crop_center: float = 0.5,
    image_sequence: bool = False,
) -> list[str]:
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
        filters.append(
            f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})'"
        )
    if filters:
        cmd += ["-vf", ",".join(filters)]

    if image_sequence:
        cmd += [
            "-vcodec", "libwebp",
            "-lossless", "1",
            "-compression_level", "4",
            os.path.join(output_path, "frame_%04d.webp"),
        ]
    else:
        cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]

    return cmd
```

**Step 5: Run tests to verify they pass**

```bash
pytest tests/test_utils.py -v
```

Expected: all tests PASS (29 existing + 5 new = 34 total)

**Step 6: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: build_sequence_dir and image_sequence flag for build_ffmpeg_command"
```

---

### Task 2: Update `ExportWorker` for image sequence mode

**Files:**
- Modify: `main.py` — `ExportWorker` class (around lines 188-225)

**Step 1: Add `image_sequence` param to `ExportWorker.__init__` and `run`**

Find the `ExportWorker` class. Its `__init__` currently reads:

```python
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
```

Replace with:

```python
def __init__(self, input_path: str, start: float, output_path: str,
             short_side: int | None = None,
             portrait_ratio: str | None = None,
             crop_center: float = 0.5,
             image_sequence: bool = False):
    super().__init__()
    self._input = input_path
    self._start = start
    self._output = output_path
    self._short_side = short_side
    self._portrait_ratio = portrait_ratio
    self._crop_center = crop_center
    self._image_sequence = image_sequence
```

**Step 2: Update `ExportWorker.run`**

Find the `run` method in `ExportWorker`. It currently reads:

```python
def run(self):
    cmd = build_ffmpeg_command(
        self._input, self._start, self._output,
        short_side=self._short_side,
        portrait_ratio=self._portrait_ratio,
        crop_center=self._crop_center,
    )
```

Replace the body with:

```python
def run(self):
    if self._image_sequence:
        os.makedirs(self._output, exist_ok=True)
    cmd = build_ffmpeg_command(
        self._input, self._start, self._output,
        short_side=self._short_side,
        portrait_ratio=self._portrait_ratio,
        crop_center=self._crop_center,
        image_sequence=self._image_sequence,
    )
```

Leave the rest of `run` (subprocess Popen, emit finished/error) unchanged.

**Step 3: Run tests**

```bash
pytest tests/test_utils.py -v
```

Expected: 34 tests PASS (no regressions — ExportWorker is not unit-tested directly)

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: ExportWorker supports image_sequence mode"
```

---

### Task 3: Add format combo to MainWindow UI

**Files:**
- Modify: `main.py` — `MainWindow.__init__` widget creation and layout (around lines 756-816)

**Step 1: Add `_cmb_format` widget after `_cmb_portrait` setup**

Find the block that sets up `_cmb_portrait` (search for `self._cmb_portrait = QComboBox()`). Immediately after the portrait combo block (after `self._cmb_portrait.currentTextChanged.connect(...)`), add:

```python
self._cmb_format = QComboBox()
self._cmb_format.addItems(["MP4", "WebP sequence"])
saved_fmt = self._settings.value("export_format", "MP4")
fmt_idx = self._cmb_format.findText(saved_fmt)
self._cmb_format.setCurrentIndex(fmt_idx if fmt_idx >= 0 else 0)
self._cmb_format.currentTextChanged.connect(
    lambda v: self._settings.setValue("export_format", v)
)
self._cmb_format.currentTextChanged.connect(self._update_next_label)
```

**Step 2: Insert combo into the export_row layout**

Find the `export_row` layout block. It ends with:

```python
        export_row.addWidget(self._cmb_portrait)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)
```

Replace with:

```python
        export_row.addWidget(self._cmb_portrait)
        export_row.addWidget(QLabel("Format:"))
        export_row.addWidget(self._cmb_format)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)
```

**Step 3: Run the app briefly to verify UI**

```bash
python main.py
```

Expected: "Format:" label and MP4/WebP sequence dropdown appear left of the Export button.

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add format combo (MP4 / WebP sequence) to export row"
```

---

### Task 4: Wire format selection into `_update_next_label` and `_on_export`

**Files:**
- Modify: `main.py` — `_update_next_label`, `_on_export`, `_on_export_done` (around lines 933-986)

**Step 1: Update `_update_next_label` to show correct output path**

Find `_update_next_label`. It currently reads:

```python
def _update_next_label(self):
    path = build_export_path(
        self._txt_folder.text(),
        self._txt_name.text() or "clip",
        self._export_counter,
    )
    self._lbl_next.setText(f"→ {os.path.basename(path)}")
```

Replace with:

```python
def _update_next_label(self):
    folder = self._txt_folder.text()
    name = self._txt_name.text() or "clip"
    if self._cmb_format.currentText() == "WebP sequence":
        path = build_sequence_dir(folder, name, self._export_counter)
    else:
        path = build_export_path(folder, name, self._export_counter)
    self._lbl_next.setText(f"→ {os.path.basename(path)}")
```

**Step 2: Update `_on_export` to use the correct path builder and pass `image_sequence`**

Find `_on_export`. The section that builds `output` and creates `ExportWorker` currently reads:

```python
        output = build_export_path(
            self._txt_folder.text(),
            self._txt_name.text() or "clip",
            self._export_counter,
        )
        ...
        self._export_worker = ExportWorker(
            self._file_path, self._cursor, output,
            short_side=short_side,
            portrait_ratio=portrait_ratio,
            crop_center=self._crop_center,
        )
```

Replace those two blocks with:

```python
        fmt = self._cmb_format.currentText()
        image_sequence = fmt == "WebP sequence"
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        if image_sequence:
            output = build_sequence_dir(folder, name, self._export_counter)
        else:
            output = build_export_path(folder, name, self._export_counter)
        ...
        self._export_worker = ExportWorker(
            self._file_path, self._cursor, output,
            short_side=short_side,
            portrait_ratio=portrait_ratio,
            crop_center=self._crop_center,
            image_sequence=image_sequence,
        )
```

(Leave everything between those two blocks — short_side parsing, setEnabled, statusBar message — unchanged.)

**Step 3: Run tests**

```bash
pytest tests/test_utils.py -v
```

Expected: 34 tests PASS

**Step 4: Smoke-test the app**

```bash
python main.py
```

Drop a video, set format to "WebP sequence", export. Verify a `clip_001/` directory appears containing `frame_0000.webp`, `frame_0001.webp`, … in lossless WebP format.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: wire WebP sequence format into export flow"
```
