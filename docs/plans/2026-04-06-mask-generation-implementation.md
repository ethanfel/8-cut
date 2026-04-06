# Mask Generation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-frame PNG mask generation (Depth Anything V2 and SAM2) via a dedicated ML venv, with a Settings dialog for installation and a Generate Masks button in the main window.

**Architecture:** Two standalone scripts in `tools/` run inside `~/.8cut/venv/` as subprocesses. `MaskWorker(QThread)` mirrors `ExportWorker` — streams stdout to status bar. `SetupWorker(QThread)` handles venv creation and pip install. `SettingsDialog(QDialog)` shows install status and streams setup output. MainWindow gains a mask row and stores the last exported path.

**Tech Stack:** PyQt6 (`QDialog`, `QPlainTextEdit`), `subprocess.Popen` for streaming output, `torch`, `transformers`, `opencv-python`, `segment-anything-2`. No new runtime dependencies for the main app.

---

### Task 1: `build_mask_output_dir` utility (TDD)

**Files:**
- Modify: `main.py` — add `build_mask_output_dir`
- Modify: `tests/test_utils.py` — add tests, update import line

**Step 1: Write failing tests**

In `tests/test_utils.py`, update the import line at the top:
```python
from main import build_export_path, format_time, build_ffmpeg_command, build_mask_output_dir
```

Then add at the end of the file:

```python
def test_mask_output_dir_basic():
    assert build_mask_output_dir("/out/clip_001.mp4") == "/out/clip_001_masks"

def test_mask_output_dir_mkv():
    assert build_mask_output_dir("/out/my_clip.mkv") == "/out/my_clip_masks"

def test_mask_output_dir_nested():
    assert build_mask_output_dir("/a/b/c/shot_042.mp4") == "/a/b/c/shot_042_masks"
```

**Step 2: Run to verify they fail**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "mask_output" -v 2>&1 | tail -10
```

Expected: ImportError — `build_mask_output_dir` not defined yet.

**Step 3: Add `build_mask_output_dir` to `main.py`**

After `build_ffmpeg_command` and before `_RATIOS` (around line 53), insert:

```python
def build_mask_output_dir(video_path: str) -> str:
    """Return path of mask output directory: <stem>_masks/ next to the video."""
    p = Path(video_path)
    return str(p.parent / f"{p.stem}_masks")
```

**Step 4: Run tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/test_utils.py -k "mask_output" -v 2>&1 | tail -10
```

Expected: all 3 pass.

**Step 5: Run full suite**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all 29 tests pass.

**Step 6: Commit**

```bash
cd /media/p5/8-cut && git add main.py tests/test_utils.py && git commit -m "feat: add build_mask_output_dir utility"
```

---

### Task 2: `tools/depth_masks.py`

**Files:**
- Create: `tools/depth_masks.py`

**Step 1: Create `tools/` directory and script**

Create `/media/p5/8-cut/tools/depth_masks.py` with this content:

```python
"""Depth Anything V2 mask generation script.

Usage:
    python tools/depth_masks.py --input video.mp4 --output masks_dir/

Outputs one binary PNG per frame: frame_0000.png, frame_0001.png, …
Foreground = white (255), background = black (0), via Otsu threshold on depth map.
Requires: torch, transformers, opencv-python, Pillow
"""
import argparse
import os
import sys

import cv2
import numpy as np
from PIL import Image
from transformers import pipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}", flush=True)

    pipe = pipeline(
        "depth-estimation",
        model="depth-anything/Depth-Anything-V2-Large-hf",
        device=device,
    )

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.input}", file=sys.stderr)
        sys.exit(1)

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # transformers pipeline expects PIL RGB image
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        result = pipe(pil_img)
        depth = np.array(result["depth"])  # float32 array

        # Normalise to 0–255
        d_min, d_max = depth.min(), depth.max()
        if d_max > d_min:
            depth_u8 = ((depth - d_min) / (d_max - d_min) * 255).astype(np.uint8)
        else:
            depth_u8 = np.zeros_like(depth, dtype=np.uint8)

        # Otsu threshold: closer objects (higher depth value) = foreground
        _, mask = cv2.threshold(depth_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        out_path = os.path.join(args.output, f"frame_{idx:04d}.png")
        cv2.imwrite(out_path, mask)

        idx += 1
        print(f"frame {idx}/{total}", flush=True)

    cap.release()
    print("done", flush=True)


if __name__ == "__main__":
    main()
```

**Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('/media/p5/8-cut/tools/depth_masks.py').read()); print('ok')"
```

Expected: `ok`

**Step 3: Run full test suite (should still pass)**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all 29 tests pass.

**Step 4: Commit**

```bash
cd /media/p5/8-cut && git add tools/depth_masks.py && git commit -m "feat: depth_masks.py script using Depth Anything V2"
```

---

### Task 3: `tools/sam_masks.py`

**Files:**
- Create: `tools/sam_masks.py`

**Step 1: Create `tools/sam_masks.py`**

Create `/media/p5/8-cut/tools/sam_masks.py` with this content:

```python
"""SAM2 mask generation script.

Usage:
    python tools/sam_masks.py --input video.mp4 --output masks_dir/

Outputs one binary PNG per frame: frame_0000.png, frame_0001.png, …
Uses center of first frame as positive point prompt, propagates across all frames.
Requires: torch, segment-anything-2, opencv-python
"""
import argparse
import os
import sys
import tempfile

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}", flush=True)

    # Extract frames to temp directory (SAM2 video predictor needs image files)
    with tempfile.TemporaryDirectory() as frame_dir:
        cap = cv2.VideoCapture(args.input)
        if not cap.isOpened():
            print(f"ERROR: cannot open {args.input}", file=sys.stderr)
            sys.exit(1)

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            cv2.imwrite(os.path.join(frame_dir, f"{idx:04d}.jpg"), frame)
            idx += 1
        cap.release()

        print(f"Extracted {idx} frames", flush=True)

        from sam2.build_sam import build_sam2_video_predictor

        predictor = build_sam2_video_predictor(
            "facebook/sam2-hiera-large",
            device=device,
        )

        with torch.inference_mode():
            state = predictor.init_state(video_path=frame_dir)

            # Center of first frame as positive point prompt
            cx, cy = width // 2, height // 2
            _, _, _ = predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=0,
                obj_id=1,
                points=np.array([[cx, cy]], dtype=np.float32),
                labels=np.array([1], dtype=np.int32),
            )

            for frame_idx, obj_ids, masks in predictor.propagate_in_video(state):
                # masks shape: (N_objects, H, W) bool tensor
                mask = masks[0].cpu().numpy().astype(np.uint8) * 255
                out_path = os.path.join(args.output, f"frame_{frame_idx:04d}.png")
                cv2.imwrite(out_path, mask)
                print(f"frame {frame_idx + 1}/{total}", flush=True)

    print("done", flush=True)


if __name__ == "__main__":
    main()
```

**Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('/media/p5/8-cut/tools/sam_masks.py').read()); print('ok')"
```

Expected: `ok`

**Step 3: Run full test suite**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all 29 tests pass.

**Step 4: Commit**

```bash
cd /media/p5/8-cut && git add tools/sam_masks.py && git commit -m "feat: sam_masks.py script using SAM2 video predictor"
```

---

### Task 4: `MaskWorker`, `SetupWorker`, `SettingsDialog` in `main.py`

**Files:**
- Modify: `main.py` — add imports, add 3 new classes before `MainWindow`

**Step 1: Add new QtWidgets imports**

Find:
```python
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox,
)
```

Replace with:
```python
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox, QDialog, QPlainTextEdit,
)
```

**Step 2: Add `_VENV_PYTHON` and `_TOOLS_DIR` constants**

After the existing `_RATIOS` dict (around line 70), add:

```python
_VENV_PYTHON = str(Path.home() / ".8cut" / "venv" / "bin" / "python")
_TOOLS_DIR = str(Path(__file__).parent / "tools")
```

**Step 3: Add `SetupWorker`, `MaskWorker`, `SettingsDialog` classes**

Insert these three classes in `main.py` immediately before `class MainWindow` (currently around line 544). Add them after the last existing class (`PlaylistWidget`):

```python
class SetupWorker(QThread):
    """Installs the ML venv. Streams output line-by-line via `line` signal."""
    line = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def run(self):
        venv_dir = str(Path.home() / ".8cut" / "venv")
        steps = [
            [sys.executable, "-m", "venv", venv_dir],
            [_VENV_PYTHON, "-m", "pip", "install", "--upgrade", "pip"],
            [
                _VENV_PYTHON, "-m", "pip", "install",
                "torch", "torchvision",
                "transformers",
                "opencv-python",
                "Pillow",
                "segment-anything-2",
            ],
        ]
        try:
            for cmd in steps:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:
                    self.line.emit(line.rstrip())
                proc.wait()
                if proc.returncode != 0:
                    self.error.emit(f"Step failed: {' '.join(cmd)}")
                    return
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class MaskWorker(QThread):
    """Runs a mask generation script as a subprocess inside the ML venv."""
    progress = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, script: str, input_path: str, output_dir: str):
        super().__init__()
        self._script = script
        self._input = input_path
        self._output = output_dir

    def run(self):
        cmd = [_VENV_PYTHON, self._script, "--input", self._input, "--output", self._output]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                self.progress.emit(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                self.finished.emit()
            else:
                self.error.emit(f"Script exited with code {proc.returncode}")
        except FileNotFoundError:
            self.error.emit(f"venv not found — install ML tools via Settings")
        except Exception as e:
            self.error.emit(str(e))


class SettingsDialog(QDialog):
    """Settings dialog: shows ML venv status and Install/Reinstall button."""

    venv_installed = pyqtSignal()  # emitted when install completes successfully

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        self._worker: SetupWorker | None = None

        status_text = "Installed" if Path(_VENV_PYTHON).exists() else "Not installed"
        self._lbl_status = QLabel(f"ML Tools: {status_text}")

        btn_label = "Reinstall" if Path(_VENV_PYTHON).exists() else "Install"
        self._btn_install = QPushButton(btn_label)
        self._btn_install.clicked.connect(self._on_install)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Install output will appear here…")

        top = QHBoxLayout()
        top.addWidget(self._lbl_status)
        top.addStretch()
        top.addWidget(self._btn_install)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._log)

    def _on_install(self):
        self._btn_install.setEnabled(False)
        self._log.clear()
        self._worker = SetupWorker()
        self._worker.line.connect(self._log.appendPlainText)
        self._worker.finished.connect(self._on_install_done)
        self._worker.error.connect(self._on_install_error)
        self._worker.start()

    def _on_install_done(self):
        self._lbl_status.setText("ML Tools: Installed")
        self._btn_install.setText("Reinstall")
        self._btn_install.setEnabled(True)
        self._log.appendPlainText("✓ Installation complete.")
        self.venv_installed.emit()

    def _on_install_error(self, msg: str):
        self._btn_install.setEnabled(True)
        self._log.appendPlainText(f"ERROR: {msg}")
```

**Step 4: Verify headless import**

```bash
cd /media/p5/8-cut && python -c "from main import SettingsDialog, MaskWorker, SetupWorker"
```

Expected: no output.

**Step 5: Run full test suite**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all 29 tests pass.

**Step 6: Commit**

```bash
cd /media/p5/8-cut && git add main.py && git commit -m "feat: MaskWorker, SetupWorker, SettingsDialog"
```

---

### Task 5: Wire MainWindow

**Files:**
- Modify: `main.py` — update `MainWindow`

**Step 1: Add `_last_export_path` state and mask widgets to `MainWindow.__init__`**

In `MainWindow.__init__`, in the `# State` block (after `self._export_worker`), add:

```python
        self._last_export_path: str = ""
        self._mask_worker: MaskWorker | None = None
```

After `self._btn_export` widget setup (after `self._btn_export.clicked.connect(self._on_export)`), add:

```python
        # Settings dialog
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.venv_installed.connect(self._on_venv_installed)

        self._btn_settings = QPushButton("Settings…")
        self._btn_settings.clicked.connect(self._settings_dialog.show)

        # Mask generation row
        self._cmb_mask = QComboBox()
        self._cmb_mask.addItems(["Depth Anything", "SAM"])
        self._btn_masks = QPushButton("Generate Masks")
        self._btn_masks.setEnabled(Path(_VENV_PYTHON).exists())
        self._btn_masks.clicked.connect(self._on_generate_masks)
```

**Step 2: Add Settings button to top_bar and mask row to layout**

Find the `top_bar` layout block:
```python
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._lbl_file, stretch=1)
```

Replace with:
```python
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._lbl_file, stretch=1)
        top_bar.addWidget(self._btn_settings)
```

Find the right_layout block ending:
```python
        right_layout.addLayout(controls)
        right_layout.addLayout(export_row)
```

Replace with:
```python
        mask_row = QHBoxLayout()
        mask_row.addWidget(QLabel("Masks:"))
        mask_row.addWidget(self._cmb_mask)
        mask_row.addWidget(self._btn_masks)
        mask_row.addStretch()

        right_layout.addLayout(controls)
        right_layout.addLayout(export_row)
        right_layout.addLayout(mask_row)
```

**Step 3: Store last export path in `_on_export_done`**

Find:
```python
    def _on_export_done(self, path: str):
        self._db.add(os.path.basename(self._file_path), self._cursor, path)
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._refresh_markers()
        self._playlist.advance()
```

Replace with:
```python
    def _on_export_done(self, path: str):
        self._db.add(os.path.basename(self._file_path), self._cursor, path)
        self._last_export_path = path
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._refresh_markers()
        self._playlist.advance()
```

**Step 4: Add `_on_venv_installed`, `_on_generate_masks`, `_on_masks_progress`, `_on_masks_done`, `_on_masks_error` methods**

Add these methods just before `if __name__ == "__main__":`:

```python
    def _on_venv_installed(self) -> None:
        self._btn_masks.setEnabled(True)

    def _on_generate_masks(self) -> None:
        if not self._last_export_path:
            self.statusBar().showMessage("No clip exported yet — export first.")
            return
        if self._mask_worker and self._mask_worker.isRunning():
            self.statusBar().showMessage("Mask generation already running…")
            return

        output_dir = build_mask_output_dir(self._last_export_path)
        os.makedirs(output_dir, exist_ok=True)

        method = self._cmb_mask.currentText()
        script = os.path.join(_TOOLS_DIR, "depth_masks.py" if method == "Depth Anything" else "sam_masks.py")

        self._btn_masks.setEnabled(False)
        self.statusBar().showMessage(f"Generating masks ({method})…")

        self._mask_worker = MaskWorker(script, self._last_export_path, output_dir)
        self._mask_worker.progress.connect(self._on_masks_progress)
        self._mask_worker.finished.connect(self._on_masks_done)
        self._mask_worker.error.connect(self._on_masks_error)
        self._mask_worker.start()

    def _on_masks_progress(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    def _on_masks_done(self) -> None:
        self._btn_masks.setEnabled(True)
        output_dir = build_mask_output_dir(self._last_export_path)
        self.statusBar().showMessage(f"Masks saved to {os.path.basename(output_dir)}/")

    def _on_masks_error(self, msg: str) -> None:
        self._btn_masks.setEnabled(True)
        self.statusBar().showMessage(f"Mask error: {msg}")
```

**Step 5: Verify headless import**

```bash
cd /media/p5/8-cut && python -c "from main import MainWindow"
```

Expected: no output.

**Step 6: Run all tests**

```bash
cd /media/p5/8-cut && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all 29 tests pass.

**Step 7: Commit**

```bash
cd /media/p5/8-cut && git add main.py && git commit -m "feat: wire mask generation and settings into MainWindow"
```

---

### Manual smoke test

```bash
python /media/p5/8-cut/main.py
```

- Click **Settings…** → dialog opens showing "Not installed"
- Click **Install** → pip output streams into the text area; button re-enables as "Reinstall" when done
- Export a clip → **Generate Masks** button enables
- Select "Depth Anything", click **Generate Masks** → status bar shows frame progress
- Check `<output_folder>/<clip_stem>_masks/` contains `frame_0000.png`, `frame_0001.png`, …
- Repeat with "SAM"
