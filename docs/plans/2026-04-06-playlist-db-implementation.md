# Playlist & Processed-Files Database Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a file queue (playlist) that auto-advances after each export, and a SQLite database that warns when a newly loaded file is fuzzy-similar to one already processed.

**Architecture:** `ProcessedDB` wraps `sqlite3` with fuzzy matching via `difflib.SequenceMatcher` on normalized filenames (resolution/quality tags stripped). `PlaylistWidget` is a `QListWidget` subclass that owns the queue and emits `file_selected` on advance or click. `MainWindow` is wired to use both.

**Tech Stack:** Python built-ins only — `sqlite3`, `difflib`, `re`, `datetime`. No new dependencies.

---

### Task 1: `_normalize_filename` and `ProcessedDB` (TDD)

**Files:**
- Modify: `main.py` — add `_normalize_filename`, `ProcessedDB`
- Modify: `tests/test_utils.py` — add DB and normalization tests

**Step 1: Add imports at top of main.py**

Add to the existing imports:

```python
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
```

**Step 2: Write failing tests**

Add to `tests/test_utils.py`:

```python
import tempfile, os
from main import _normalize_filename, ProcessedDB


# --- _normalize_filename ---

def test_normalize_strips_extension():
    assert _normalize_filename("clip.mp4") == "clip"

def test_normalize_strips_resolution():
    assert _normalize_filename("clip_2160p.mp4") == "clip"

def test_normalize_strips_1080p():
    assert _normalize_filename("clip_1080p.mkv") == "clip"

def test_normalize_strips_multiple_tags():
    assert _normalize_filename("show_1080p_HDR.mkv") == "show"

def test_normalize_lowercases():
    assert _normalize_filename("MyVideo_4K.mp4") == "myvideo"

def test_normalize_collapses_separators():
    assert _normalize_filename("my__video--2160p.mp4") == "my_video"


# --- ProcessedDB ---

def test_db_add_and_find_exact():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4")
        assert db.find_similar("video.mp4") == "video.mp4"
    finally:
        os.unlink(path)

def test_db_find_similar_resolution_variant():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("episode_s01e01_2160p.mkv")
        assert db.find_similar("episode_s01e01_1080p.mkv") == "episode_s01e01_2160p.mkv"
    finally:
        os.unlink(path)

def test_db_find_similar_no_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("alpha.mp4")
        assert db.find_similar("completely_different_zzzz.mp4") is None
    finally:
        os.unlink(path)

def test_db_disabled_survives_bad_path():
    db = ProcessedDB("/no/such/directory/8cut.db")
    db.add("x.mp4")           # must not raise
    assert db.find_similar("x.mp4") is None   # gracefully returns None
```

**Step 3: Run tests to verify they fail**

```bash
pytest tests/test_utils.py -k "normalize or db" -v
```

Expected: ImportError — functions not defined yet.

**Step 4: Add `_normalize_filename` to main.py**

Add after the existing imports, before `build_export_path`:

```python
def _normalize_filename(filename: str) -> str:
    """Strip extension and common resolution/quality tags for fuzzy comparison."""
    name = os.path.splitext(filename)[0].lower()
    name = re.sub(
        r'\b(2160p?|4k|8k|1080p?|720p?|480p?|360p?|240p?'
        r'|hdr|sdr|x264|x265|h264|h265|hevc|avc'
        r'|blu[-_.]?ray|webrip|web[-_.]dl|dvdrip|hdtv)\b',
        '', name, flags=re.IGNORECASE,
    )
    name = re.sub(r'[\s_\-\.]+', '_', name).strip('_')
    return name
```

**Step 5: Add `ProcessedDB` to main.py**

Add after `_normalize_filename`:

```python
class ProcessedDB:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".8cut.db")
        try:
            self._con = sqlite3.connect(db_path)
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS processed "
                "(filename TEXT NOT NULL, processed_at TEXT NOT NULL)"
            )
            self._con.commit()
            self._enabled = True
        except Exception as e:
            print(f"8-cut: DB unavailable: {e}", file=sys.stderr)
            self._con = None
            self._enabled = False

    def add(self, filename: str) -> None:
        if not self._enabled:
            return
        self._con.execute(
            "INSERT INTO processed (filename, processed_at) VALUES (?, ?)",
            (filename, datetime.utcnow().isoformat()),
        )
        self._con.commit()

    def find_similar(self, filename: str) -> str | None:
        if not self._enabled:
            return None
        rows = self._con.execute(
            "SELECT DISTINCT filename FROM processed"
        ).fetchall()
        norm_new = _normalize_filename(filename)
        best_ratio, best_match = 0.0, None
        for (stored,) in rows:
            ratio = SequenceMatcher(
                None, norm_new, _normalize_filename(stored)
            ).ratio()
            if ratio >= 0.75 and ratio > best_ratio:
                best_ratio, best_match = ratio, stored
        return best_match
```

**Step 6: Run tests to verify they pass**

```bash
pytest tests/test_utils.py -k "normalize or db" -v
```

Expected: all 10 new tests PASS (plus existing 8 = 18 total).

**Step 7: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: ProcessedDB and _normalize_filename with tests"
```

---

### Task 2: `PlaylistWidget`

**Files:**
- Modify: `main.py` — add `PlaylistWidget` class

**Step 1: Add PlaylistWidget before MainWindow**

Add after `MpvWidget`, before `main()`:

```python
class PlaylistWidget(QListWidget):
    file_selected = pyqtSignal(str)  # emits full path of selected file

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setFixedWidth(200)
        self.setWordWrap(True)
        self._paths: list[str] = []
        self.itemClicked.connect(self._on_item_clicked)

    def add_files(self, paths: list[str]) -> None:
        """Append paths not already in queue; auto-select first if queue was empty."""
        was_empty = len(self._paths) == 0
        for path in paths:
            if path not in self._paths and os.path.isfile(path):
                self._paths.append(path)
                self.addItem(os.path.basename(path))
        if was_empty and self._paths:
            self._select(0)

    def advance(self) -> None:
        """Move to next item in queue. Does nothing if at end."""
        row = self.currentRow()
        if row < self.count() - 1:
            self._select(row + 1)

    def current_path(self) -> str | None:
        row = self.currentRow()
        return self._paths[row] if 0 <= row < len(self._paths) else None

    def _select(self, row: int) -> None:
        self.setCurrentRow(row)
        self._refresh_labels()
        self.file_selected.emit(self._paths[row])

    def _refresh_labels(self) -> None:
        current = self.currentRow()
        for i in range(self.count()):
            name = os.path.basename(self._paths[i])
            self.item(i).setText(f"▶ {name}" if i == current else name)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._select(self.row(item))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            u.toLocalFile() for u in event.mimeData().urls()
            if os.path.isfile(u.toLocalFile())
        ]
        if paths:
            self.add_files(paths)
```

**Step 2: Add missing imports**

`QListWidget`, `QListWidgetItem`, and `QAbstractItemView` need to be in the QtWidgets import line. Check which are missing and add them. The updated import line should be:

```python
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter,
)
```

(`QSplitter` is added now for use in Task 3.)

**Step 3: Verify headless import**

```bash
python -c "from main import PlaylistWidget"
```

Expected: no output.

**Step 4: Run all tests**

```bash
pytest tests/ -v
```

Expected: all 18 tests pass.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: PlaylistWidget with drop support and auto-advance"
```

---

### Task 3: Wire MainWindow

**Files:**
- Modify: `main.py` — update `MainWindow.__init__`, `_load_file`, `_after_load`, `_on_export_done`, layout

**Step 1: Update MainWindow.__init__**

Replace the `MainWindow.__init__` method entirely with the version below. Key changes:
- Add `self._db = ProcessedDB()` and `self._playlist = PlaylistWidget()`
- Connect `_playlist.file_selected` to `_load_file`
- Change the root layout to a horizontal split: playlist on left, existing content on right
- Remove `self.setAcceptDrops(True)` from MainWindow (playlist handles drops now)

```python
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("8-cut")
        self.resize(1100, 680)

        # Services
        self._db = ProcessedDB()

        # State
        self._file_path: str = ""
        self._cursor: float = 0.0
        self._export_counter: int = 1
        self._export_worker: ExportWorker | None = None

        # Widgets
        self._playlist = PlaylistWidget()
        self._playlist.file_selected.connect(self._load_file)

        self._mpv = MpvWidget()
        self._mpv.file_loaded.connect(self._after_load)
        self._timeline = TimelineWidget()
        self._timeline.cursor_changed.connect(self._on_cursor_changed)

        self._lbl_file = QLabel("Drop files onto the queue →")
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
        self._txt_folder.textChanged.connect(self._reset_counter)
        self._btn_folder = QPushButton("Browse")
        self._btn_folder.clicked.connect(self._pick_folder)

        self._lbl_next = QLabel()
        self._update_next_label()

        self._btn_export = QPushButton("Export")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._on_export)

        # Right-side layout (video + controls)
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

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(top_bar)
        right_layout.addWidget(self._mpv, stretch=1)
        right_layout.addWidget(self._timeline)
        right_layout.addLayout(controls)
        right_layout.addLayout(export_row)

        # Left: playlist label + list
        queue_label = QLabel("Queue")
        queue_label.setStyleSheet("color: #aaa; padding: 4px;")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(queue_label)
        left_layout.addWidget(self._playlist)

        # Root: horizontal split
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([200, 900])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())
```

**Step 2: Update `_load_file` — remove isfile guard (PlaylistWidget already filters)**

```python
    def _load_file(self, path: str):
        self._file_path = path
        self._lbl_file.setText(os.path.basename(path))
        self._mpv.load(path)
        # _after_load triggered by MpvWidget.file_loaded signal
```

**Step 3: Update `_after_load` — add DB similarity check**

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
```

**Step 4: Update `_on_export_done` — record to DB and advance queue**

```python
    def _on_export_done(self, path: str):
        self._db.add(os.path.basename(self._file_path))
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._playlist.advance()
```

**Step 5: Remove `dragEnterEvent` and `dropEvent` from MainWindow**

These methods are no longer needed on `MainWindow` — drops go directly to `PlaylistWidget`. Delete both methods.

**Step 6: Verify headless import**

```bash
python -c "from main import MainWindow"
```

Expected: no output.

**Step 7: Run all tests**

```bash
pytest tests/ -v
```

Expected: all 18 tests pass.

**Step 8: Manual smoke test**

```bash
python main.py
```

- Drop one or more video files onto the queue panel → they appear in the list
- First file loads automatically into the player
- Scrub, play, pause — all work as before
- Export → file saved, counter increments, next file in queue loads automatically
- Drop the same file again → `⚠ Similar to already processed:` appears in status bar

**Step 9: Commit**

```bash
git add main.py
git commit -m "feat: wire playlist and DB into MainWindow"
```
