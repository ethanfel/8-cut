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
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox, QDialog, QPlainTextEdit, QCheckBox,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QSettings
from PyQt6.QtGui import QPainter, QColor, QPen, QDragEnterEvent, QDropEvent, QCursor, QFont, QKeyEvent
import mpv


def build_export_path(folder: str, basename: str, counter: int) -> str:
    filename = f"{basename}_{counter:03d}.mp4"
    return os.path.join(folder, filename)


def build_sequence_dir(folder: str, basename: str, counter: int) -> str:
    return os.path.join(folder, f"{basename}_{counter:03d}")


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    # Floor-truncate to 1 dp (not round) — prevents "X:60.0" rollover when
    # seconds is e.g. 59.95. This means display may lag true position by up to 0.1s.
    s = int(seconds % 60 * 10) / 10
    return f"{m}:{s:04.1f}"


def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
    portrait_ratio: str | None = None,
    crop_center: float = 0.5,
    image_sequence: bool = False,
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

    if image_sequence:
        cmd += [
            "-an",
            "-c:v", "libwebp",
            "-lossless", "1",
            "-compression_level", "4",
            os.path.join(output_path, "frame_%04d.webp"),
        ]
    else:
        cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    return cmd


def build_audio_extract_command(input_path: str, start: float, sequence_dir: str) -> list[str]:
    """Return an ffmpeg command that extracts audio to <sequence_dir>.wav."""
    audio_path = sequence_dir + ".wav"
    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
        "-vn",
        "-c:a", "pcm_s16le",
        audio_path,
    ]


def build_mask_output_dir(video_path: str) -> str:
    """Return path of mask output directory: <stem>_masks/ next to the video."""
    p = Path(video_path)
    return str(p.parent / f"{p.stem}_masks")


_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "4:5":  (4, 5),
    "1:1":  (1, 1),
}

_VENV_PYTHON = str(Path.home() / ".8cut" / "venv" / "bin" / "python")
_TOOLS_DIR = str(Path(__file__).parent / "tools")


def _portrait_crop_filter(ratio: str, crop_center: float) -> str:
    """Return an ffmpeg crop= filter expression for the given portrait ratio.

    Uses ffmpeg expression syntax so source dimensions are resolved at runtime.
    Commas inside min()/max() are escaped with \\, to prevent ffmpeg's
    filtergraph parser from treating them as filter-chain separators.
    """
    num, den = _RATIOS[ratio]
    cw = f"ih*{num}/{den}"
    x = f"max(0\\,min((iw-{cw})*{crop_center}\\,iw-{cw}))"
    return f"crop={cw}:ih:{x}:0"


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

    def run(self):
        try:
            if self._image_sequence:
                os.makedirs(self._output, exist_ok=True)
            cmd = build_ffmpeg_command(
                self._input, self._start, self._output,
                short_side=self._short_side,
                portrait_ratio=self._portrait_ratio,
                crop_center=self._crop_center,
                image_sequence=self._image_sequence,
            )
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                if self._image_sequence:
                    audio_cmd = build_audio_extract_command(
                        self._input, self._start, self._output
                    )
                    subprocess.run(audio_cmd, capture_output=True, text=True, timeout=60)
                    # Audio extraction failure (e.g. no audio stream) is ignored —
                    # the frame sequence is the primary output.
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
        self._markers: list[tuple[float, int, str]] = []

    def set_duration(self, duration: float):
        self._duration = duration
        self._cursor = 0.0
        self.update()

    def set_cursor(self, seconds: float):
        self._cursor = max(0.0, min(seconds, max(0.0, self._duration - 8.0)))
        self.update()

    def set_markers(self, markers: list[tuple[float, int, str]]) -> None:
        """markers: list of (start_time, number, output_path)"""
        self._markers = markers
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

    def mousePressEvent(self, event):
        self._seek(event.position().x())

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

    def _seek(self, x: float):
        t = self._pos_to_time(int(x))
        self.set_cursor(t)
        self.cursor_changed.emit(self._cursor)


class MpvWidget(QFrame):
    file_loaded = pyqtSignal()   # emitted (on Qt thread) when a file is ready
    crop_clicked = pyqtSignal(float)  # x fraction 0–1 when user clicks video

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

    def get_video_size(self) -> tuple[int, int]:
        if self._player:
            return (self._player.width or 0, self._player.height or 0)
        return (0, 0)

    def get_fps(self) -> float:
        if self._player:
            return self._player.container_fps or 25.0
        return 25.0

    def is_playing(self) -> bool:
        return bool(self._player and not self._player.pause)

    def mousePressEvent(self, event):
        w = self.width()
        if w > 0:
            self.crop_clicked.emit(event.position().x() / w)

    def closeEvent(self, event):
        if self._player:
            self._player.terminate()
        super().closeEvent(event)


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
        portrait_ar = num / den
        return portrait_ar / self._source_ratio

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

            p.fillRect(x, 1, win_px, h - 2, QColor(80, 140, 220, 160))
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
            frac = (x - win_px / 2) / max_x
            frac = max(0.0, min(1.0, frac))
        self.set_crop_center(frac)
        self.crop_changed.emit(self._crop_center)


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
                for output_line in proc.stdout:
                    self.line.emit(output_line.rstrip())
                proc.wait()
                if proc.returncode != 0:
                    self.error.emit(f"Step failed: {' '.join(cmd[:3])}")
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
            self.error.emit("venv not found — install ML tools via Settings")
        except Exception as e:
            self.error.emit(str(e))


class SettingsDialog(QDialog):
    """Settings dialog: shows ML venv status and Install/Reinstall button."""

    venv_installed = pyqtSignal()  # emitted when install completes successfully
    masks_visibility_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        self._worker: SetupWorker | None = None
        self._qsettings = QSettings("8cut", "8cut")

        status_text = "Installed" if Path(_VENV_PYTHON).exists() else "Not installed"
        self._lbl_status = QLabel(f"ML Tools: {status_text}")

        btn_label = "Reinstall" if Path(_VENV_PYTHON).exists() else "Install"
        self._btn_install = QPushButton(btn_label)
        self._btn_install.clicked.connect(self._on_install)

        self._chk_masks = QCheckBox("Show mask generation row")
        show_masks = self._qsettings.value("show_masks_row", "true") == "true"
        self._chk_masks.setChecked(show_masks)
        self._chk_masks.toggled.connect(self._on_masks_toggled)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Install output will appear here…")

        top = QHBoxLayout()
        top.addWidget(self._lbl_status)
        top.addStretch()
        top.addWidget(self._btn_install)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._chk_masks)
        layout.addWidget(self._log)

    def _on_masks_toggled(self, checked: bool) -> None:
        self._qsettings.setValue("show_masks_row", "true" if checked else "false")
        self.masks_visibility_changed.emit(checked)

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
        self._last_export_path: str = ""
        self._mask_worker: MaskWorker | None = None

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

        self._settings = QSettings("8cut", "8cut")
        self._txt_resize = QLineEdit()
        self._txt_resize.setPlaceholderText("px (opt.)")
        self._txt_resize.setMaximumWidth(70)
        self._txt_resize.setText(self._settings.value("resize_short_side", ""))
        self._txt_resize.textChanged.connect(
            lambda v: self._settings.setValue("resize_short_side", v)
        )

        self._crop_center: float = float(
            self._settings.value("crop_center", "0.5")
        )

        self._cmb_portrait = QComboBox()
        self._cmb_portrait.addItems(["Off", "9:16", "4:5", "1:1"])
        saved_ratio = self._settings.value("portrait_ratio", "Off")
        idx = self._cmb_portrait.findText(saved_ratio)
        self._cmb_portrait.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_portrait.currentTextChanged.connect(self._on_portrait_ratio_changed)

        self._cmb_format = QComboBox()
        self._cmb_format.addItems(["MP4", "WebP sequence"])
        saved_fmt = self._settings.value("export_format", "MP4")
        idx = self._cmb_format.findText(saved_fmt)
        self._cmb_format.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_format.currentTextChanged.connect(
            lambda v: self._settings.setValue("export_format", v)
        )
        self._cmb_format.currentTextChanged.connect(self._update_next_label)

        self._crop_bar = CropBarWidget()
        self._crop_bar.set_crop_center(self._crop_center)
        self._crop_bar.set_portrait_ratio(
            None if saved_ratio == "Off" else saved_ratio
        )
        self._crop_bar.crop_changed.connect(self._on_crop_click)
        self._mpv.crop_clicked.connect(self._on_crop_click)

        self._lbl_next = QLabel()
        self._update_next_label()

        self._btn_export = QPushButton("Export")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._on_export)

        # Settings dialog
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.venv_installed.connect(self._on_venv_installed)
        self._settings_dialog.masks_visibility_changed.connect(self._on_masks_visibility_changed)

        self._btn_settings = QPushButton("Settings…")
        self._btn_settings.clicked.connect(self._settings_dialog.show)

        # Mask generation row
        self._cmb_mask = QComboBox()
        self._cmb_mask.addItems(["Depth Anything", "SAM"])
        self._btn_masks = QPushButton("Generate Masks")
        self._btn_masks.setEnabled(Path(_VENV_PYTHON).exists())
        self._btn_masks.clicked.connect(self._on_generate_masks)

        # Right-side layout (video + controls)
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._lbl_file, stretch=1)
        top_bar.addWidget(self._btn_settings)

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
        export_row.addWidget(QLabel("Short side:"))
        export_row.addWidget(self._txt_resize)
        export_row.addWidget(QLabel("Portrait:"))
        export_row.addWidget(self._cmb_portrait)
        export_row.addWidget(QLabel("Format:"))
        export_row.addWidget(self._cmb_format)
        export_row.addWidget(self._lbl_next)
        export_row.addWidget(self._btn_export)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(top_bar)
        right_layout.addWidget(self._mpv, stretch=1)
        right_layout.addWidget(self._timeline)
        right_layout.addWidget(self._crop_bar)

        self._mask_row_widget = QWidget()
        mask_row = QHBoxLayout(self._mask_row_widget)
        mask_row.setContentsMargins(0, 0, 0, 0)
        mask_row.addWidget(QLabel("Masks:"))
        mask_row.addWidget(self._cmb_mask)
        mask_row.addWidget(self._btn_masks)
        mask_row.addStretch()
        show_masks = QSettings("8cut", "8cut").value("show_masks_row", "true") == "true"
        self._mask_row_widget.setVisible(show_masks)

        right_layout.addLayout(controls)
        right_layout.addLayout(export_row)
        right_layout.addWidget(self._mask_row_widget)

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
        self._crop_bar.setVisible(saved_ratio != "Off")

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

        self._crop_bar.set_source_ratio(*self._mpv.get_video_size())
        self._refresh_markers()

    def _refresh_markers(self) -> None:
        markers = self._db.get_markers(os.path.basename(self._file_path))
        self._timeline.set_markers(markers)

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

    def _step_cursor(self, delta: float) -> None:
        if not self._file_path:
            return
        dur = self._mpv.get_duration()
        new_t = max(0.0, min(self._cursor + delta, max(0.0, dur - 8.0)))
        self._timeline.set_cursor(new_t)
        self._on_cursor_changed(new_t)

    def _jump_to_next_marker(self) -> None:
        markers = sorted(self._timeline._markers, key=lambda m: m[0])
        if not markers:
            return
        for (t, _num, _path) in markers:
            if t > self._cursor + 0.1:
                self._step_cursor(t - self._cursor)
                return
        self._step_cursor(markers[0][0] - self._cursor)  # wrap to first

    def keyPressEvent(self, event: QKeyEvent) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QPlainTextEdit)):
            super().keyPressEvent(event)
            return

        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        frame = 1.0 / self._mpv.get_fps()
        step = 1.0 if shift else frame

        if key in (Qt.Key.Key_Left, Qt.Key.Key_J):
            self._step_cursor(-step)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self._step_cursor(step)
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_P):
            if self._mpv.is_playing():
                self._on_pause()
            else:
                self._on_play()
        elif key == Qt.Key.Key_K:
            self._on_pause()
        elif key == Qt.Key.Key_E:
            self._on_export()
        elif key == Qt.Key.Key_M:
            self._jump_to_next_marker()
        else:
            super().keyPressEvent(event)

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
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        if self._cmb_format.currentText() == "WebP sequence":
            path = build_sequence_dir(folder, name, self._export_counter)
        else:
            path = build_export_path(folder, name, self._export_counter)
        self._lbl_next.setText(f"→ {os.path.basename(path)}")

    def _on_export(self):
        if not self._file_path:
            return
        if self._export_worker and self._export_worker.isRunning():
            self.statusBar().showMessage("Export already running…")
            return

        fmt = self._cmb_format.currentText()
        image_sequence = fmt == "WebP sequence"
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        if image_sequence:
            output = build_sequence_dir(folder, name, self._export_counter)
        else:
            output = build_export_path(folder, name, self._export_counter)

        raw = self._txt_resize.text().strip()
        try:
            short_side = int(raw) if raw else None
            if short_side is not None and short_side <= 0:
                short_side = None
        except ValueError:
            short_side = None

        self._btn_export.setEnabled(False)
        self.statusBar().showMessage(f"Exporting {os.path.basename(output)}…")

        ratio_text = self._cmb_portrait.currentText()
        portrait_ratio = None if ratio_text == "Off" else ratio_text

        self._export_worker = ExportWorker(
            self._file_path, self._cursor, output,
            short_side=short_side,
            portrait_ratio=portrait_ratio,
            crop_center=self._crop_center,
            image_sequence=image_sequence,
        )
        self._export_worker.finished.connect(self._on_export_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.start()

    def _on_export_done(self, path: str):
        self._db.add(os.path.basename(self._file_path), self._cursor, path)
        # For MP4 exports path is a file; for WebP sequence it is a directory.
        # build_mask_output_dir handles both correctly via Path.stem.
        self._last_export_path = path
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._refresh_markers()
        self._playlist.advance()

    def _on_export_error(self, msg: str):
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Export error: {msg}")

    # --- Mask generation ---

    def _on_venv_installed(self) -> None:
        self._btn_masks.setEnabled(True)

    def _on_masks_visibility_changed(self, visible: bool) -> None:
        self._mask_row_widget.setVisible(visible)

    def _on_generate_masks(self) -> None:
        if not self._last_export_path:
            self.statusBar().showMessage("No clip exported yet — export first.")
            return
        if os.path.isdir(self._last_export_path):
            self.statusBar().showMessage("Mask generation requires an MP4 export — switch format to MP4 and export first.")
            return
        if self._mask_worker and self._mask_worker.isRunning():
            self.statusBar().showMessage("Mask generation already running…")
            return

        output_dir = build_mask_output_dir(self._last_export_path)
        os.makedirs(output_dir, exist_ok=True)

        method = self._cmb_mask.currentText()
        script = os.path.join(
            _TOOLS_DIR,
            "depth_masks.py" if method == "Depth Anything" else "sam_masks.py",
        )

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


if __name__ == "__main__":
    main()
