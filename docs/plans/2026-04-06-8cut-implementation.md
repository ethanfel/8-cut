# 8-cut Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Linux desktop tool to drop a video, scrub a timeline, and export exactly 8 seconds to an auto-numbered output file.

**Architecture:** Single `main.py` file with PyQt6 for the window/widgets, python-mpv embedded for playback with AB-loop preview, and ffmpeg subprocess in a QThread for non-blocking export. Pure logic (filename counter, ffmpeg command builder) is tested with pytest; GUI is verified manually.

**Tech Stack:** Python 3.10+, PyQt6, python-mpv, ffmpeg (system), pytest

---

### Task 1: Project setup

**Files:**
- Create: `main.py`
- Create: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/test_utils.py`

**Step 1: Install dependencies**

```bash
pip install PyQt6 python-mpv pytest
```

Verify mpv is on the system:
```bash
mpv --version
ffmpeg -version
```

**Step 2: Create requirements.txt**

```
PyQt6>=6.4
python-mpv>=1.0
pytest>=7.0
```

**Step 3: Create tests/__init__.py**

Empty file.

**Step 4: Create main.py skeleton**

```python
import sys
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtCore import Qt


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("8-cut")
        self.resize(900, 650)


if __name__ == "__main__":
    main()
```

**Step 5: Run to verify window opens**

```bash
python main.py
```

Expected: empty 900×650 window titled "8-cut" appears.

**Step 6: Init git and commit**

```bash
cd /media/p5/8-cut
git init
git add main.py requirements.txt tests/
git commit -m "feat: project skeleton"
```

---

### Task 2: Pure utility functions (TDD)

**Files:**
- Modify: `main.py` — add `build_export_path`, `format_time`
- Modify: `tests/test_utils.py`

**Step 1: Write failing tests**

```python
# tests/test_utils.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from main import build_export_path, format_time


def test_build_export_path_first():
    assert build_export_path("/out", "clip", 1) == "/out/clip_001.mp4"

def test_build_export_path_counter():
    assert build_export_path("/out", "clip", 42) == "/out/clip_042.mp4"

def test_build_export_path_deep_counter():
    assert build_export_path("/out", "shot", 999) == "/out/shot_999.mp4"

def test_format_time_seconds():
    assert format_time(0.0) == "0:00.0"

def test_format_time_minutes():
    assert format_time(75.3) == "1:15.3"

def test_format_time_rounding():
    assert format_time(61.05) == "1:01.0"
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_utils.py -v
```

Expected: `ImportError` or `AttributeError` — functions not yet defined.

**Step 3: Add functions to main.py**

Add after the imports:

```python
def build_export_path(folder: str, basename: str, counter: int) -> str:
    filename = f"{basename}_{counter:03d}.mp4"
    return os.path.join(folder, filename)


def format_time(seconds: float) -> str:
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m}:{s:04.1f}"
```

Also add `import os` at the top of main.py.

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_utils.py -v
```

Expected: all 6 tests PASS.

**Step 5: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: add utility functions with tests"
```

---

### Task 3: ExportWorker (QThread)

**Files:**
- Modify: `main.py` — add `ExportWorker` class
- Modify: `tests/test_utils.py` — add ffmpeg command test

**Step 1: Write failing test for command builder**

Add to `tests/test_utils.py`:

```python
from main import build_ffmpeg_command

def test_ffmpeg_command():
    cmd = build_ffmpeg_command("/in/video.mp4", 12.5, "/out/clip_001.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd
    assert str(12.5) in cmd
    assert "-t" in cmd
    assert "8" in cmd
    assert cmd[-1] == "/out/clip_001.mp4"
```

**Step 2: Run to verify it fails**

```bash
pytest tests/test_utils.py::test_ffmpeg_command -v
```

Expected: ImportError.

**Step 3: Add build_ffmpeg_command and ExportWorker to main.py**

Add after imports:

```python
import subprocess
from PyQt6.QtCore import QThread, pyqtSignal


def build_ffmpeg_command(input_path: str, start: float, output_path: str) -> list:
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
        "-c:v", "libx264",
        "-c:a", "aac",
        output_path,
    ]


class ExportWorker(QThread):
    finished = pyqtSignal(str)   # output path
    error = pyqtSignal(str)      # error message

    def __init__(self, input_path: str, start: float, output_path: str):
        super().__init__()
        self._input = input_path
        self._start = start
        self._output = output_path

    def run(self):
        cmd = build_ffmpeg_command(self._input, self._start, self._output)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                self.finished.emit(self._output)
            else:
                self.error.emit(result.stderr[-500:])
        except Exception as e:
            self.error.emit(str(e))
```

**Step 4: Run tests**

```bash
pytest tests/test_utils.py -v
```

Expected: all 7 tests PASS.

**Step 5: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: ExportWorker with ffmpeg command builder"
```

---

### Task 4: TimelineWidget

**Files:**
- Modify: `main.py` — add `TimelineWidget` class

**Step 1: Add TimelineWidget**

Add before `MainWindow`:

```python
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QPen
from PyQt6.QtCore import pyqtSignal


class TimelineWidget(QWidget):
    cursor_changed = pyqtSignal(float)  # emits position in seconds

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(40)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._cursor = 0.0

    def set_duration(self, duration: float):
        self._duration = duration
        self._cursor = 0.0
        self.update()

    def set_cursor(self, seconds: float):
        self._cursor = max(0.0, min(seconds, max(0.0, self._duration - 8.0)))
        self.update()

    def _pos_to_time(self, x: int) -> float:
        if self._duration <= 0 or self.width() <= 0:
            return 0.0
        ratio = max(0.0, min(1.0, x / self.width()))
        return ratio * self._duration

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, QColor(30, 30, 30))

        if self._duration <= 0:
            return

        # 8s selection highlight
        x_start = int(self._cursor / self._duration * w)
        x_end = int(min(self._cursor + 8.0, self._duration) / self._duration * w)
        p.fillRect(x_start, 0, x_end - x_start, h, QColor(60, 120, 200, 120))

        # Cursor line
        pen = QPen(QColor(255, 200, 0))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(x_start, 0, x_start, h)

    def mousePressEvent(self, event):
        self._seek(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons():
            self._seek(event.position().x())

    def _seek(self, x: float):
        t = self._pos_to_time(int(x))
        self.set_cursor(t)
        self.cursor_changed.emit(self._cursor)
```

**Step 2: Verify it renders — quick smoke test**

Temporarily add to `MainWindow.__init__`:

```python
from PyQt6.QtWidgets import QVBoxLayout, QWidget as QW
container = QW()
layout = QVBoxLayout(container)
self._timeline = TimelineWidget()
self._timeline.set_duration(60.0)
layout.addWidget(self._timeline)
self.setCentralWidget(container)
```

Run `python main.py` — you should see a dark bar. Click/drag on it to move the yellow cursor with blue highlight.

**Step 3: Remove the temporary test code from MainWindow**

Revert `MainWindow.__init__` to just `super().__init__()`, `setWindowTitle`, `resize`.

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: TimelineWidget with cursor and 8s highlight"
```

---

### Task 5: MpvWidget

**Files:**
- Modify: `main.py` — add `MpvWidget` class

**Step 1: Add MpvWidget**

Add before `MainWindow`:

```python
import mpv
from PyQt6.QtWidgets import QFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal


class MpvWidget(QFrame):
    file_loaded = pyqtSignal()  # emitted (on Qt thread) when a file is ready

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background: black;")
        # Required so Qt creates a real native window handle for mpv to embed into.
        # Without these, mpv opens a separate window instead of embedding.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self._player = None

    def _init_player(self):
        if self._player is not None:
            return
        self._player = mpv.MPV(
            wid=str(int(self.winId())),
            keep_open=True,
            pause=True,
        )
        # mpv fires events on its own thread; bounce to Qt thread via QTimer.
        @self._player.event_callback("file-loaded")
        def _on_file_loaded(event):
            QTimer.singleShot(0, self.file_loaded.emit)

    def load(self, path: str):
        self._init_player()
        self._player.play(path)

    def seek(self, t: float):
        if self._player:
            self._player.pause = True
            self._player.seek(t, "absolute")

    def play_loop(self, a: float, b: float):
        if self._player:
            self._player["ab-loop-a"] = a
            self._player["ab-loop-b"] = b
            self._player.pause = False

    def stop_loop(self):
        if self._player:
            # ab-loop-a/b are numeric properties — setting to "no" via dict
            # accessor throws TypeError. Disable loop via ab_loop_count instead.
            self._player.ab_loop_count = 0
            self._player.pause = True

    def get_duration(self) -> float:
        if self._player:
            d = self._player.duration
            return d if d else 0.0
        return 0.0

    def closeEvent(self, event):
        if self._player:
            self._player.terminate()
        super().closeEvent(event)
```

**Step 2: Smoke test**

Temporarily in `MainWindow.__init__`:

```python
from PyQt6.QtWidgets import QVBoxLayout, QWidget as QW
container = QW()
layout = QVBoxLayout(container)
self._mpv = MpvWidget()
layout.addWidget(self._mpv)
self.setCentralWidget(container)
# after show(), call: self._mpv.load("/path/to/any/video.mp4")
```

Run, load a real video path. Should display and be paused.

**Step 3: Remove temporary test code**

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: MpvWidget with seek and AB-loop"
```

---

### Task 6: MainWindow — full UI

**Files:**
- Modify: `main.py` — implement complete `MainWindow`

**Step 1: Add all imports at top of main.py**

```python
import sys
import os
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QDragEnterEvent, QDropEvent
import mpv
```

**Step 2: Replace MainWindow with full implementation**

```python
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("8-cut")
        self.resize(900, 680)
        self.setAcceptDrops(True)

        # State
        self._file_path: str = ""
        self._cursor: float = 0.0
        self._export_counter: int = 1
        self._export_worker: ExportWorker | None = None

        # Widgets
        self._mpv = MpvWidget()
        self._mpv.file_loaded.connect(self._after_load)
        self._timeline = TimelineWidget()
        self._timeline.cursor_changed.connect(self._on_cursor_changed)

        self._lbl_file = QLabel("Drop a video file here")
        self._lbl_file.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_file.setStyleSheet("color: #aaa; padding: 6px;")

        self._btn_play = QPushButton("▶ Play 8s")
        self._btn_play.setEnabled(False)
        self._btn_play.clicked.connect(self._on_play)

        self._btn_pause = QPushButton("⏸ Pause")
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause)

        self._lbl_cursor = QLabel("cursor: --")
        self._lbl_duration = QLabel("dur: --")

        self._txt_name = QLineEdit("clip")
        self._txt_name.setPlaceholderText("base name")
        self._txt_name.setMaximumWidth(150)
        self._txt_name.textChanged.connect(self._reset_counter)

        self._txt_folder = QLineEdit(str(Path.home()))
        self._btn_folder = QPushButton("Browse")
        self._btn_folder.clicked.connect(self._pick_folder)

        self._lbl_next = QLabel()
        self._update_next_label()

        self._btn_export = QPushButton("Export")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._on_export)

        # Layout
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._lbl_file, stretch=1)

        controls = QHBoxLayout()
        controls.addWidget(self._btn_play)
        controls.addWidget(self._btn_pause)
        controls.addStretch()
        controls.addWidget(self._lbl_cursor)
        controls.addWidget(self._lbl_duration)

        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Name:"))
        export_row.addWidget(self._txt_name)
        export_row.addWidget(QLabel("Folder:"))
        export_row.addWidget(self._txt_folder, stretch=1)
        export_row.addWidget(self._btn_folder)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)

        root = QVBoxLayout()
        root.addLayout(top_bar)
        root.addWidget(self._mpv, stretch=1)
        root.addWidget(self._timeline)
        root.addLayout(controls)
        root.addLayout(export_row)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())

    # --- Drag & Drop ---

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self._load_file(path)

    def _load_file(self, path: str):
        self._file_path = path
        self._lbl_file.setText(os.path.basename(path))
        self._mpv.load(path)
        # _after_load is triggered by MpvWidget.file_loaded signal (connected in __init__)

    def _after_load(self):
        dur = self._mpv.get_duration()
        self._timeline.set_duration(dur)
        self._cursor = 0.0
        self._lbl_duration.setText(f"dur: {format_time(dur)}")
        self._lbl_cursor.setText(f"cursor: {format_time(0.0)}")
        self._btn_play.setEnabled(True)
        self._btn_pause.setEnabled(True)
        self._btn_export.setEnabled(True)

    # --- Playback ---

    def _on_cursor_changed(self, t: float):
        self._cursor = t
        self._lbl_cursor.setText(f"cursor: {format_time(t)}")
        self._mpv.seek(t)

    def _on_play(self):
        if not self._file_path:
            return
        self._mpv.play_loop(self._cursor, self._cursor + 8.0)

    def _on_pause(self):
        self._mpv.stop_loop()
        self._mpv.seek(self._cursor)

    # --- Export ---

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self._txt_folder.setText(folder)
            self._reset_counter()

    def _reset_counter(self):
        self._export_counter = 1
        self._update_next_label()

    def _update_next_label(self):
        path = build_export_path(
            self._txt_folder.text(),
            self._txt_name.text() or "clip",
            self._export_counter,
        )
        self._lbl_next.setText(f"→ {os.path.basename(path)}")

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
        self._btn_export.setEnabled(False)
        self.statusBar().showMessage(f"Exporting {os.path.basename(output)}…")

        self._export_worker = ExportWorker(self._file_path, self._cursor, output)
        self._export_worker.finished.connect(self._on_export_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.start()

    def _on_export_done(self, path: str):
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")

    def _on_export_error(self, msg: str):
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Export error: {msg}")
```

**Step 3: Run the app**

```bash
python main.py
```

Expected:
- Window opens with dark drop zone
- Drop a video → preview appears, timeline shows duration
- Drag cursor → video seeks to that frame
- Click "▶ Play 8s" → 8-second loop plays with audio
- Click "⏸ Pause" → pauses and seeks back to cursor
- Click "Export" → exports clip_001.mp4 to home folder, counter becomes 2

**Step 4: Run all tests to confirm nothing broken**

```bash
pytest tests/ -v
```

Expected: all tests PASS.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: complete MainWindow UI with playback and export"
```

---

### Task 7: Final polish

**Files:**
- Modify: `main.py` — dark theme, minor UX

**Step 1: Add dark stylesheet to main()**

```python
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #1e1e1e; color: #ddd; }
        QPushButton { background: #333; border: 1px solid #555; padding: 4px 10px; border-radius: 3px; }
        QPushButton:hover { background: #444; }
        QPushButton:disabled { color: #555; }
        QLineEdit { background: #2a2a2a; border: 1px solid #555; padding: 3px; border-radius: 3px; }
        QStatusBar { color: #aaa; }
    """)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
```

**Step 2: Run and verify visuals**

```bash
python main.py
```

Drop a video, scrub, export. Everything should look clean and dark.

**Step 3: Final commit**

```bash
git add main.py
git commit -m "feat: dark theme, complete 8-cut tool"
```
