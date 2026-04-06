# Timeline Markers Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show numbered markers on the timeline at positions where clips were previously extracted from the current source file, with a hover tooltip showing the output path.

**Architecture:** DB schema is migrated to store `start_time` and `output_path` per export row (dropping the old UNIQUE constraint). `TimelineWidget` gains `set_markers()`, draws red numbered lines in `paintEvent`, and shows `QToolTip` on hover. `MainWindow` feeds markers to the timeline on load and after each export.

**Tech Stack:** Python built-ins (`sqlite3`, `re`, `difflib`), PyQt6 (`QToolTip`, `QCursor`, `QFont`). No new dependencies.

---

### Task 1: DB schema migration and new methods (TDD)

**Files:**
- Modify: `main.py` — update `ProcessedDB.__init__`, `add`, add `get_markers`
- Modify: `tests/test_utils.py` — update existing DB tests, add marker tests

**Step 1: Write failing tests**

Replace the four existing `test_db_*` tests and add new ones in `tests/test_utils.py`:

```python
# --- ProcessedDB (updated) ---

def test_db_add_and_find_exact():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 12.5, "/out/clip_001.mp4")
        assert db.find_similar("video.mp4") == "video.mp4"
    finally:
        os.unlink(path)

def test_db_find_similar_resolution_variant():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("episode_s01e01_2160p.mkv", 0.0, "/out/ep_001.mp4")
        assert db.find_similar("episode_s01e01_1080p.mkv") == "episode_s01e01_2160p.mkv"
    finally:
        os.unlink(path)

def test_db_find_similar_no_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("alpha.mp4", 0.0, "/out/alpha_001.mp4")
        assert db.find_similar("completely_different_zzzz.mp4") is None
    finally:
        os.unlink(path)

def test_db_disabled_survives_bad_path():
    db = ProcessedDB("/no/such/directory/8cut.db")
    db.add("x.mp4", 0.0, "/out/x_001.mp4")   # must not raise
    assert db.find_similar("x.mp4") is None

def test_db_get_markers_returns_sorted():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("video.mp4", 30.0, "/out/clip_002.mp4")
        db.add("video.mp4", 10.0, "/out/clip_001.mp4")
        db.add("video.mp4", 50.0, "/out/clip_003.mp4")
        markers = db.get_markers("video.mp4")
        assert len(markers) == 3
        assert markers[0] == (10.0, 1, "/out/clip_001.mp4")
        assert markers[1] == (30.0, 2, "/out/clip_002.mp4")
        assert markers[2] == (50.0, 3, "/out/clip_003.mp4")
    finally:
        os.unlink(path)

def test_db_get_markers_fuzzy_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        db.add("show_2160p.mkv", 5.0, "/out/s_001.mp4")
        markers = db.get_markers("show_1080p.mkv")
        assert len(markers) == 1
        assert markers[0][0] == 5.0
        assert markers[0][2] == "/out/s_001.mp4"
    finally:
        os.unlink(path)

def test_db_get_markers_no_match():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = ProcessedDB(path)
        markers = db.get_markers("nothing.mp4")
        assert markers == []
    finally:
        os.unlink(path)

def test_db_get_markers_disabled():
    db = ProcessedDB("/no/such/directory/8cut.db")
    assert db.get_markers("x.mp4") == []
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_utils.py -k "db" -v
```

Expected: failures — `add` has wrong signature, `get_markers` doesn't exist.

**Step 3: Update `ProcessedDB` in main.py**

Replace the entire `ProcessedDB` class:

```python
class ProcessedDB:
    _SCHEMA_VERSION = 2  # bump when schema changes

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".8cut.db")
        try:
            self._con = sqlite3.connect(db_path)
            self._migrate()
            self._enabled = True
        except Exception as e:
            print(f"8-cut: DB unavailable: {e}", file=sys.stderr)
            self._con = None
            self._enabled = False

    def _migrate(self) -> None:
        """Create or recreate table if schema is outdated."""
        cols = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(processed)").fetchall()
        }
        needs_recreate = "start_time" not in cols or "output_path" not in cols
        if needs_recreate:
            self._con.execute("DROP TABLE IF EXISTS processed")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS processed ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename     TEXT    NOT NULL,"
            "  start_time   REAL    NOT NULL,"
            "  output_path  TEXT    NOT NULL,"
            "  processed_at TEXT    NOT NULL"
            ")"
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_filename ON processed(filename)"
        )
        self._con.commit()

    def add(self, filename: str, start_time: float, output_path: str) -> None:
        if not self._enabled:
            return
        self._con.execute(
            "INSERT INTO processed (filename, start_time, output_path, processed_at)"
            " VALUES (?, ?, ?, ?)",
            (filename, start_time, output_path, datetime.now(timezone.utc).isoformat()),
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

    def get_markers(self, filename: str) -> list[tuple[float, int, str]]:
        """Return [(start_time, marker_number, output_path), ...] for the best
        fuzzy match of filename, sorted by start_time. Empty list if no match."""
        if not self._enabled:
            return []
        match = self.find_similar(filename)
        if match is None:
            return []
        rows = self._con.execute(
            "SELECT start_time, output_path FROM processed"
            " WHERE filename = ? ORDER BY start_time",
            (match,),
        ).fetchall()
        return [(t, i + 1, p) for i, (t, p) in enumerate(rows)]
```

**Step 4: Run DB tests**

```bash
pytest tests/test_utils.py -k "db" -v
```

Expected: all 8 DB tests PASS.

**Step 5: Run full suite**

```bash
pytest tests/ -v
```

Expected: all tests pass (normalize tests + db tests = 14 DB/normalize tests + 8 original = 22 total — count may vary).

**Step 6: Commit**

```bash
git add main.py tests/test_utils.py
git commit -m "feat: DB schema v2 — store start_time and output_path, add get_markers"
```

---

### Task 2: TimelineWidget markers (paint + hover)

**Files:**
- Modify: `main.py` — update `TimelineWidget`

**Step 1: Add missing imports to main.py**

`QToolTip` and `QCursor` are needed. Add them to the existing imports:

```python
from PyQt6.QtWidgets import (
    ...existing..., QToolTip,
)
from PyQt6.QtGui import (
    ...existing..., QCursor, QFont,
)
```

**Step 2: Add `set_markers` and update `paintEvent` and `mouseMoveEvent`**

In `TimelineWidget`, add the `_markers` attribute in `__init__`:

```python
        self._markers: list[tuple[float, int, str]] = []
```

Add the `set_markers` method:

```python
    def set_markers(self, markers: list[tuple[float, int, str]]) -> None:
        """markers: list of (start_time, number, output_path)"""
        self._markers = markers
        self.update()
```

Replace `paintEvent` with:

```python
    def paintEvent(self, event):
        p = QPainter(self)
        try:
            w, h = self.width(), self.height()
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

            # Markers
            font = QFont()
            font.setPixelSize(9)
            p.setFont(font)
            marker_pen = QPen(QColor(220, 60, 60))
            marker_pen.setWidth(2)
            for (t, num, _path) in self._markers:
                if self._duration <= 0:
                    break
                mx = int(t / self._duration * w)
                p.setPen(marker_pen)
                p.drawLine(mx, 0, mx, h)
                p.setPen(QColor(255, 255, 255))
                p.drawText(mx + 2, 10, str(num))
        finally:
            p.end()
```

Replace `mouseMoveEvent` with:

```python
    def mouseMoveEvent(self, event):
        x = event.position().x()
        # Check marker hover (±4px)
        if self._duration > 0 and self._markers:
            w = self.width()
            for (t, _num, output_path) in self._markers:
                mx = t / self._duration * w
                if abs(x - mx) <= 4:
                    QToolTip.showText(QCursor.pos(), output_path, self)
                    if event.buttons():
                        self._seek(x)
                    return
        QToolTip.hideText()
        if event.buttons():
            self._seek(x)
```

**Step 3: Verify headless import**

```bash
python -c "from main import TimelineWidget"
```

Expected: no output.

**Step 4: Run all tests**

```bash
pytest tests/ -v
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: timeline markers with hover tooltip"
```

---

### Task 3: Wire MainWindow

**Files:**
- Modify: `main.py` — update `_after_load`, `_on_export_done`, add `_refresh_markers`

**Step 1: Add `_refresh_markers` helper to MainWindow**

Add this method after `_after_load`:

```python
    def _refresh_markers(self) -> None:
        markers = self._db.get_markers(os.path.basename(self._file_path))
        self._timeline.set_markers(markers)
```

**Step 2: Update `_after_load`**

Add `self._refresh_markers()` at the end of `_after_load`:

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

        self._refresh_markers()
```

**Step 3: Update `_on_export_done`**

Pass `start_time` and `output_path` to `db.add`, then refresh markers:

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

**Step 4: Verify headless import**

```bash
python -c "from main import MainWindow"
```

Expected: no output.

**Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all tests pass.

**Step 6: Manual smoke test**

```bash
python main.py
```

- Drop a video, set cursor, export → a red numbered marker `1` appears on the timeline at that position
- Export again at a different position → marker `2` appears
- Hover over a marker → tooltip shows the output file path
- Drop a resolution variant of the same video → markers from the original appear immediately

**Step 7: Commit**

```bash
git add main.py
git commit -m "feat: wire timeline markers into MainWindow"
```
