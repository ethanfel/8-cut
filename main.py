#!/usr/bin/env python3
import locale
locale.setlocale(locale.LC_NUMERIC, "C")  # required by libmpv before any import

import sys
import os
import re
import json
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
from PyQt6.QtGui import QPainter, QColor, QPen, QDragEnterEvent, QDropEvent, QCursor, QFont, QKeySequence, QShortcut
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
        "-threads", "0",
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
            f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})':flags=lanczos"
        )
    if filters:
        cmd += ["-vf", ",".join(filters)]

    if image_sequence:
        cmd += [
            "-an",
            "-c:v", "libwebp",
            "-quality", "92",
            "-compression_level", "1",
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


def build_annotation_json_path(folder: str) -> str:
    return os.path.join(folder, "dataset.json")


def upsert_clip_annotation(folder: str, clip_path: str, label: str) -> None:
    """Insert or update one entry in <folder>/dataset.json.

    Each entry stores a path relative to *folder* and the sound label.
    Matches on ``path``; if an entry for the same clip already exists it is
    replaced (overwrite-export case).  Nothing is written when *label* is
    empty.
    """
    if not label.strip():
        return
    os.makedirs(folder, exist_ok=True)
    json_path = build_annotation_json_path(folder)
    entries: list[dict] = []
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except (json.JSONDecodeError, ValueError):
                entries = []
    abs_path = os.path.abspath(clip_path)
    entry: dict = {"path": abs_path, "label": label}
    for i, e in enumerate(entries):
        if e.get("path") == abs_path:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")


def build_mask_output_dir(video_path: str) -> str:
    """Return path of mask output directory: <stem>_masks/ next to the video."""
    p = Path(video_path)
    return str(p.parent / f"{p.stem}_masks")


_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "4:5":  (4, 5),
    "1:1":  (1, 1),
}

_VENV_PYTHON = str(
    Path.home() / ".8cut" / "venv"
    / ("Scripts" if sys.platform == "win32" else "bin")
    / ("python.exe" if sys.platform == "win32" else "python")
)
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


_QUALITY_RE = re.compile(
    r'(?<![a-z0-9])(2160p?|4k|8k|1080p?|720p?|480p?|360p?|240p?'
    r'|hdr|sdr|x264|x265|h264|h265|hevc|avc'
    r'|blu[-_.]?ray|webrip|web[-_.]dl|dvdrip|hdtv)(?![a-z0-9])',
    re.IGNORECASE,
)
_SEP_RE = re.compile(r'[\s_\-\.]+')
_SELVA_CATEGORIES = ["", "Human", "Animal", "Vehicle", "Tool", "Music", "Nature", "Sport", "Other"]


def _normalize_filename(filename: str) -> str:
    """Strip extension and common resolution/quality tags for fuzzy comparison."""
    # Use lookaround assertions instead of \b: \b treats '_' as a word char,
    # so 'clip_2160p' would not form a word boundary before '2160p'.
    name = os.path.splitext(filename)[0].lower()
    name = _QUALITY_RE.sub('', name)
    name = _SEP_RE.sub('_', name).strip('_')
    return name


class ProcessedDB:
    _SCHEMA_VERSION = 3  # bump when schema changes

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".8cut.db")
        self._path = db_path
        try:
            self._con = sqlite3.connect(db_path, check_same_thread=False)
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
        needs_recreate = not {"start_time", "output_path", "label", "category"}.issubset(cols)
        if needs_recreate:
            self._con.execute("DROP TABLE IF EXISTS processed")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS processed ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  filename     TEXT    NOT NULL,"
            "  start_time   REAL    NOT NULL,"
            "  output_path  TEXT    NOT NULL,"
            "  label        TEXT    NOT NULL DEFAULT '',"
            "  category     TEXT    NOT NULL DEFAULT '',"
            "  processed_at TEXT    NOT NULL"
            ")"
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_filename ON processed(filename)"
        )
        self._con.commit()

    def add(self, filename: str, start_time: float, output_path: str,
            label: str = "", category: str = "") -> None:
        if not self._enabled:
            return
        self._con.execute(
            "INSERT INTO processed (filename, start_time, output_path, label, category, processed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (filename, start_time, output_path, label, category,
             datetime.now(timezone.utc).isoformat()),
        )
        self._con.commit()

    def get_labels(self) -> list[str]:
        """Return distinct non-empty labels ordered by most recently used."""
        if not self._enabled:
            return []
        rows = self._con.execute(
            "SELECT DISTINCT label FROM processed"
            " WHERE label != '' ORDER BY processed_at DESC"
        ).fetchall()
        # Deduplicate while preserving order (DISTINCT on processed_at DESC
        # may return duplicates if the same label was used multiple times).
        seen: set[str] = set()
        result = []
        for (lbl,) in rows:
            if lbl not in seen:
                seen.add(lbl)
                result.append(lbl)
        return result

    def delete_by_output_path(self, output_path: str) -> None:
        if not self._enabled:
            return
        self._con.execute("DELETE FROM processed WHERE output_path = ?", (output_path,))
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

    def _get_markers_for(self, match: str) -> list[tuple[float, int, str]]:
        rows = self._con.execute(
            "SELECT start_time, output_path FROM processed"
            " WHERE filename = ? ORDER BY start_time",
            (match,),
        ).fetchall()
        return [(t, i + 1, p) for i, (t, p) in enumerate(rows)]

    def get_markers(self, filename: str) -> list[tuple[float, int, str]]:
        """Return [(start_time, marker_number, output_path), ...] for the best
        fuzzy match of filename, sorted by start_time. Empty list if no match."""
        if not self._enabled:
            return []
        match = self.find_similar(filename)
        if match is None:
            return []
        return self._get_markers_for(match)


class _DBWorker(QThread):
    """Runs ProcessedDB fuzzy-match lookup off the main thread."""
    result = pyqtSignal(str, object, list)  # (queried_filename, match|None, markers)

    def __init__(self, db: "ProcessedDB", filename: str):
        super().__init__()
        self._db = db
        self._filename = filename

    def run(self):
        try:
            match = self._db.find_similar(self._filename)
            markers = self._db._get_markers_for(match) if match else []
        except Exception:
            match, markers = None, []
        self.result.emit(self._filename, match, markers)


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
    cursor_changed = pyqtSignal(float)              # emits position in seconds
    marker_delete_requested = pyqtSignal(str)       # emits output_path
    marker_clicked = pyqtSignal(float, str)         # emits (start_time, output_path)

    _RULER_H = 22   # pixels reserved for the time ruler
    _HANDLE_H = 8   # height of the playhead triangle

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(80)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._cursor = 0.0
        self._markers: list[tuple[float, int, str]] = []
        self._hover_cache: list[tuple[float, str]] = []  # (t/duration, path)

        # Cached paint resources — created once, reused every frame
        self._cursor_pen = QPen(QColor(255, 210, 0))
        self._cursor_pen.setWidth(2)
        self._marker_pen = QPen(QColor(220, 60, 60))
        self._marker_pen.setWidth(2)
        self._ruler_pen = QPen(QColor(120, 120, 120))
        self._ruler_pen.setWidth(1)
        self._marker_font = QFont()
        self._marker_font.setPixelSize(9)
        self._ruler_font = QFont()
        self._ruler_font.setPixelSize(9)

        # Debounce timer: update visual cursor immediately but only emit
        # cursor_changed (which triggers mpv.seek) at most once per interval.
        self._seek_timer = QTimer()
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(16)  # ~60 fps
        self._seek_timer.timeout.connect(lambda: self.cursor_changed.emit(self._cursor))

    def set_duration(self, duration: float):
        self._duration = duration
        self._cursor = 0.0
        self._rebuild_hover_cache()
        self.update()

    def set_cursor(self, seconds: float):
        clamped = max(0.0, min(seconds, max(0.0, self._duration - 8.0)))
        if clamped == self._cursor:
            return
        self._cursor = clamped
        self.update()

    def set_markers(self, markers: list[tuple[float, int, str]]) -> None:
        """markers: list of (start_time, number, output_path)"""
        self._markers = markers
        self._rebuild_hover_cache()
        self.update()

    def _rebuild_hover_cache(self) -> None:
        """Pre-compute (pixel_x_fraction, output_path) for hover detection."""
        if self._duration > 0:
            self._hover_cache = [
                (t / self._duration, path)
                for (t, _num, path) in self._markers
            ]
        else:
            self._hover_cache: list[tuple[float, str]] = []

    def _pos_to_time(self, x: int) -> float:
        if self._duration <= 0 or self.width() <= 0:
            return 0.0
        ratio = max(0.0, min(1.0, x / self.width()))
        return ratio * self._duration

    def paintEvent(self, event):
        from PyQt6.QtGui import QPolygon
        from PyQt6.QtCore import QPoint
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        try:
            w, h = self.width(), self.height()
            rh = self._RULER_H
            th = h - rh          # track height

            # ── backgrounds ──────────────────────────────────────────────
            p.fillRect(0, 0, w, rh, QColor(22, 22, 22))        # ruler bg
            p.fillRect(0, rh, w, th, QColor(32, 32, 32))       # track bg

            # subtle track lane (slightly raised strip in the middle)
            lane_y = rh + th // 4
            lane_h = th // 2
            p.fillRect(0, lane_y, w, lane_h, QColor(42, 42, 42))

            if self._duration <= 0:
                p.setPen(QColor(80, 80, 80))
                p.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, "No file loaded")
                return

            # ── time ruler ticks & labels ─────────────────────────────────
            # Pick a tick interval so we get ~8-12 major ticks across the width
            raw_step = self._duration / 10.0
            for candidate in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
                if candidate >= raw_step:
                    major_step = candidate
                    break
            else:
                major_step = int(raw_step / 60 + 1) * 60

            minor_step = major_step / 5.0
            p.setFont(self._ruler_font)

            t = 0.0
            while t <= self._duration + minor_step * 0.1:
                rx = int(t / self._duration * w)
                is_major = (round(t / major_step) * major_step - t) < minor_step * 0.1
                if is_major:
                    p.setPen(self._ruler_pen)
                    p.drawLine(rx, rh - 10, rx, rh)
                    # label
                    mins = int(t) // 60
                    secs = int(t) % 60
                    label = f"{mins}:{secs:02d}" if mins else f"{secs}s"
                    p.setPen(QColor(160, 160, 160))
                    p.drawText(rx + 3, 0, 60, rh - 2,
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                               label)
                else:
                    p.setPen(QPen(QColor(70, 70, 70)))
                    p.drawLine(rx, rh - 5, rx, rh)
                t += minor_step

            # ruler bottom border
            p.setPen(QPen(QColor(55, 55, 55)))
            p.drawLine(0, rh, w, rh)

            # ── 8-second selection region ─────────────────────────────────
            x_start = int(self._cursor / self._duration * w)
            x_end   = int(min(self._cursor + 8.0, self._duration) / self._duration * w)
            sel_w   = max(x_end - x_start, 1)
            p.fillRect(x_start, rh, sel_w, th, QColor(60, 130, 220, 90))
            # left/right edges of selection
            p.setPen(QPen(QColor(60, 130, 220, 180), 1))
            p.drawLine(x_start, rh, x_start, h)
            p.drawLine(x_end,   rh, x_end,   h)

            # ── export markers ────────────────────────────────────────────
            p.setFont(self._marker_font)
            for (t, num, _path) in self._markers:
                mx = int(t / self._duration * w)
                p.setPen(self._marker_pen)
                p.drawLine(mx, rh, mx, h)
                # small filled rectangle label
                p.fillRect(mx, rh + 2, 14, 12, QColor(200, 50, 50))
                p.setPen(QColor(255, 255, 255))
                p.drawText(mx + 1, rh + 2, 13, 12,
                           Qt.AlignmentFlag.AlignCenter, str(num))

            # ── playhead ──────────────────────────────────────────────────
            p.setPen(self._cursor_pen)
            p.drawLine(x_start, rh, x_start, h)
            # downward-pointing triangle handle in the ruler
            hh = self._HANDLE_H
            tri = QPolygon([
                QPoint(x_start - hh // 2, rh - hh),
                QPoint(x_start + hh // 2, rh - hh),
                QPoint(x_start,           rh),
            ])
            p.setBrush(QColor(255, 210, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(tri)

        finally:
            p.end()

    def mousePressEvent(self, event):
        from PyQt6.QtCore import Qt as _Qt
        if event.button() == _Qt.MouseButton.LeftButton and self._hover_cache:
            x = event.position().x()
            w = self.width()
            for (frac, output_path) in self._hover_cache:
                if abs(x - frac * w) <= 6:
                    t = frac * self._duration
                    self._seek(x)
                    self.marker_clicked.emit(t, output_path)
                    return
        self._seek(event.position().x())

    def mouseMoveEvent(self, event):
        x = event.position().x()
        # Check marker hover (±4px) using pre-computed fractions.
        if self._hover_cache:
            w = self.width()
            for (frac, output_path) in self._hover_cache:
                if abs(x - frac * w) <= 4:
                    QToolTip.showText(QCursor.pos(), output_path, self)
                    if event.buttons():
                        self._seek(x)
                    return
        QToolTip.hideText()
        if event.buttons():
            self._seek(x)

    def mouseReleaseEvent(self, event):
        # On release, flush any pending debounced seek immediately.
        self._seek_timer.stop()
        self.cursor_changed.emit(self._cursor)

    def contextMenuEvent(self, event):
        if not self._hover_cache or self._duration <= 0:
            return
        x = event.pos().x()
        w = self.width()
        hit_path = None
        for (frac, output_path) in self._hover_cache:
            if abs(x - frac * w) <= 6:
                hit_path = output_path
                break
        if hit_path is None:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        name = os.path.basename(hit_path)
        action = menu.addAction(f"Delete marker: {name}")
        if menu.exec(event.globalPos()) == action:
            self.marker_delete_requested.emit(hit_path)

    def _seek(self, x: float):
        t = self._pos_to_time(int(x))
        self.set_cursor(t)           # update visuals immediately
        self._seek_timer.start()     # debounce the mpv seek


import ctypes


class MpvWidget(QWidget):
    """Embeds mpv using an off-screen OpenGL FBO with QPainter readback.

    mpv renders each frame into a QOpenGLFramebufferObject on an off-screen
    surface.  The FBO is read back to a QImage and displayed via QPainter,
    bypassing Wayland sub-surface compositing issues that affect both
    QOpenGLWidget and QOpenGLWindow+createWindowContainer.
    """
    file_loaded = pyqtSignal()
    crop_clicked = pyqtSignal(float)
    _do_file_loaded = pyqtSignal()  # mpv thread → Qt main thread for file-loaded event

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frame: "QImage | None" = None
        self._render_ctx = None
        self._video_w: int = 0
        self._video_h: int = 0
        self._fbo = None
        self._needs_render = False  # set True by mpv update_cb (any thread)

        from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        from PyQt6.QtOpenGL import QOpenGLFramebufferObject

        fmt = QSurfaceFormat.defaultFormat()
        self._gl_surface = QOffscreenSurface()
        self._gl_surface.setFormat(fmt)
        self._gl_surface.create()

        self._gl_ctx = QOpenGLContext()
        self._gl_ctx.setFormat(fmt)
        self._gl_ctx.create()
        self._gl_ctx.makeCurrent(self._gl_surface)

        _PROC_ADDR_T = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)

        @_PROC_ADDR_T
        def _get_proc_addr(_, name):
            addr = self._gl_ctx.getProcAddress(name)
            return int(addr) if addr else 0

        self._get_proc_addr_fn = _get_proc_addr

        self._player = mpv.MPV(keep_open=True, pause=True, vo="libmpv")
        try:
            self._render_ctx = mpv.MpvRenderContext(
                self._player, "opengl",
                opengl_init_params={"get_proc_address": self._get_proc_addr_fn},
            )
            self._render_ctx.update_cb = self._on_mpv_update
        except Exception as e:
            print(f"[8-cut] MpvRenderContext failed: {e}", file=sys.stderr)

        self._gl_ctx.doneCurrent()

        # Timer polls for new frames at ~60 fps; avoids flooding the event loop
        # from mpv's C thread which calls update_cb at playback rate.
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(16)
        self._render_timer.timeout.connect(self._poll_render)
        self._render_timer.start()

        self._do_file_loaded.connect(self._on_file_loaded_qt)
        self._overlay_ratio: tuple[int, int] | None = None  # (num, den) or None
        self._overlay_crop_center: float = 0.5
        self._overlay_fracs: "tuple[float, float] | None" = None  # (left_frac, right_frac)

        @self._player.event_callback("file-loaded")
        def _on_file_loaded(event):
            self._do_file_loaded.emit()

    def _on_file_loaded_qt(self) -> None:
        self._video_w = self._player.width or 0
        self._video_h = self._player.height or 0
        self._overlay_fracs = None  # recompute with new dimensions
        self.file_loaded.emit()

    def set_crop_overlay(self, ratio: "tuple[int,int] | None", crop_center: float) -> None:
        self._overlay_ratio = ratio
        self._overlay_crop_center = crop_center
        self._overlay_fracs: "tuple[float,float] | None" = None  # invalidate cache
        self.update()

    def _on_mpv_update(self):
        # Called from mpv's C thread — only set a flag, no Qt calls here.
        self._needs_render = True

    def _poll_render(self):
        if self._needs_render and self._render_ctx and self._render_ctx.update():
            self._needs_render = False
            self._render_frame()

    def _render_frame(self):
        from PyQt6.QtOpenGL import QOpenGLFramebufferObject
        if not self._render_ctx:
            return
        w, h = max(self.width(), 1), max(self.height(), 1)
        self._gl_ctx.makeCurrent(self._gl_surface)
        try:
            if self._fbo is None or self._fbo.width() != w or self._fbo.height() != h:
                self._fbo = QOpenGLFramebufferObject(w, h)
            self._render_ctx.render(
                flip_y=True,
                opengl_fbo={"w": w, "h": h, "fbo": self._fbo.handle()},
            )
            self._render_ctx.report_swap()
            self._frame = self._fbo.toImage()
        except Exception as e:
            print(f"[8-cut] render error: {e}", file=sys.stderr)
        finally:
            self._gl_ctx.doneCurrent()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if self._frame and not self._frame.isNull():
            p.drawImage(self.rect(), self._frame)
        else:
            p.fillRect(self.rect(), QColor(0, 0, 0))

        if self._overlay_ratio is not None and self._player.pause:
            if self._overlay_fracs is None:
                vw, vh = self._video_w, self._video_h
                if vw > 0 and vh > 0:
                    num, den = self._overlay_ratio
                    crop_w_frac = min((vh * num / den) / vw, 1.0)
                    half = crop_w_frac / 2.0
                    center = self._overlay_crop_center
                    self._overlay_fracs = (
                        max(0.0, center - half),
                        min(1.0, center + half),
                    )
            if self._overlay_fracs is not None:
                left_frac, right_frac = self._overlay_fracs
                ww, wh = self.width(), self.height()
                left_px  = int(left_frac  * ww)
                right_px = int(right_frac * ww)
                cut_color = QColor(180, 0, 0, 140)
                if left_px > 0:
                    p.fillRect(0, 0, left_px, wh, cut_color)
                if right_px < ww:
                    p.fillRect(right_px, 0, ww - right_px, wh, cut_color)

        p.end()

    def mousePressEvent(self, event):
        w = self.width()
        if w > 0:
            self.crop_clicked.emit(event.position().x() / w)

    def load(self, path: str): self._player.play(path)

    def seek(self, t: float):
        if self._player.duration is None:
            return
        try:
            self._player.seek(t, "absolute")
        except SystemError:
            pass

    def play_loop(self, a: float, b: float):
        self._player["ab-loop-a"] = a
        self._player["ab-loop-b"] = min(b, self._player.duration or b)
        self._player.seek(a, "absolute")
        self._player.pause = False

    def stop_loop(self):
        self._player["ab-loop-a"] = "no"
        self._player["ab-loop-b"] = "no"
        self._player.pause = True

    def get_duration(self) -> float:
        d = self._player.duration
        return d if d else 0.0

    def get_video_size(self) -> tuple[int, int]:
        return (self._video_w, self._video_h)

    def get_fps(self) -> float:
        return self._player.container_fps or 25.0

    def is_playing(self) -> bool:
        return not self._player.pause

    def closeEvent(self, event):
        self._render_timer.stop()
        if self._render_ctx:
            self._render_ctx.free()
            self._render_ctx = None
        self._player.terminate()
        self._fbo = None
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
        self._crop_pen = QPen(QColor(100, 160, 240))
        self._crop_pen.setWidth(1)

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
            p.setPen(self._crop_pen)
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
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setMinimumWidth(200)
        self.setWordWrap(True)
        self._paths: list[str] = []
        self._path_set: set[str] = set()  # O(1) duplicate check
        self.itemClicked.connect(self._on_item_clicked)

    def add_files(self, paths: list[str]) -> None:
        """Append paths not already in queue; auto-select first if queue was empty."""
        was_empty = len(self._paths) == 0
        for path in paths:
            if path not in self._path_set and os.path.isfile(path):
                self._paths.append(path)
                self._path_set.add(path)
                self.addItem(os.path.basename(path))
        if was_empty and self._paths:
            self._select(0)

    def mark_done(self, path: str) -> None:
        """Gray out and prefix ✓ on the queue item for path."""
        if path not in self._path_set:
            return
        row = self._paths.index(path)
        item = self.item(row)
        if item is None:
            return
        name = os.path.basename(path)
        item.setText(f"✓ {name}")
        item.setForeground(QColor(100, 180, 100))

    def advance(self) -> None:
        """Move to next item in queue. Does nothing if at end or nothing selected."""
        row = self.currentRow()
        if row >= 0 and row < self.count() - 1:
            self._select(row + 1)

    def current_path(self) -> str | None:
        row = self.currentRow()
        return self._paths[row] if 0 <= row < len(self._paths) else None

    def _select(self, row: int) -> None:
        prev = self.currentRow()
        self.setCurrentRow(row)
        if prev >= 0 and prev != row and self.item(prev):
            self._refresh_item_text(prev)
        if self.item(row):
            item = self.item(row)
            prefix = "✓ " if item.foreground().color() == QColor(100, 180, 100) else ""
            item.setText(f"▶ {prefix}{os.path.basename(self._paths[row])}")
        self.file_selected.emit(self._paths[row])

    def _refresh_item_text(self, row: int) -> None:
        item = self.item(row)
        if item is None:
            return
        name = os.path.basename(self._paths[row])
        if item.foreground().color() == QColor(100, 180, 100):
            item.setText(f"✓ {name}")
        else:
            item.setText(name)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._select(self.row(item))


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
        if self._worker and self._worker.isRunning():
            return
        if self._worker:
            self._worker.quit()
            self._worker.wait()
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
    # Force desktop OpenGL (not GLES) so mpv's render context produces non-black output.
    # Must be set before QApplication.
    from PyQt6.QtGui import QSurfaceFormat
    _fmt = QSurfaceFormat()
    _fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    _fmt.setVersion(3, 3)
    _fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(_fmt)

    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")  # QApplication resets locale; re-apply for libmpv
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
        self.setAcceptDrops(True)

        # Services
        self._db = ProcessedDB()

        # State
        self._file_path: str = ""
        self._cursor: float = 0.0
        self._export_counter: int = 1
        self._export_worker: ExportWorker | None = None
        self._last_export_path: str = ""
        self._overwrite_path: str = ""   # set when a marker is selected for re-export
        self._mask_worker: MaskWorker | None = None
        self._db_worker: _DBWorker | None = None
        self._fps: float = 25.0  # cached on file load via get_fps()

        # Widgets
        self._playlist = PlaylistWidget()
        self._playlist.file_selected.connect(self._load_file)

        self._mpv = MpvWidget()
        self._mpv.file_loaded.connect(self._after_load)
        self._timeline = TimelineWidget()
        self._timeline.setFixedHeight(160)
        self._timeline.cursor_changed.connect(self._on_cursor_changed)
        self._timeline.marker_delete_requested.connect(self._on_delete_marker)
        self._timeline.marker_clicked.connect(self._on_marker_clicked)

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

        self._settings = QSettings("8cut", "8cut")

        self._txt_name = QLineEdit("clip")
        self._txt_name.setPlaceholderText("base name")
        self._txt_name.setMaximumWidth(150)
        self._txt_name.textChanged.connect(self._reset_counter)

        self._txt_folder = QLineEdit(self._settings.value("export_folder", str(Path.home())))
        self._txt_folder.textChanged.connect(self._reset_counter)
        self._txt_folder.textChanged.connect(
            lambda v: self._settings.setValue("export_folder", v)
        )
        self._btn_folder = QPushButton("Browse")
        self._btn_folder.clicked.connect(self._pick_folder)
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

        self._txt_label = QComboBox()
        self._txt_label.setEditable(True)
        self._txt_label.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._txt_label.lineEdit().setPlaceholderText("Sound label (e.g. dog barking)")
        self._txt_label.setFixedWidth(220)
        self._txt_label.addItems(self._db.get_labels())
        saved_label = self._settings.value("sound_label", "")
        self._txt_label.setCurrentText(saved_label)
        self._txt_label.currentTextChanged.connect(
            lambda v: self._settings.setValue("sound_label", v)
        )

        self._cmb_category = QComboBox()
        self._cmb_category.addItems(_SELVA_CATEGORIES)
        saved_cat = self._settings.value("sound_category", "")
        cat_idx = self._cmb_category.findText(saved_cat)
        self._cmb_category.setCurrentIndex(max(cat_idx, 0))
        self._cmb_category.currentTextChanged.connect(
            lambda v: self._settings.setValue("sound_category", v)
        )

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

        # Row 1 — transport + annotation + export trigger
        transport_row = QHBoxLayout()
        transport_row.addWidget(self._btn_play)
        transport_row.addWidget(self._btn_pause)
        transport_row.addWidget(self._lbl_cursor)
        transport_row.addWidget(self._lbl_duration)
        transport_row.addStretch()
        transport_row.addWidget(QLabel("Label:"))
        transport_row.addWidget(self._txt_label)
        transport_row.addWidget(QLabel("Cat:"))
        transport_row.addWidget(self._cmb_category)
        transport_row.addWidget(self._lbl_next)
        transport_row.addWidget(self._btn_export)

        # Row 2 — output path + encoding settings (bottom)
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Name:"))
        settings_row.addWidget(self._txt_name)
        settings_row.addWidget(QLabel("Folder:"))
        settings_row.addWidget(self._txt_folder, stretch=1)
        settings_row.addWidget(self._btn_folder)
        settings_row.addWidget(QLabel("Short side:"))
        settings_row.addWidget(self._txt_resize)
        settings_row.addWidget(QLabel("Portrait:"))
        settings_row.addWidget(self._cmb_portrait)
        settings_row.addWidget(QLabel("Format:"))
        settings_row.addWidget(self._cmb_format)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addLayout(top_bar)
        right_layout.addWidget(self._mpv, stretch=1)
        right_layout.addWidget(self._timeline)
        right_layout.addWidget(self._crop_bar)
        right_layout.addLayout(transport_row)
        right_layout.addLayout(settings_row)

        self._mask_row_widget = QWidget()
        mask_row = QHBoxLayout(self._mask_row_widget)
        mask_row.setContentsMargins(0, 0, 0, 0)
        mask_row.addWidget(QLabel("Masks:"))
        mask_row.addWidget(self._cmb_mask)
        mask_row.addWidget(self._btn_masks)
        _lbl_mask_warn = QLabel("⚠ Untested — use ComfyUI instead")
        _lbl_mask_warn.setStyleSheet("color: #e0a030; font-style: italic;")
        mask_row.addWidget(_lbl_mask_warn)
        mask_row.addStretch()
        show_masks = self._settings.value("show_masks_row", "true") == "true"
        self._mask_row_widget.setVisible(show_masks)

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
        if saved_ratio != "Off":
            self._mpv.set_crop_overlay(_RATIOS[saved_ratio], self._crop_center)

        # Application-wide shortcuts — fire regardless of which widget has focus.
        ctx = Qt.ShortcutContext.ApplicationShortcut
        for key in ("Left", "J"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(
                lambda: self._step_cursor(-1.0 / self._fps)
            )
        for key in ("Right", "L"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(
                lambda: self._step_cursor(1.0 / self._fps)
            )
        for key in ("Shift+Left", "Shift+J"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(
                lambda: self._step_cursor(-1.0)
            )
        for key in ("Shift+Right", "Shift+L"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(
                lambda: self._step_cursor(1.0)
            )
        for key in ("Space", "P"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(
                self._toggle_play
            )
        QShortcut(QKeySequence("K"), self, context=ctx).activated.connect(self._on_pause)
        QShortcut(QKeySequence("E"), self, context=ctx).activated.connect(self._on_export)
        QShortcut(QKeySequence("M"), self, context=ctx).activated.connect(self._jump_to_next_marker)

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
        self._fps = self._mpv.get_fps()
        self._crop_bar.set_source_ratio(*self._mpv.get_video_size())

        # Run DB fuzzy match off the main thread — can be slow on large databases.
        filename = os.path.basename(self._file_path)
        self._db_worker = _DBWorker(self._db, filename)
        self._db_worker.result.connect(self._on_db_result)
        self._db_worker.start()

    def _on_db_result(self, queried: str, match: object, markers: list) -> None:
        # Discard stale results if the user loaded a different file already.
        if os.path.basename(self._file_path) != queried:
            return
        if match:
            self.statusBar().showMessage(f"⚠ Similar to already processed: {match}")
        else:
            self.statusBar().clearMessage()
        self._timeline.set_markers(markers)

    def _refresh_markers(self) -> None:
        filename = os.path.basename(self._file_path)
        # After an export we already know the exact stored filename, so skip
        # the expensive fuzzy match and query directly.
        if self._db._enabled:
            markers = self._db._get_markers_for(filename)
            if not markers:
                # First export for this file — fall back to fuzzy match once.
                markers = self._db.get_markers(filename)
        else:
            markers = []
        self._timeline.set_markers(markers)

    def _on_delete_marker(self, output_path: str) -> None:
        self._db.delete_by_output_path(output_path)
        self._refresh_markers()
        self.statusBar().showMessage(
            f"Deleted marker: {os.path.basename(output_path)}", 4000
        )

    def _on_marker_clicked(self, start_time: float, output_path: str) -> None:
        self._overwrite_path = output_path
        self._lbl_next.setText(f"↺ {os.path.basename(output_path)}")
        self.statusBar().showMessage(
            f"Overwrite mode: {os.path.basename(output_path)} — export to replace", 5000
        )

    def _on_portrait_ratio_changed(self, text: str) -> None:
        ratio = None if text == "Off" else text
        self._crop_bar.set_portrait_ratio(ratio)
        self._crop_bar.setVisible(ratio is not None)
        self._settings.setValue("portrait_ratio", text)
        self._mpv.set_crop_overlay(
            _RATIOS[ratio] if ratio else None, self._crop_center
        )

    def _on_crop_click(self, frac: float) -> None:
        ratio = self._cmb_portrait.currentText()
        if ratio == "Off":
            return
        self._crop_center = max(0.0, min(1.0, frac))
        self._settings.setValue("crop_center", str(self._crop_center))
        self._crop_bar.set_crop_center(self._crop_center)
        self._mpv.set_crop_overlay(_RATIOS[ratio], self._crop_center)

    # --- Playback ---

    def _on_cursor_changed(self, t: float):
        self._cursor = t
        self._lbl_cursor.setText(f"cursor: {format_time(t)}")
        if self._overwrite_path:
            self._overwrite_path = ""
            self._update_next_label()
        if self._mpv.is_playing():
            self._mpv.play_loop(t, t + 8.0)
        else:
            self._mpv.seek(t)

    def _toggle_play(self):
        if not self._file_path:
            return
        if self._mpv.is_playing():
            self._on_pause()
        else:
            self._on_play()

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
        # Update label and internal state immediately; route the seek through
        # the timeline's debounce timer so rapid key repeats don't hammer mpv.
        self._cursor = new_t
        self._lbl_cursor.setText(f"cursor: {format_time(new_t)}")
        self._timeline.set_cursor(new_t)
        self._timeline._seek_timer.start()

    def _jump_to_next_marker(self) -> None:
        markers = sorted(self._timeline._markers, key=lambda m: m[0])
        if not markers:
            return
        for (t, _num, _path) in markers:
            if t > self._cursor + 0.1:
                self._step_cursor(t - self._cursor)
                return
        self._step_cursor(markers[0][0] - self._cursor)  # wrap to first

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
        is_seq = self._cmb_format.currentText() == "WebP sequence"
        # Advance past any files/dirs that already exist on disk.
        while True:
            if is_seq:
                path = build_sequence_dir(folder, name, self._export_counter)
            else:
                path = build_export_path(folder, name, self._export_counter)
            if not os.path.exists(path):
                break
            self._export_counter += 1
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
        os.makedirs(folder, exist_ok=True)
        if self._overwrite_path:
            output = self._overwrite_path
            self._overwrite_path = ""
        else:
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

        # Show marker immediately — don't wait for ffmpeg to finish.
        pending = self._timeline._markers + [(self._cursor, self._export_counter, output)]
        self._timeline.set_markers(pending)

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
        label = self._txt_label.currentText().strip()
        category = self._cmb_category.currentText()
        self._db.add(
            os.path.basename(self._file_path),
            self._cursor,
            path,
            label=label,
            category=category,
        )
        folder = self._txt_folder.text()
        upsert_clip_annotation(folder, path, label)
        # For MP4 exports path is a file; for WebP sequence it is a directory.
        # build_mask_output_dir handles both correctly via Path.stem.
        self._last_export_path = path
        self._export_counter += 1
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Exported: {os.path.basename(path)}")
        self._refresh_markers()
        self._playlist.mark_done(self._file_path)
        # Refresh label history so the new label is immediately selectable.
        current = self._txt_label.currentText()
        self._txt_label.blockSignals(True)
        self._txt_label.clear()
        self._txt_label.addItems(self._db.get_labels())
        self._txt_label.setCurrentText(current)
        self._txt_label.blockSignals(False)
        self._playlist.advance()

    def _on_export_error(self, msg: str):
        self._btn_export.setEnabled(True)
        self.statusBar().showMessage(f"Export error: {msg}")

    # --- Mask generation ---

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            u.toLocalFile() for u in event.mimeData().urls()
            if os.path.isfile(u.toLocalFile())
        ]
        if paths:
            self._playlist.add_files(paths)
            for p in paths:
                if self._db.get_markers(os.path.basename(p)):
                    self._playlist.mark_done(p)

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
