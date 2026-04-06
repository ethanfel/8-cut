import sys
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QFrame, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QDragEnterEvent, QDropEvent
import mpv


def build_export_path(folder: str, basename: str, counter: int) -> str:
    filename = f"{basename}_{counter:03d}.mp4"
    return os.path.join(folder, filename)


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    # Floor-truncate to 1 dp (not round) — prevents "X:60.0" rollover when
    # seconds is e.g. 59.95. This means display may lag true position by up to 0.1s.
    s = int(seconds % 60 * 10) / 10
    return f"{m}:{s:04.1f}"


def build_ffmpeg_command(input_path: str, start: float, output_path: str) -> list[str]:
    # -ss before -i: fast input-seeking. Safe here because we always re-encode
    # (libx264/aac), so there is no keyframe-alignment issue from pre-input seek.
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
        "-c:v", "libx264",
        "-c:a", "aac",
        output_path,
    ]


def _normalize_filename(filename: str) -> str:
    """Strip extension and common resolution/quality tags for fuzzy comparison."""
    name = os.path.splitext(filename)[0].lower()
    # Use lookaround assertions instead of \b: \b treats '_' as a word char,
    # so 'clip_2160p' would not form a word boundary before '2160p'.
    name = re.sub(
        r'(?<![a-z0-9])(2160p?|4k|8k|1080p?|720p?|480p?|360p?|240p?'
        r'|hdr|sdr|x264|x265|h264|h265|hevc|avc'
        r'|blu[-_.]?ray|webrip|web[-_.]dl|dvdrip|hdtv)(?![a-z0-9])',
        '', name, flags=re.IGNORECASE,
    )
    name = re.sub(r'[\s_\-\.]+', '_', name).strip('_')
    return name


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
        except FileNotFoundError:
            self.error.emit("ffmpeg not found — is it installed and on PATH?")
        except Exception as e:
            self.error.emit(str(e))


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
        try:
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
        finally:
            p.end()

    def mousePressEvent(self, event):
        self._seek(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons():
            self._seek(event.position().x())

    def _seek(self, x: float):
        t = self._pos_to_time(int(x))
        self.set_cursor(t)
        self.cursor_changed.emit(self._cursor)


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
            # Clamp b to duration so AB loop fires even on clips shorter than 8s.
            self._player["ab-loop-b"] = min(b, self._player.duration or b)
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


class PlaylistWidget(QListWidget):
    file_selected = pyqtSignal(str)  # emits full path of selected file

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setMinimumWidth(200)
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
        """Move to next item in queue. Does nothing if at end or nothing selected."""
        row = self.currentRow()
        if row >= 0 and row < self.count() - 1:
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


def main():
    # Force X11/XCB mode so mpv can embed via wid — Wayland uses a different
    # surface handle that mpv's wid parameter cannot accept.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #1e1e1e; color: #ddd; }
        QPushButton { background: #333; border: 1px solid #555; padding: 4px 10px; border-radius: 3px; }
        QPushButton:hover { background: #444; }
        QPushButton:disabled { color: #555; }
        QLineEdit { background: #2a2a2a; border: 1px solid #555; padding: 3px; border-radius: 3px; }
        QStatusBar { color: #aaa; }
        QListWidget { background: #252525; }
        QListWidget::item { padding: 4px; color: #ddd; }
        QListWidget::item:selected { background: #3a6ea8; color: #fff; }
    """)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


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

        # Left: queue label + playlist
        queue_label = QLabel("Queue")
        queue_label.setStyleSheet("color: #aaa; padding: 4px;")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(queue_label)
        left_layout.addWidget(self._playlist)

        # Root: horizontal splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([200, 900])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())

    def _load_file(self, path: str):
        self._file_path = path
        self._lbl_file.setText(os.path.basename(path))
        self._mpv.load(path)
        # _after_load triggered by MpvWidget.file_loaded signal

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
            self._txt_folder.setText(folder)  # textChanged fires _reset_counter

    def _reset_counter(self):
        # Counter resets to 1 when name or folder changes. ffmpeg's -y flag
        # will silently overwrite if the same name+folder is reused later.
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
        self._db.add(os.path.basename(self._file_path), self._cursor, path)
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._playlist.advance()

    def _on_export_error(self, msg: str):
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Export error: {msg}")


if __name__ == "__main__":
    main()
