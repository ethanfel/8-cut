#!/usr/bin/env python3
import locale
locale.setlocale(locale.LC_NUMERIC, "C")  # required by libmpv before any import

import sys
import os
import random
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QFormLayout,
)
from PyQt6.QtCore import Qt, QObject, QThread, QTimer, QRect, QSize, pyqtSignal, QSettings
from PyQt6.QtGui import QPainter, QColor, QPen, QPixmap, QDragEnterEvent, QDropEvent, QCursor, QFont, QKeySequence, QShortcut
if sys.platform == "win32":
    # Help ctypes find libmpv-2.dll next to main.py or in frozen bundle
    _dll_dir = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
    os.add_dll_directory(str(_dll_dir))
elif sys.platform == "darwin" and getattr(sys, "frozen", False):
    os.environ.setdefault("DYLD_LIBRARY_PATH", str(Path(sys._MEIPASS)))
import mpv

from core.paths import _bin, _log, build_export_path, build_sequence_dir, format_time
from core.ffmpeg import (
    _RATIOS, resolve_keyframe, apply_keyframes_to_jobs,
    build_ffmpeg_command, build_audio_extract_command, detect_hw_encoders,
)
from core.db import ProcessedDB
from core.annotations import remove_clip_annotation, upsert_clip_annotation
from core.tracking import track_centers_for_jobs

_SELVA_CATEGORIES = ["", "Human", "Animal", "Vehicle", "Tool", "Music", "Nature", "Sport", "Other"]


class _DBWorker(QThread):
    """Runs ProcessedDB fuzzy-match lookup off the main thread."""
    result = pyqtSignal(str, object, list)  # (queried_filename, match|None, markers)

    def __init__(self, db: "ProcessedDB", filename: str, profile: str = "default"):
        super().__init__()
        self._db = db
        self._filename = filename
        self._profile = profile

    def run(self):
        try:
            markers = self._db._get_markers_for(self._filename, self._profile)
        except Exception:
            markers = []
        self.result.emit(self._filename, self._filename if markers else None, markers)


class ExportWorker(QThread):
    finished = pyqtSignal(str)   # emitted per completed clip
    error = pyqtSignal(str)      # error message
    all_done = pyqtSignal()      # emitted after all jobs complete
    cancelled = pyqtSignal()     # emitted when cancel completes

    def __init__(self, input_path: str,
                 jobs: list[tuple[float, str, str | None, float]],
                 short_side: int | None = None,
                 image_sequence: bool = False,
                 max_workers: int | None = None,
                 encoder: str = "libx264"):
        super().__init__()
        self._input = input_path
        self._jobs = jobs  # [(start, output, portrait_ratio, crop_center), ...]
        self._short_side = short_side
        self._image_sequence = image_sequence
        self._max_workers = max_workers
        self._encoder = encoder
        self._cancel = False
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = __import__('threading').Lock()

    def cancel(self) -> None:
        self._cancel = True
        with self._procs_lock:
            for proc in self._procs:
                try:
                    proc.kill()
                except OSError:
                    pass

    def _run_one(self, start: float, output: str,
                  portrait_ratio: str | None, crop_center: float) -> str:
        """Encode a single clip. Returns output path on success, raises on error."""
        if self._cancel:
            raise RuntimeError("cancelled")
        if self._image_sequence:
            os.makedirs(output, exist_ok=True)
        cmd = build_ffmpeg_command(
            self._input, start, output,
            short_side=self._short_side,
            portrait_ratio=portrait_ratio,
            crop_center=crop_center,
            image_sequence=self._image_sequence,
            encoder=self._encoder,
        )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._procs_lock:
            self._procs.append(proc)
        try:
            _, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("ffmpeg timed out")
        finally:
            with self._procs_lock:
                self._procs.remove(proc)
        if self._cancel:
            raise RuntimeError("cancelled")
        if proc.returncode != 0:
            msg = stderr.decode(errors='replace')[-500:] if stderr else "ffmpeg failed"
            raise RuntimeError(msg)
        if self._image_sequence:
            audio_cmd = build_audio_extract_command(self._input, start, output)
            subprocess.run(audio_cmd, capture_output=True, text=True, timeout=60)
        return output

    def run(self):
        cap = self._max_workers or (os.cpu_count() or 2)
        workers = min(len(self._jobs), cap)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._run_one, s, o, pr, cc): o
                    for s, o, pr, cc in self._jobs
                }
                for fut in as_completed(futures):
                    if self._cancel:
                        pool.shutdown(wait=False, cancel_futures=True)
                        self.cancelled.emit()
                        return
                    try:
                        path = fut.result()
                        self.finished.emit(path)
                    except FileNotFoundError:
                        self.error.emit("ffmpeg not found — is it installed and on PATH?")
                        return
                    except Exception as e:
                        if self._cancel:
                            break
                        self.error.emit(str(e))
                        return
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))
            return
        if self._cancel:
            self.cancelled.emit()
        else:
            self.all_done.emit()


class FrameGrabber(QThread):
    """Grab a single frame via ffmpeg and emit it as raw PNG bytes."""
    frame_ready = pyqtSignal(bytes)

    def __init__(self, input_path: str, time: float):
        super().__init__()
        self._input = input_path
        self._time = time

    def run(self):
        try:
            cmd = [
                _bin("ffmpeg"), "-ss", str(self._time),
                "-i", self._input,
                "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png",
                "pipe:1",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                self.frame_ready.emit(result.stdout)
        except Exception:
            pass


class ScanWorker(QThread):
    """Runs audio similarity scan off the main thread."""
    scan_done = pyqtSignal(list)  # emits list of (start, end, score)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)    # status message

    def __init__(self, video_path: str, model: dict,
                 threshold: float = 0.30):
        super().__init__()
        self._video_path = video_path
        self._model = model
        self._threshold = threshold
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self):
        from core.audio_scan import scan_video
        try:
            self.progress.emit("Scanning audio...")
            regions = scan_video(
                self._video_path, model=self._model,
                threshold=self._threshold, cancel_flag=self,
            )
            if not self._cancel:
                self.scan_done.emit(regions)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))


class TrainDialog(QDialog):
    """Dialog for configuring and launching classifier training."""

    def __init__(self, db: ProcessedDB, profile: str, video_dir: str = "",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Train Classifier")
        self.setMinimumWidth(400)

        from core.audio_scan import _EMBED_MODELS
        self._db = db
        self._profile = profile
        self._video_dir = video_dir

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Positive class selector — lists export folders
        self._cmb_positive = QComboBox()
        stats = db.get_training_stats(profile)
        if not stats:
            form.addRow("", QLabel("No exported clips found for this profile."))
        for folder_name, info in stats.items():
            label = f"{folder_name}  ({info['videos']} videos, {info['clips']} clips)"
            self._cmb_positive.addItem(label, userData=folder_name)
        form.addRow("Positive class:", self._cmb_positive)

        # Model selector
        self._cmb_model = QComboBox()
        for name in _EMBED_MODELS:
            self._cmb_model.addItem(name)
        self._cmb_model.setCurrentText("WAV2VEC2_BASE")
        form.addRow("Model:", self._cmb_model)

        # Video source directory (fallback for old DB rows without source_path)
        self._txt_video_dir = QLineEdit(video_dir)
        self._txt_video_dir.setPlaceholderText("Directory containing source videos")
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._update_stats)
        self._txt_video_dir.textChanged.connect(lambda: self._debounce.start())
        vid_row = QHBoxLayout()
        vid_row.addWidget(self._txt_video_dir)
        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self._browse_video_dir)
        vid_row.addWidget(btn_browse)
        form.addRow("Video dir:", vid_row)

        layout.addLayout(form)

        # Stats summary
        self._lbl_stats = QLabel()
        self._update_stats()
        self._cmb_positive.currentIndexChanged.connect(self._update_stats)
        layout.addWidget(self._lbl_stats)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Train")
        btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._cmb_positive.count() > 0
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _browse_video_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select video source directory")
        if d:
            self._txt_video_dir.setText(d)

    def _update_stats(self):
        folder = self._cmb_positive.currentData()
        if not folder:
            self._lbl_stats.setText("No export folder data available.")
            return
        video_infos = self._db.get_training_data(
            self._profile, folder,
            fallback_video_dir=self._txt_video_dir.text(),
        )
        n_videos = len(video_infos)
        n_pos = sum(len(gt) for _, gt, _ in video_infos)
        n_soft = sum(len(s) for _, _, s in video_infos)
        lines = [f"<b>{n_videos}</b> videos with positive clips"]
        lines.append(f"<b>{n_pos}</b> positive markers, <b>{n_soft}</b> soft/buffer markers")
        if n_videos == 0:
            lines.append("<i>No source videos found. Set Video dir above.</i>")
        elif n_videos < 3:
            lines.append("<i>Recommend at least 3 videos for decent results.</i>")
        self._lbl_stats.setText("<br>".join(lines))

    @property
    def positive_folder(self) -> str:
        return self._cmb_positive.currentData() or ""

    @property
    def embed_model(self) -> str:
        return self._cmb_model.currentText()

    @property
    def video_dir(self) -> str:
        return self._txt_video_dir.text()


class TrainWorker(QThread):
    """Trains an audio classifier off the main thread."""
    train_done = pyqtSignal(str)   # emits model path on success
    error = pyqtSignal(str)
    progress = pyqtSignal(str)     # per-video status

    def __init__(self, video_infos: list, model_path: str,
                 embed_model: str | None = None):
        super().__init__()
        self._video_infos = video_infos
        self._model_path = model_path
        self._embed_model = embed_model
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self):
        from core.audio_scan import train_classifier
        try:
            self.progress.emit(f"Training on {len(self._video_infos)} videos...")
            result = train_classifier(
                self._video_infos,
                model_path=self._model_path,
                embed_model=self._embed_model,
            )
            if self._cancel:
                return
            if result is None:
                self.error.emit("Training failed: not enough data or missing class balance")
            else:
                self.train_done.emit(self._model_path)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))


class TimelineWidget(QWidget):
    cursor_changed = pyqtSignal(float)              # emits position in seconds
    seek_changed = pyqtSignal(float)                # emits seek position (lock mode)
    marker_delete_requested = pyqtSignal(str)       # emits output_path
    keyframe_delete_requested = pyqtSignal(float)   # emits keyframe time
    marker_clicked = pyqtSignal(float, str)         # emits (start_time, output_path)
    marker_deselected = pyqtSignal()                # double-click on empty space

    _RULER_H = 22   # pixels reserved for the time ruler
    _HANDLE_H = 8   # height of the playhead triangle

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(80)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._cursor = 0.0
        self._clip_span = 14.0  # 8 + 2*spread, updated from MainWindow
        self._play_pos: float | None = None  # current playback position (seconds)
        self._locked = False                 # when True, clicks scrub playback, not cursor
        self._crop_keyframes: list[tuple[float, float, str | None, bool, bool]] = []
        self._markers: list[tuple[float, int, str]] = []
        self._hover_cache: list[tuple[float, str]] = []  # (t/duration, path)
        self._scan_regions: list[tuple[float, float, float]] = []  # (start, end, score)

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
        self._seek_timer.timeout.connect(self._emit_seek)

    def set_duration(self, duration: float):
        self._duration = duration
        self._cursor = 0.0
        self._play_pos = None
        self._rebuild_hover_cache()
        self.update()

    def set_clip_span(self, span: float):
        self._clip_span = span
        self.update()

    def set_cursor(self, seconds: float):
        clamped = max(0.0, min(seconds, max(0.0, self._duration - self._clip_span)))
        if clamped == self._cursor:
            return
        self._cursor = clamped
        self.update()

    def set_markers(self, markers: list[tuple[float, int, str]]) -> None:
        """markers: list of (start_time, number, output_path)"""
        self._markers = markers
        self._rebuild_hover_cache()
        self.update()

    def set_scan_regions(self, regions: list[tuple[float, float, float]]) -> None:
        """regions: list of (start_time, end_time, score)"""
        self._scan_regions = regions
        self.update()

    def clear_scan_regions(self) -> None:
        self._scan_regions = []
        self.update()

    def set_play_position(self, t: float | None) -> None:
        # In lock mode, ignore mpv position updates while the user is dragging
        # — the async seek hasn't caught up yet, so mpv reports stale values.
        if self._locked and self._play_pos is not None and self._seek_timer.isActive():
            return
        self._play_pos = t
        self.update()

    def set_crop_keyframes(self, kfs: list[tuple[float, float, str | None, bool, bool]]) -> None:
        self._crop_keyframes = kfs
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

            # ── selection region (full clip span) ─────────────────────────
            x_start = int(self._cursor / self._duration * w)
            x_end   = int(min(self._cursor + self._clip_span, self._duration) / self._duration * w)
            sel_w   = max(x_end - x_start, 1)
            p.fillRect(x_start, rh, sel_w, th, QColor(60, 130, 220, 90))

            # ── playback progress fill ────────────────────────────────────
            if self._play_pos is not None and self._play_pos > self._cursor:
                prog_end = min(self._play_pos, self._cursor + self._clip_span, self._duration)
                x_prog = int(prog_end / self._duration * w)
                prog_w = max(x_prog - x_start, 0)
                if prog_w > 0:
                    p.fillRect(x_start, rh, prog_w, th, QColor(100, 200, 255, 60))

            # left/right edges of selection
            p.setPen(QPen(QColor(60, 130, 220, 180), 1))
            p.drawLine(x_start, rh, x_start, h)
            p.drawLine(x_end,   rh, x_end,   h)

            # ── scan regions ──────────────────────────────────────────────
            if self._scan_regions and self._duration > 0:
                for (start, end, score) in self._scan_regions:
                    x1 = int(start / self._duration * w)
                    x2 = int(end / self._duration * w)
                    alpha = int(40 + score * 80)  # 40–120 opacity
                    p.fillRect(x1, rh, x2 - x1, h - rh, QColor(100, 200, 255, alpha))

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

            # ── crop keyframe diamonds ────────────────────────────────────
            if self._crop_keyframes and self._duration > 0:
                _KF_GOLD = QColor(255, 180, 0)
                _KF_RED = QColor(220, 60, 60)
                _KF_BLUE = QColor(60, 180, 220)
                for kf in self._crop_keyframes:
                    kt = kf[0]
                    rp = kf[3] if len(kf) > 3 else False
                    rs = kf[4] if len(kf) > 4 else False
                    kx = int(kt / self._duration * w)
                    d = 4  # half-size of diamond
                    ky = h - d - 2  # near bottom of track
                    if rp and rs:
                        # Split diamond: left half red, right half blue
                        left = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx, ky + d),
                            QPoint(kx - d, ky),
                        ])
                        right = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx + d, ky),
                            QPoint(kx, ky + d),
                        ])
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(_KF_RED)
                        p.drawPolygon(left)
                        p.setBrush(_KF_BLUE)
                        p.drawPolygon(right)
                    else:
                        diamond = QPolygon([
                            QPoint(kx, ky - d), QPoint(kx + d, ky),
                            QPoint(kx, ky + d), QPoint(kx - d, ky),
                        ])
                        if rp:
                            color = _KF_RED
                        elif rs:
                            color = _KF_BLUE
                        else:
                            color = _KF_GOLD
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(color)
                        p.drawPolygon(diamond)

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
        self._seek(event.position().x())

    def mouseDoubleClickEvent(self, event):
        from PyQt6.QtCore import Qt as _Qt
        if event.button() == _Qt.MouseButton.LeftButton:
            x = event.position().x()
            if self._hover_cache:
                w = self.width()
                for (frac, output_path) in self._hover_cache:
                    if abs(x - frac * w) <= 10:
                        t = frac * self._duration
                        self.marker_clicked.emit(t, output_path)
                        if not self._locked:
                            self._seek(x)
                        return
            self.marker_deselected.emit()
            self._seek(x)

    def mouseMoveEvent(self, event):
        x = event.position().x()
        # Check marker hover using pre-computed fractions.
        if self._hover_cache:
            w = self.width()
            for (frac, output_path) in self._hover_cache:
                if abs(x - frac * w) <= 8:
                    QToolTip.showText(QCursor.pos(), os.path.basename(output_path), self)
                    if event.buttons():
                        self._seek(x)
                    return
        QToolTip.hideText()
        if event.buttons():
            self._seek(x)

    def _emit_seek(self):
        if self._locked:
            self.seek_changed.emit(self._play_pos if self._play_pos is not None else self._cursor)
        else:
            self.cursor_changed.emit(self._cursor)

    def mouseReleaseEvent(self, event):
        # On release, flush any pending debounced seek immediately.
        self._seek_timer.stop()
        self._emit_seek()

    def contextMenuEvent(self, event):
        if self._duration <= 0:
            return
        x = event.pos().x()
        w = self.width()
        # Check keyframe diamonds first.
        hit_kf_time = None
        for kf in self._crop_keyframes:
            kt = kf[0]
            kx = kt / self._duration * w
            if abs(x - kx) <= 8:
                hit_kf_time = kt
                break
        # Check export markers.
        hit_path = None
        if self._hover_cache:
            for (frac, output_path) in self._hover_cache:
                if abs(x - frac * w) <= 10:
                    hit_path = output_path
                    break
        if hit_kf_time is None and hit_path is None:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        act_kf = None
        act_marker = None
        if hit_kf_time is not None:
            act_kf = menu.addAction(f"Delete keyframe @ {format_time(hit_kf_time)}")
        if hit_path is not None:
            act_marker = menu.addAction(f"Delete marker: {os.path.basename(hit_path)}")
        chosen = menu.exec(event.globalPos())
        if chosen and chosen == act_kf:
            self.keyframe_delete_requested.emit(hit_kf_time)
        elif chosen and chosen == act_marker:
            self.marker_delete_requested.emit(hit_path)

    def _seek(self, x: float):
        t = self._pos_to_time(int(x))
        if self._locked:
            self._play_pos = t
            self.update()
            self._seek_timer.start()
        else:
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
    time_pos_changed = pyqtSignal(float)  # emits current playback position in seconds
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

        self._player = mpv.MPV(keep_open=True, pause=True, vo="libmpv", hwdec="auto")
        _log("mpv created (hwdec=auto)")
        try:
            self._render_ctx = mpv.MpvRenderContext(
                self._player, "opengl",
                opengl_init_params={"get_proc_address": self._get_proc_addr_fn},
            )
            self._render_ctx.update_cb = self._on_mpv_update
            _log("OpenGL render context ready")
        except Exception as e:
            _log(f"MpvRenderContext failed: {e}")

        self._gl_ctx.doneCurrent()

        # Timer polls for new frames at ~60 fps; avoids flooding the event loop
        # from mpv's C thread which calls update_cb at playback rate.
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(16)
        self._render_timer.timeout.connect(self._poll_render)
        self._render_timer.start()

        self._do_file_loaded.connect(self._on_file_loaded_qt)
        # Each overlay: {"ratio": (num,den), "center": float, "lines_only": bool,
        #                "color": QColor, "_fracs": (left,right)|None}
        self._overlays: list[dict] = []

        @self._player.event_callback("file-loaded")
        def _on_file_loaded(event):
            self._do_file_loaded.emit()

    def _on_file_loaded_qt(self) -> None:
        self._video_w = self._player.width or 0
        self._video_h = self._player.height or 0
        for ov in self._overlays:
            ov["_fracs"] = None  # recompute with new dimensions
        self.file_loaded.emit()

    def set_crop_overlays(self, overlays: "list[tuple[tuple[int,int], float, bool, QColor | None]]") -> None:
        """Set one or more crop overlays.

        Each entry is (ratio, center, lines_only, color).
        Pass an empty list to clear.
        """
        self._overlays = []
        for ratio, center, lines_only, color in overlays:
            self._overlays.append({
                "ratio": ratio, "center": center,
                "lines_only": lines_only,
                "color": color or QColor(220, 60, 60, 200),
                "_fracs": None,
            })
        self.update()

    def set_crop_overlay(self, ratio: "tuple[int,int] | None", crop_center: float,
                         lines_only: bool = False) -> None:
        """Convenience: single overlay (backward-compat)."""
        if ratio is None:
            self._overlays = []
        else:
            self.set_crop_overlays([(ratio, crop_center, lines_only, None)])
        self.update()

    def _on_mpv_update(self):
        # Called from mpv's C thread — only set a flag, no Qt calls here.
        self._needs_render = True

    def _poll_render(self):
        if self._needs_render and self._render_ctx and self._render_ctx.update():
            self._needs_render = False
            self._render_frame()
        if not self._player.pause:
            tp = self._player.time_pos
            if tp is not None:
                self.time_pos_changed.emit(tp)

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
            _log(f"Render error: {e}")
        finally:
            self._gl_ctx.doneCurrent()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-render the current frame at the new widget size so it isn't
        # stretched from the old FBO dimensions.
        if self._render_ctx:
            self._render_frame()

    def _video_rect(self) -> QRect:
        """Return the sub-rect where the video sits inside the widget (letterboxed)."""
        ww, wh = self.width(), self.height()
        vw, vh = self._video_w, self._video_h
        if vw <= 0 or vh <= 0:
            return QRect(0, 0, ww, wh)
        video_aspect = vw / vh
        widget_aspect = ww / wh
        if widget_aspect > video_aspect:
            # Pillarbox — black bars on sides
            draw_h = wh
            draw_w = int(wh * video_aspect)
            return QRect((ww - draw_w) // 2, 0, draw_w, draw_h)
        else:
            # Letterbox — black bars top/bottom
            draw_w = ww
            draw_h = int(ww / video_aspect)
            return QRect(0, (wh - draw_h) // 2, draw_w, draw_h)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self._frame and not self._frame.isNull():
            p.drawImage(self.rect(), self._frame)

        if self._overlays and self._player.pause:
            vw, vh = self._video_w, self._video_h
            vr = self._video_rect()
            for ov in self._overlays:
                if ov["_fracs"] is None and vw > 0 and vh > 0:
                    num, den = ov["ratio"]
                    crop_w_frac = min((vh * num / den) / vw, 1.0)
                    half = crop_w_frac / 2.0
                    center = ov["center"]
                    ov["_fracs"] = (
                        max(0.0, center - half),
                        min(1.0, center + half),
                    )
                if ov["_fracs"] is None:
                    continue
                left_frac, right_frac = ov["_fracs"]
                left_px  = vr.x() + int(left_frac  * vr.width())
                right_px = vr.x() + int(right_frac * vr.width())
                color = ov["color"]
                if ov["lines_only"]:
                    line_pen = QPen(color)
                    line_pen.setWidth(2)
                    p.setPen(line_pen)
                    p.drawLine(left_px, vr.y(), left_px, vr.y() + vr.height())
                    p.drawLine(right_px, vr.y(), right_px, vr.y() + vr.height())
                else:
                    cut_color = QColor(color.red(), color.green(), color.blue(), 140)
                    if left_px > vr.x():
                        p.fillRect(vr.x(), vr.y(), left_px - vr.x(), vr.height(), cut_color)
                    if right_px < vr.x() + vr.width():
                        p.fillRect(right_px, vr.y(), vr.x() + vr.width() - right_px, vr.height(), cut_color)

        p.end()

    def mousePressEvent(self, event):
        vr = self._video_rect()
        if vr.width() > 0:
            x = (event.position().x() - vr.x()) / vr.width()
            self.crop_clicked.emit(max(0.0, min(1.0, x)))

    def load(self, path: str): self._player.play(path)

    def seek(self, t: float):
        if self._player.duration is None:
            return
        try:
            self._player.seek(t, "absolute")
        except SystemError:
            pass

    def play_loop(self, a: float, b: float, resume: bool = False):
        self._player["ab-loop-a"] = a
        self._player["ab-loop-b"] = min(b, self._player.duration or b)
        if not resume:
            self._player.seek(a, "absolute")
        self._player.pause = False

    def update_loop_end(self, b: float):
        """Adjust the B point of the current loop without seeking."""
        self._player["ab-loop-b"] = min(b, self._player.duration or b)

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
        if self._player:
            self._player.terminate()
            self._player = None
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


class PreviewLabel(QWidget):
    """Displays a pixmap with optional crop region overlay lines."""

    def __init__(self):
        super().__init__()
        self._pixmap: QPixmap | None = None
        # list of (ratio, crop_center, color)
        self._overlays: list[tuple[tuple[int, int], float, QColor]] = []
        self._source_ratio: float = 16 / 9
        self.setMinimumSize(160, 120)

    def setPixmap(self, px: QPixmap) -> None:
        self._pixmap = px
        self.update()

    def set_overlays(self, overlays: list[tuple[tuple[int, int], float, QColor]],
                     source_ratio: float) -> None:
        self._overlays = overlays
        self._source_ratio = source_ratio
        self.update()

    def sizeHint(self):
        if self._pixmap:
            return self._pixmap.size()
        return QSize(320, 240)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            w, h = self.width(), self.height()
            p.fillRect(0, 0, w, h, QColor(26, 26, 26))
            if self._pixmap and not self._pixmap.isNull():
                scaled = self._pixmap.scaled(
                    w, h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                ix = (w - scaled.width()) // 2
                iy = (h - scaled.height()) // 2
                p.drawPixmap(ix, iy, scaled)
                iw, ih = scaled.width(), scaled.height()
                for ratio, center, color in self._overlays:
                    num, den = ratio
                    win_frac = (num / den) / self._source_ratio
                    if win_frac >= 1.0:
                        continue
                    win_px = int(iw * win_frac)
                    max_x = iw - win_px
                    cx = ix + int(max_x * center)
                    pen = QPen(color)
                    pen.setWidth(1)
                    p.setPen(pen)
                    p.drawLine(cx, iy, cx, iy + ih)
                    p.drawLine(cx + win_px, iy, cx + win_px, iy + ih)
        finally:
            p.end()


class SnapPreviewWindow(QWidget):
    """Floating preview window that snaps and docks to the main window edges."""

    _SNAP_DIST = 20  # pixels within which snapping activates

    def __init__(self, main_win: QMainWindow):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self._main_win = main_win
        self._dock_edge: str | None = None  # "left", "right", "top", "bottom" or None
        self._dock_offset: int = 0  # offset along the docked edge
        self._in_dock = False  # recursion guard for move → dock → move

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._in_dock or not self._main_win.isVisible():
            return
        mg = self._main_win.frameGeometry()
        pg = self.frameGeometry()
        snap = self._SNAP_DIST

        # Check each edge for snapping
        if abs(pg.right() - mg.left()) < snap and self._overlaps_v(pg, mg):
            self._dock("left", mg, pg)
        elif abs(pg.left() - mg.right()) < snap and self._overlaps_v(pg, mg):
            self._dock("right", mg, pg)
        elif abs(pg.bottom() - mg.top()) < snap and self._overlaps_h(pg, mg):
            self._dock("top", mg, pg)
        elif abs(pg.top() - mg.bottom()) < snap and self._overlaps_h(pg, mg):
            self._dock("bottom", mg, pg)
        else:
            self._dock_edge = None

    def _overlaps_v(self, a, b) -> bool:
        return a.bottom() > b.top() and a.top() < b.bottom()

    def _overlaps_h(self, a, b) -> bool:
        return a.right() > b.left() and a.left() < b.right()

    def _dock(self, edge: str, mg, pg) -> None:
        self._dock_edge = edge
        self._in_dock = True
        if edge == "left":
            x = mg.left() - pg.width()
            self._dock_offset = pg.top() - mg.top()
            self.move(x, pg.top())
        elif edge == "right":
            x = mg.right()
            self._dock_offset = pg.top() - mg.top()
            self.move(x, pg.top())
        elif edge == "top":
            y = mg.top() - pg.height()
            self._dock_offset = pg.left() - mg.left()
            self.move(pg.left(), y)
        elif edge == "bottom":
            y = mg.bottom()
            self._dock_offset = pg.left() - mg.left()
            self.move(pg.left(), y)
        self._in_dock = False

    def follow_main(self) -> None:
        """Called by main window on move/resize to keep docked position."""
        if self._dock_edge is None:
            return
        self._in_dock = True
        mg = self._main_win.frameGeometry()
        pw, ph = self.frameGeometry().width(), self.frameGeometry().height()
        if self._dock_edge == "left":
            self.move(mg.left() - pw, mg.top() + self._dock_offset)
        elif self._dock_edge == "right":
            self.move(mg.right(), mg.top() + self._dock_offset)
        elif self._dock_edge == "top":
            self.move(mg.left() + self._dock_offset, mg.top() - ph)
        elif self._dock_edge == "bottom":
            self.move(mg.left() + self._dock_offset, mg.bottom())
        self._in_dock = False


class PlaylistWidget(QListWidget):
    file_selected = pyqtSignal(str)  # emits full path of selected file

    def __init__(self):
        super().__init__()
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumWidth(200)
        self.setAlternatingRowColors(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._paths: list[str] = []           # all paths (full list)
        self._path_set: set[str] = set()      # O(1) duplicate check
        self._done_set: set[str] = set()      # paths with exported clips
        self._done_counts: dict[str, int] = {}  # path → clip count
        self._hidden_basenames: set[str] = set()
        self._hide_exported = False
        self._show_hidden = False
        self._visible: list[str] = []         # paths currently shown in widget
        self._selected_path: str | None = None
        self.itemClicked.connect(self._on_item_clicked)

    def _is_visible(self, path: str) -> bool:
        if os.path.basename(path) in self._hidden_basenames:
            return self._show_hidden
        if self._hide_exported and path in self._done_set:
            return False
        return True

    def _rebuild(self) -> None:
        """Rebuild the QListWidget from scratch with only visible items."""
        self.blockSignals(True)
        self.clear()
        self._visible = [p for p in self._paths if self._is_visible(p)]
        for path in self._visible:
            name = os.path.basename(path)
            is_hidden = os.path.basename(path) in self._hidden_basenames
            if is_hidden:
                item = QListWidgetItem(f"[hidden] {name}")
                item.setForeground(QColor(120, 120, 120))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            elif path in self._done_set:
                n = self._done_counts.get(path, 0)
                tag = f"[{n}]" if n else "✓"
                item = QListWidgetItem(f"{tag} {name}")
                item.setForeground(QColor(100, 180, 100))
            else:
                item = QListWidgetItem(name)
            self.addItem(item)
        # Restore selection.
        if self._selected_path and self._selected_path in self._visible:
            row = self._visible.index(self._selected_path)
            self.setCurrentRow(row)
            self._decorate_current(row)
        self.blockSignals(False)

    def add_files(self, paths: list[str]) -> None:
        was_empty = len(self._paths) == 0
        for path in paths:
            if path not in self._path_set and os.path.isfile(path):
                self._paths.append(path)
                self._path_set.add(path)
        self._rebuild()
        if was_empty and self._visible:
            self._select(0)

    def mark_done(self, path: str, n_clips: int = 0) -> None:
        if path not in self._path_set:
            return
        self._done_set.add(path)
        self._done_counts[path] = n_clips
        # Update in-place if visible, otherwise rebuild handles it.
        if path in self._visible:
            row = self._visible.index(path)
            item = self.item(row)
            if item:
                name = os.path.basename(path)
                tag = f"[{n_clips}]" if n_clips else "✓"
                item.setText(f"{tag} {name}")
                item.setForeground(QColor(100, 180, 100))

    def unmark_done(self, path: str) -> None:
        if path not in self._path_set:
            return
        self._done_set.discard(path)
        self._done_counts.pop(path, None)
        if path in self._visible:
            row = self._visible.index(path)
            item = self.item(row)
            if item:
                item.setText(os.path.basename(path))
                item.setForeground(QColor(200, 200, 200))

    def set_hidden_basenames(self, basenames: set[str]) -> None:
        self._hidden_basenames = basenames
        self._rebuild()

    def set_show_hidden(self, show: bool) -> None:
        self._show_hidden = show
        self._rebuild()

    def set_hide_exported(self, hide: bool) -> None:
        self._hide_exported = hide
        self._rebuild()

    def advance(self) -> None:
        row = self.currentRow()
        if row >= 0 and row < self.count() - 1:
            self._select(row + 1)

    def current_path(self) -> str | None:
        row = self.currentRow()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def _select(self, row: int) -> None:
        """Select a row in the visible list."""
        prev = self.currentRow()
        self.setCurrentRow(row)
        if prev >= 0 and prev != row:
            self._decorate_prev(prev)
        if 0 <= row < len(self._visible):
            self._selected_path = self._visible[row]
            self._decorate_current(row)
            self.file_selected.emit(self._visible[row])

    def _decorate_current(self, row: int) -> None:
        item = self.item(row)
        if not item:
            return
        path = self._visible[row]
        name = os.path.basename(path)
        if path in self._done_set:
            n = self._done_counts.get(path, 0)
            tag = f"[{n}] " if n else "✓ "
        else:
            tag = ""
        item.setText(f"▶ {tag}{name}")

    def _decorate_prev(self, row: int) -> None:
        item = self.item(row)
        if not item or row >= len(self._visible):
            return
        path = self._visible[row]
        name = os.path.basename(path)
        if path in self._done_set:
            n = self._done_counts.get(path, 0)
            tag = f"[{n}] " if n else "✓ "
            item.setText(f"{tag}{name}")
        else:
            item.setText(name)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        # Only load file when it's a plain click (no Ctrl/Shift for multi-select).
        mods = QApplication.keyboardModifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        self._select(self.row(item))

    hide_requested = pyqtSignal(list)  # emits list of full paths to hide
    unhide_requested = pyqtSignal(list)  # emits list of full paths to unhide

    def _selected_paths(self) -> list[str]:
        return [self._visible[self.row(it)]
                for it in self.selectedItems()
                if self.row(it) < len(self._visible)]

    def contextMenuEvent(self, event) -> None:
        sel = self._selected_paths()
        if not sel:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        # Check if any selected files are hidden.
        hidden_sel = [p for p in sel if os.path.basename(p) in self._hidden_basenames]
        act_remove = act_hide = act_unhide = None
        if len(sel) == 1:
            name = os.path.basename(sel[0])
            act_remove = menu.addAction(f"Remove: {name}")
            if hidden_sel:
                act_unhide = menu.addAction(f"Unhide: {name}")
            else:
                act_hide = menu.addAction(f"Hide in profile: {name}")
        else:
            act_remove = menu.addAction(f"Remove {len(sel)} files")
            if hidden_sel:
                act_unhide = menu.addAction(f"Unhide {len(hidden_sel)} file(s)")
            non_hidden = [p for p in sel if p not in hidden_sel]
            if non_hidden:
                act_hide = menu.addAction(f"Hide {len(non_hidden)} file(s) in profile")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen == act_remove:
            for path in sel:
                if path in self._path_set:
                    self._paths.remove(path)
                    self._path_set.discard(path)
                    self._done_set.discard(path)
                    self._done_counts.pop(path, None)
            self._rebuild()
        elif chosen == act_hide:
            self.hide_requested.emit(sel)
        elif chosen == act_unhide:
            self.unhide_requested.emit(hidden_sel)


class _KeyFilter(QObject):
    """Suppress global keyboard shortcuts when a text input widget has focus,
    and release focus from input widgets on click-away."""
    _INPUT_TYPES = (QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(obj, QLineEdit):
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            if not isinstance(obj, self._INPUT_TYPES):
                focused = QApplication.focusWidget()
                if isinstance(focused, self._INPUT_TYPES):
                    focused.clearFocus()
        return super().eventFilter(obj, event)


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
    _kf = _KeyFilter(app)
    app.installEventFilter(_kf)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #1e1e1e; color: #ddd; }
        QPushButton { background: #333; border: 1px solid #555; padding: 4px 10px; border-radius: 3px; }
        QPushButton:hover { background: #444; }
        QPushButton:disabled { color: #555; }
        QLineEdit { background: #2a2a2a; border: 1px solid #555; padding: 3px; border-radius: 3px; }
        QComboBox { background: #2a2a2a; border: 1px solid #555; padding: 3px 6px; border-radius: 3px; }
        QComboBox::drop-down { subcontrol-position: right center; width: 18px; border-left: 1px solid #444; }
        QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #888; margin-right: 4px; }
        QComboBox QAbstractItemView { background: #2a2a2a; border: 1px solid #555; selection-background-color: #3a6ea8; }
        QSpinBox, QDoubleSpinBox { background: #2a2a2a; border: 1px solid #555; padding: 3px; border-radius: 3px; }
        QCheckBox::indicator { width: 14px; height: 14px; }
        QListWidget { background: #252525; alternate-background-color: #2a2a2a; }
        QListWidget::item { padding: 4px; color: #ccc; }
        QListWidget::item:alternate { color: #ddd; }
        QListWidget::item:selected { background: #3a6ea8; color: #fff; }
    """)
    win = MainWindow()
    win.show()
    ret = app.exec()
    # Prevent SEGV: ensure the MainWindow (and its child C++ objects) is
    # destroyed while QApplication is still alive, before Python's GC
    # tears down wrappers in arbitrary order.
    del win
    sys.exit(ret)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("8-cut")
        self.resize(1100, 680)
        self.setAcceptDrops(True)

        # Services
        self._db = ProcessedDB()
        self._settings = QSettings("8cut", "8cut")

        # State
        self._file_path: str = ""
        self._cursor: float = 0.0
        self._export_counter: int = 1
        self._export_worker: ExportWorker | None = None
        self._last_export_path: str = ""
        self._overwrite_path: str = ""   # set when a marker is selected for re-export
        self._overwrite_group: list[str] = []  # all output_paths in the selected group
        self._db_worker: _DBWorker | None = None
        self._frame_grabber: FrameGrabber | None = None
        self._fps: float = 25.0  # cached on file load via get_fps()
        self._crop_keyframes: list[tuple[float, float, str | None, bool, bool]] = []  # sorted by time
        self._export_folder: str = ""  # actual folder used for current export (may include suffix)
        self._export_folder_suffix: str = ""

        # Subprofiles — lightweight export variants that append a suffix to the
        # export folder.  Stored in QSettings only (no DB impact).
        _raw = self._settings.value("subprofiles", [])
        if isinstance(_raw, str):
            _raw = [_raw] if _raw else []
        self._subprofiles: list[str] = _raw or []

        # Widgets
        self._playlist = PlaylistWidget()
        self._playlist.file_selected.connect(self._load_file)
        self._playlist.hide_requested.connect(self._on_hide_files)
        self._playlist.unhide_requested.connect(self._on_unhide_files)

        self._mpv = MpvWidget()
        self._mpv.file_loaded.connect(self._after_load)

        self._end_preview = PreviewLabel()

        self._preview_win = SnapPreviewWindow(self)
        self._preview_win.setWindowTitle("End frame")
        self._preview_win.resize(320, 240)
        _pw_layout = QVBoxLayout(self._preview_win)
        _pw_layout.setContentsMargins(0, 0, 0, 0)
        _pw_layout.addWidget(self._end_preview)

        self._preview_timer = QTimer()
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._grab_end_frame)

        self._timeline = TimelineWidget()
        self._timeline.setFixedHeight(160)
        _init_clips = int(self._settings.value("clip_count", "3"))
        _init_spread = float(self._settings.value("spread", "3.0"))
        self._timeline.set_clip_span(8.0 + (_init_clips - 1) * _init_spread)
        self._timeline.cursor_changed.connect(self._on_cursor_changed)
        self._timeline.seek_changed.connect(self._on_seek_changed)
        self._timeline.marker_delete_requested.connect(self._on_delete_marker)
        self._timeline.keyframe_delete_requested.connect(self._on_delete_keyframe)
        self._mpv.time_pos_changed.connect(self._timeline.set_play_position)
        self._timeline.marker_clicked.connect(self._on_marker_clicked)
        self._timeline.marker_deselected.connect(self._on_marker_deselected)

        self._lbl_file = QLabel("← Drop files onto the queue")
        self._lbl_file.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_file.setStyleSheet("color: #aaa; padding: 6px;")
        self._lbl_file.setWordWrap(False)
        from PyQt6.QtWidgets import QSizePolicy
        self._lbl_file.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._btn_play = QPushButton("▶ Play")
        self._btn_play.setEnabled(False)
        self._btn_play.setToolTip("Play selection loop (Space / P)")
        self._btn_play.clicked.connect(self._on_play)

        self._btn_pause = QPushButton("⏸ Pause")
        self._btn_pause.setEnabled(False)
        self._btn_pause.setToolTip("Pause playback (Space / K)")
        self._btn_pause.clicked.connect(self._on_pause)

        self._btn_lock = QPushButton("🔒 Lock")
        self._btn_lock.setCheckable(True)
        self._btn_lock.setToolTip("Lock cursor — click/drag scrubs playback without moving the export point")
        self._btn_lock.toggled.connect(self._on_lock_toggled)

        self._lbl_time = QLabel("-- / --")

        self._txt_name = QLineEdit("clip")
        self._txt_name.setPlaceholderText("base name")
        self._txt_name.setMaximumWidth(150)
        self._txt_name.setToolTip("Base name for exported clips")
        self._txt_name.textChanged.connect(self._reset_counter)

        self._txt_folder = QLineEdit(self._settings.value("export_folder", str(Path.home())))
        self._txt_folder.setToolTip("Export output folder")
        self._txt_folder.textChanged.connect(self._reset_counter)
        self._txt_folder.textChanged.connect(
            lambda v: self._settings.setValue("export_folder", v)
        )
        self._btn_folder = QPushButton("...")
        self._btn_folder.setFixedWidth(30)
        self._btn_folder.setToolTip("Browse for output folder")
        self._btn_folder.clicked.connect(self._pick_folder)
        self._spn_resize = QSpinBox()
        self._spn_resize.setRange(0, 4320)
        self._spn_resize.setSingleStep(64)
        self._spn_resize.setSpecialValueText("off")
        self._spn_resize.setToolTip("Resize short side in pixels (0 = no resize)")
        saved_resize = int(self._settings.value("resize_short_side", "0") or "0")
        self._spn_resize.setValue(saved_resize)
        self._spn_resize.valueChanged.connect(
            lambda v: self._settings.setValue("resize_short_side", str(v))
        )

        self._crop_center: float = float(
            self._settings.value("crop_center", "0.5")
        )

        self._cmb_portrait = QComboBox()
        self._cmb_portrait.addItems(["Off", "9:16", "4:5", "1:1"])
        self._cmb_portrait.setToolTip("Portrait crop ratio (click video to reposition)")
        saved_ratio = self._settings.value("portrait_ratio", "Off")
        idx = self._cmb_portrait.findText(saved_ratio)
        self._cmb_portrait.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_portrait.currentTextChanged.connect(self._on_portrait_ratio_changed)

        self._cmb_format = QComboBox()
        self._cmb_format.setToolTip("Export format")
        self._cmb_format.addItems(["MP4", "WebP sequence"])
        saved_fmt = self._settings.value("export_format", "MP4")
        idx = self._cmb_format.findText(saved_fmt)
        self._cmb_format.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_format.currentTextChanged.connect(
            lambda v: self._settings.setValue("export_format", v)
        )
        self._cmb_format.currentTextChanged.connect(self._update_next_label)

        self._hw_encoders = detect_hw_encoders()
        self._chk_hw = QCheckBox("HW encode")
        if self._hw_encoders:
            self._chk_hw.setToolTip(f"Use GPU encoder ({self._hw_encoders[0]})")
            self._chk_hw.setChecked(
                self._settings.value("hw_encode", "false") == "true"
            )
        else:
            self._chk_hw.setToolTip("No GPU encoder detected")
            self._chk_hw.setEnabled(False)
        self._chk_hw.toggled.connect(
            lambda v: self._settings.setValue("hw_encode", "true" if v else "false")
        )

        self._spn_clips = QSpinBox()
        self._spn_clips.setRange(1, 99)
        self._spn_clips.setToolTip("Number of overlapping 8s clips per export")
        saved_clips = int(self._settings.value("clip_count", "3"))
        self._spn_clips.setValue(saved_clips)
        self._spn_clips.valueChanged.connect(
            lambda v: self._settings.setValue("clip_count", str(v))
        )
        self._spn_clips.valueChanged.connect(
            lambda: self._timeline.set_clip_span(self._clip_span)
        )
        self._spn_clips.valueChanged.connect(lambda: self._update_next_label())
        self._spn_clips.valueChanged.connect(lambda: self._preview_timer.start())
        self._spn_clips.valueChanged.connect(self._update_play_loop)

        self._spn_spread = QDoubleSpinBox()
        self._spn_spread.setRange(2.0, 8.0)
        self._spn_spread.setSingleStep(0.5)
        self._spn_spread.setSuffix("s")
        self._spn_spread.setToolTip("Offset between overlapping 8s clips")
        saved_spread = float(self._settings.value("spread", "3.0"))
        self._spn_spread.setValue(saved_spread)
        self._spn_spread.valueChanged.connect(
            lambda v: self._settings.setValue("spread", str(v))
        )
        self._spn_spread.valueChanged.connect(
            lambda: self._timeline.set_clip_span(self._clip_span)
        )
        self._spn_spread.valueChanged.connect(lambda: self._preview_timer.start())
        self._spn_spread.valueChanged.connect(self._update_play_loop)

        self._chk_rand_portrait = QCheckBox("1 random portrait")
        self._chk_rand_portrait.setToolTip(
            "One random clip per batch gets a random portrait crop (9:16 + random position)"
        )
        self._chk_rand_portrait.setChecked(
            self._settings.value("rand_portrait", "false") == "true"
        )
        self._chk_rand_portrait.toggled.connect(
            lambda v: self._settings.setValue("rand_portrait", "true" if v else "false")
        )
        self._chk_rand_portrait.toggled.connect(self._on_rand_toggle)

        self._chk_rand_square = QCheckBox("1 random square")
        self._chk_rand_square.setToolTip(
            "One random clip per batch gets a random square crop (1:1 + random position)"
        )
        self._chk_rand_square.setChecked(
            self._settings.value("rand_square", "false") == "true"
        )
        self._chk_rand_square.toggled.connect(
            lambda v: self._settings.setValue("rand_square", "true" if v else "false")
        )
        self._chk_rand_square.toggled.connect(self._on_rand_toggle)

        self._chk_track = QCheckBox("Track subject")
        self._chk_track.setToolTip(
            "Auto-adjust crop center per sub-clip using YOLO detection\n"
            "(requires: pip install ultralytics)"
        )
        self._chk_track.setChecked(
            self._settings.value("track_subject", "false") == "true"
        )
        self._chk_track.toggled.connect(
            lambda v: self._settings.setValue("track_subject", "true" if v else "false")
        )

        # ── audio scan controls ──────────────────────────────────────
        self._btn_scan = QPushButton("Scan")
        self._btn_scan.setToolTip("Scan current video for audio segments matching reference clips")
        self._btn_scan.clicked.connect(self._start_scan)

        self._btn_auto_export = QPushButton("Auto")
        self._btn_auto_export.setToolTip("Scan + auto-export best 8s clips")
        self._btn_auto_export.clicked.connect(self._auto_export)

        self._btn_train = QPushButton("Train")
        self._btn_train.setToolTip("Train audio classifier from exported clips")
        self._btn_train.clicked.connect(self._open_train_dialog)
        self._train_worker: TrainWorker | None = None

        self._spn_auto_fuse = QDoubleSpinBox()
        self._spn_auto_fuse.setDecimals(1)
        self._spn_auto_fuse.setRange(0.0, 60.0)
        self._spn_auto_fuse.setSingleStep(1.0)
        self._spn_auto_fuse.setValue(float(self._settings.value("auto_fuse", "4.0")))
        self._spn_auto_fuse.setPrefix("Fuse: ")
        self._spn_auto_fuse.setSuffix("s")
        self._spn_auto_fuse.setToolTip("Max gap between scan regions to merge into one cluster")
        self._spn_auto_fuse.valueChanged.connect(
            lambda v: self._settings.setValue("auto_fuse", str(v))
        )

        self._sld_threshold = QDoubleSpinBox()
        self._sld_threshold.setDecimals(2)
        self._sld_threshold.setRange(0.0, 1.0)
        self._sld_threshold.setSingleStep(0.01)
        self._sld_threshold.setValue(0.30)
        self._sld_threshold.setPrefix("Thr: ")
        self._sld_threshold.setToolTip("Similarity threshold (0=match everything, 1=exact match)")

        self._scan_worker: ScanWorker | None = None

        cpu_count = os.cpu_count() or 2
        self._spn_workers = QSpinBox()
        self._spn_workers.setRange(1, cpu_count)
        self._spn_workers.setToolTip("Max parallel ffmpeg workers for export")
        saved_workers = int(self._settings.value("workers", str(cpu_count)))
        self._spn_workers.setValue(min(saved_workers, cpu_count))
        self._spn_workers.valueChanged.connect(
            lambda v: self._settings.setValue("workers", str(v))
        )

        self._txt_label = QComboBox()
        self._txt_label.setEditable(True)
        self._txt_label.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._txt_label.lineEdit().setPlaceholderText("Sound label (e.g. dog barking)")
        self._txt_label.setMinimumWidth(180)
        self._txt_label.setToolTip("SELVA sound label — persists between exports")
        self._txt_label.addItems(self._db.get_labels())
        saved_label = self._settings.value("sound_label", "")
        self._txt_label.setCurrentText(saved_label)
        self._txt_label.currentTextChanged.connect(
            lambda v: self._settings.setValue("sound_label", v)
        )

        self._cmb_category = QComboBox()
        self._cmb_category.setToolTip("SELVA sound category")
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
        self._btn_export.setToolTip("Export clips at cursor position (E)")
        self._btn_export.clicked.connect(self._on_export)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setToolTip("Cancel running export")
        self._btn_cancel.clicked.connect(self._on_cancel_export)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setEnabled(False)
        self._btn_delete.setToolTip("Delete last export or selected marker from disk and DB")
        self._btn_delete.clicked.connect(self._on_delete_export)

        self._cmb_profile = QComboBox()
        self._cmb_profile.setToolTip("Export profile — each profile has its own set of markers")
        self._cmb_profile.setMinimumWidth(100)
        self._populate_profile_combo()
        saved_profile = self._settings.value("profile", "default")
        idx = self._cmb_profile.findText(saved_profile)
        if idx >= 0:
            self._cmb_profile.setCurrentIndex(idx)
        self._cmb_profile.activated.connect(self._on_profile_activated)

        self._btn_shortcuts = QPushButton("?")
        self._btn_shortcuts.setFixedWidth(28)
        self._btn_shortcuts.setToolTip("Keyboard shortcuts (? or F1)")
        self._btn_shortcuts.clicked.connect(self._show_shortcuts)

        # Right-side layout (video + controls)
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._lbl_file, stretch=1)
        top_bar.addWidget(QLabel("Profile:"))
        top_bar.addWidget(self._cmb_profile)
        top_bar.addWidget(self._btn_shortcuts)

        # Row 1 — transport + export actions
        transport_row = QHBoxLayout()
        transport_row.addWidget(self._btn_play)
        transport_row.addWidget(self._btn_pause)
        transport_row.addWidget(self._btn_lock)
        transport_row.addWidget(self._lbl_time)
        transport_row.addStretch()
        transport_row.addWidget(self._lbl_next)
        transport_row.addWidget(self._btn_export)
        # Subprofile export buttons sit right after Export
        self._subprofile_btns: list[QPushButton] = []
        self._sub_insert_anchor = self._btn_cancel  # buttons inserted before this
        self._btn_add_sub = QPushButton("+")
        self._btn_add_sub.setFixedWidth(28)
        self._btn_add_sub.setToolTip("Add a subprofile — exports to folder_suffix")
        self._btn_add_sub.clicked.connect(self._add_subprofile)
        transport_row.addWidget(self._btn_add_sub)
        transport_row.addWidget(self._btn_cancel)
        transport_row.addWidget(self._spn_workers)
        transport_row.addWidget(self._btn_delete)
        self._transport_row = transport_row
        self._rebuild_subprofile_buttons()

        # Row 2 — annotation + output path
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Label:"))
        path_row.addWidget(self._txt_label)
        path_row.addWidget(QLabel("Cat:"))
        path_row.addWidget(self._cmb_category)
        path_row.addWidget(QLabel("Name:"))
        path_row.addWidget(self._txt_name)
        path_row.addWidget(QLabel("Folder:"))
        path_row.addWidget(self._txt_folder, stretch=1)
        path_row.addWidget(self._btn_folder)

        # Row 3 — video + encoding settings
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Resize:"))
        settings_row.addWidget(self._spn_resize)
        settings_row.addWidget(QLabel("Portrait:"))
        settings_row.addWidget(self._cmb_portrait)
        settings_row.addWidget(QLabel("Format:"))
        settings_row.addWidget(self._cmb_format)
        settings_row.addWidget(self._chk_hw)
        settings_row.addWidget(QLabel("Clips:"))
        settings_row.addWidget(self._spn_clips)
        settings_row.addWidget(QLabel("Spread:"))
        settings_row.addWidget(self._spn_spread)
        settings_row.addWidget(self._chk_rand_portrait)
        settings_row.addWidget(self._chk_rand_square)
        settings_row.addWidget(self._chk_track)
        settings_row.addWidget(self._btn_scan)
        settings_row.addWidget(self._btn_auto_export)
        settings_row.addWidget(self._spn_auto_fuse)
        settings_row.addWidget(self._sld_threshold)
        settings_row.addWidget(self._btn_train)
        settings_row.addStretch()
        self._lbl_status = QLabel()
        self._lbl_status.setStyleSheet("color: #888; font-size: 11px;")
        self._lbl_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._lbl_status.clear())
        settings_row.addWidget(self._lbl_status)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 4, 0)
        right_layout.setSpacing(4)
        right_layout.addLayout(top_bar)
        right_layout.addWidget(self._mpv, stretch=1)
        right_layout.addWidget(self._timeline)
        right_layout.addWidget(self._crop_bar)
        right_layout.addLayout(transport_row)
        right_layout.addLayout(path_row)
        right_layout.addLayout(settings_row)

        # Left: queue header + playlist
        self._btn_open = QPushButton("+ Open Files")
        self._btn_open.setToolTip("Add video files to the queue")
        self._btn_open.clicked.connect(self._on_open_files)

        self._chk_hide_exported = QPushButton("Hide exported")
        self._chk_hide_exported.setCheckable(True)
        self._chk_hide_exported.setToolTip("Hide files that already have exported clips")
        self._chk_hide_exported.setChecked(
            self._settings.value("hide_exported", "false") == "true"
        )
        self._chk_hide_exported.toggled.connect(self._on_hide_exported_toggled)

        self._btn_show_hidden = QPushButton("Show Hidden")
        self._btn_show_hidden.setCheckable(True)
        self._btn_show_hidden.setToolTip("Reveal hidden files so you can right-click to unhide them")
        self._btn_show_hidden.toggled.connect(self._on_show_hidden_toggled)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_top = QHBoxLayout()
        left_top.addWidget(self._btn_open)
        left_top.addWidget(self._chk_hide_exported)
        left_top.addWidget(self._btn_show_hidden)
        left_layout.addLayout(left_top)
        left_layout.addWidget(self._playlist)

        # Root: horizontal splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([200, 900])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        self.setCentralWidget(splitter)
        self.setStatusBar(None)
        if saved_ratio != "Off":
            self._crop_bar.setVisible(True)
            self._mpv.set_crop_overlay(_RATIOS[saved_ratio], self._crop_center)
        else:
            self._update_rand_overlays()

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
        for i in range(1, 10):
            QShortcut(QKeySequence(str(i)), self, context=ctx).activated.connect(
                lambda _, idx=i - 1: self._export_subprofile(idx)
            )
        QShortcut(QKeySequence("M"), self, context=ctx).activated.connect(self._jump_to_next_marker)
        QShortcut(QKeySequence("S"), self, context=ctx).activated.connect(self._jump_to_next_scan_region)
        QShortcut(QKeySequence("N"), self, context=ctx).activated.connect(self._playlist.advance)
        QShortcut(QKeySequence("G"), self, context=ctx).activated.connect(self._btn_lock.toggle)
        QShortcut(QKeySequence("A"), self, context=ctx).activated.connect(self._autoclip)
        for key in ("?", "F1"):
            QShortcut(QKeySequence(key), self, context=ctx).activated.connect(self._show_shortcuts)

        # Resume last session: reload previous playlist files.
        session_files = self._settings.value("session_files", [])
        if session_files:
            valid = [p for p in session_files if os.path.isfile(p)]
            if valid:
                self._playlist.add_files(valid)
                self._apply_playlist_filters()
                if self._playlist.count() > 0:
                    self._playlist._select(0)
                _log(f"Resumed session: {len(valid)} file(s)")

    def _show_shortcuts(self) -> None:
        text = (
            "<table cellpadding='4' style='font-size:13px'>"
            "<tr><td><b>Left / J</b></td><td>Step back 1 frame</td></tr>"
            "<tr><td><b>Right / L</b></td><td>Step forward 1 frame</td></tr>"
            "<tr><td><b>Shift+Left / Shift+J</b></td><td>Step back 1 second</td></tr>"
            "<tr><td><b>Shift+Right / Shift+L</b></td><td>Step forward 1 second</td></tr>"
            "<tr><td><b>Space / P</b></td><td>Play / Pause</td></tr>"
            "<tr><td><b>K</b></td><td>Pause and snap to cursor</td></tr>"
            "<tr><td><b>E</b></td><td>Export</td></tr>"
            "<tr><td><b>1–9</b></td><td>Export to subprofile 1–9</td></tr>"
            "<tr><td><b>M</b></td><td>Jump to next marker</td></tr>"
            "<tr><td><b>S</b></td><td>Jump to next scan region</td></tr>"
            "<tr><td><b>N</b></td><td>Next file in playlist</td></tr>"
            "<tr><td><b>G</b></td><td>Toggle cursor lock</td></tr>"
            "<tr><td><b>A</b></td><td>Autoclip — fit clip count to pause position</td></tr>"
            "<tr><td><b>? / F1</b></td><td>This help</td></tr>"
            "<tr><td colspan='2'><hr></td></tr>"
            "<tr><td><b>Double-click marker</b></td><td>Enter overwrite mode (locked: jump to end of clip span)</td></tr>"
            "<tr><td><b>Right-click marker</b></td><td>Delete clip group</td></tr>"
            "<tr><td><b>Click video / crop bar</b></td><td>Reposition portrait crop</td></tr>"
            "</table>"
        )
        QMessageBox.information(self, "Keyboard shortcuts", text)

    _NEW_PROFILE_SENTINEL = "+ New profile..."

    def _populate_profile_combo(self) -> None:
        """Rebuild profile combo items from DB, preserving selection."""
        self._cmb_profile.blockSignals(True)
        prev = self._cmb_profile.currentText()
        self._cmb_profile.clear()
        existing = self._db.get_profiles()
        if existing:
            self._cmb_profile.addItems(existing)
        else:
            self._cmb_profile.addItem("default")
        self._cmb_profile.addItem(self._NEW_PROFILE_SENTINEL)
        idx = self._cmb_profile.findText(prev)
        if idx >= 0:
            self._cmb_profile.setCurrentIndex(idx)
        self._cmb_profile.blockSignals(False)

    @property
    def _profile(self) -> str:
        text = self._cmb_profile.currentText()
        if text == self._NEW_PROFILE_SENTINEL:
            return "default"
        return text.strip() or "default"

    def _on_profile_activated(self, index: int) -> None:
        text = self._cmb_profile.itemText(index)
        if text == self._NEW_PROFILE_SENTINEL:
            name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
            name = name.strip()
            if ok and name and name != self._NEW_PROFILE_SENTINEL:
                # Insert before the sentinel and select it
                sentinel_idx = self._cmb_profile.count() - 1
                self._cmb_profile.insertItem(sentinel_idx, name)
                self._cmb_profile.setCurrentIndex(sentinel_idx)
            else:
                # Cancelled — revert to previous profile
                prev = self._settings.value("profile", "default")
                idx = self._cmb_profile.findText(prev)
                if idx >= 0:
                    self._cmb_profile.setCurrentIndex(idx)
                return
            text = name
        self._settings.setValue("profile", text)
        # Clear overwrite state — the selected marker belongs to the old profile
        if self._overwrite_path:
            self._overwrite_path = ""
            self._overwrite_group = []
            self._btn_export.setText("Export")
            self._btn_export.setStyleSheet("")
            self._btn_delete.setText("Delete")
            if not self._last_export_path:
                self._btn_delete.setEnabled(False)
        self._update_next_label()
        self._apply_playlist_filters()
        if self._file_path:
            self._refresh_markers()
            _log(f"Profile switched: {text}")
            self._show_status(f"Profile: {text}", 3000)

    # ── Subprofiles ──────────────────────────────────────────

    def _rebuild_subprofile_buttons(self):
        """Recreate the per-subprofile export buttons in the transport row."""
        for btn in self._subprofile_btns:
            self._transport_row.removeWidget(btn)
            btn.deleteLater()
        self._subprofile_btns.clear()
        # Find where to insert: right after the main Export button.
        anchor = self._transport_row.indexOf(self._btn_add_sub)
        has_file = bool(self._file_path)
        for i, name in enumerate(self._subprofiles):
            btn = QPushButton(f"▸ {name}")
            btn.setToolTip(f"Export to folder_{name}  (right-click to remove)")
            btn.setEnabled(has_file)
            btn.clicked.connect(lambda _, s=name: self._on_export(folder_suffix=s))
            self._transport_row.insertWidget(anchor + i, btn)
            self._subprofile_btns.append(btn)

    def _add_subprofile(self):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        for name in self._subprofiles:
            menu.addAction(f"Remove '{name}'", lambda n=name: self._remove_subprofile(n))
        if self._subprofiles:
            menu.addSeparator()
        menu.addAction("Add new…", self._new_subprofile)
        menu.exec(self._btn_add_sub.mapToGlobal(self._btn_add_sub.rect().bottomLeft()))

    def _new_subprofile(self):
        name, ok = QInputDialog.getText(self, "New subprofile", "Suffix name:")
        if ok and name.strip():
            name = name.strip().replace(" ", "_")
            if name not in self._subprofiles:
                self._subprofiles.append(name)
                self._settings.setValue("subprofiles", self._subprofiles)
                self._rebuild_subprofile_buttons()

    def _export_subprofile(self, idx: int):
        if idx < len(self._subprofiles):
            self._on_export(folder_suffix=self._subprofiles[idx])

    def _remove_subprofile(self, name: str):
        if name in self._subprofiles:
            self._subprofiles.remove(name)
            self._settings.setValue("subprofiles", self._subprofiles)
            self._rebuild_subprofile_buttons()

    def _set_subprofile_btns_enabled(self, enabled: bool):
        for btn in self._subprofile_btns:
            btn.setEnabled(enabled)

    def _show_status(self, msg: str, timeout: int = 0) -> None:
        """Show a message in the inline status label. Timeout in ms (0 = sticky)."""
        self._lbl_status.setText(msg)
        if timeout > 0:
            self._status_timer.start(timeout)
        else:
            self._status_timer.stop()

    def _on_hide_exported_toggled(self, hide: bool) -> None:
        self._settings.setValue("hide_exported", "true" if hide else "false")
        self._playlist.set_hide_exported(hide)

    def _on_show_hidden_toggled(self, show: bool) -> None:
        self._playlist.set_show_hidden(show)

    def _on_unhide_files(self, paths: list[str]) -> None:
        """Remove files from the hidden list in the current profile."""
        for path in paths:
            basename = os.path.basename(path)
            self._db.unhide_file(basename, self._profile)
            self._playlist._hidden_basenames.discard(basename)
        self._playlist._rebuild()
        _log(f"Unhid {len(paths)} file(s) in profile {self._profile}")

    def _on_hide_files(self, paths: list[str]) -> None:
        """Persistently hide files in the current profile."""
        for path in paths:
            basename = os.path.basename(path)
            self._db.hide_file(basename, self._profile)
            self._playlist._hidden_basenames.add(basename)
        self._playlist._rebuild()
        _log(f"Hidden {len(paths)} file(s) in profile {self._profile}")

    def _apply_playlist_filters(self) -> None:
        """Apply profile-hidden files, export marks, and hide-exported filter."""
        self._refresh_playlist_checks()
        self._playlist._hide_exported = self._chk_hide_exported.isChecked()
        self._playlist.set_hidden_basenames(self._db.get_hidden_files(self._profile))

    def _on_open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open video files", "",
            "Video files (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.ts);;All files (*)",
        )
        if paths:
            self._playlist.add_files(paths)
            self._apply_playlist_filters()

    def _load_file(self, path: str):
        self._file_path = path
        self._lbl_file.setText(os.path.basename(path))
        self.setWindowTitle(f"8-cut — {os.path.basename(path)}")
        _log(f"Loading: {os.path.basename(path)}")
        self._mpv.load(path)
        # _after_load triggered by MpvWidget.file_loaded signal

    def _after_load(self):
        # Disengage lock and clear keyframes for the new file.
        if self._btn_lock.isChecked():
            self._btn_lock.setChecked(False)
        self._crop_keyframes.clear()
        self._timeline.set_crop_keyframes([])
        self._timeline.clear_scan_regions()
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.cancel()
        self._cleanup_scan_worker()
        self._btn_scan.setEnabled(True)

        dur = self._mpv.get_duration()
        self._timeline.set_duration(dur)
        self._cursor = 0.0
        self._lbl_time.setText(f"{format_time(0.0)} / {format_time(dur)}")
        self._btn_play.setEnabled(True)
        self._btn_pause.setEnabled(True)
        self._btn_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        # Reset stale state from previous file
        self._overwrite_path = ""
        self._overwrite_group = []
        self._last_export_path = ""
        self._btn_export.setText("Export")
        self._btn_export.setStyleSheet("")
        self._btn_delete.setEnabled(False)
        self._btn_delete.setText("Delete")
        self._fps = self._mpv.get_fps()
        vw, vh = self._mpv.get_video_size()
        self._crop_bar.set_source_ratio(vw, vh)
        hwdec_active = self._mpv._player.hwdec_current or "none"
        _log(f"Loaded: {vw}x{vh} @ {self._fps:.2f}fps, duration={format_time(dur)}, hwdec={hwdec_active}")
        # Reset export settings to defaults for the new video
        self._spn_clips.setValue(int(self._settings.value("clip_count", "3")))
        self._spn_spread.setValue(float(self._settings.value("spread", "3.0")))
        self._preview_win.show()
        self._preview_timer.start()
        # Unlock scrollbar after Qt finishes processing layout events from load.

        # Run DB fuzzy match off the main thread — can be slow on large databases.
        filename = os.path.basename(self._file_path)
        self._db_worker = _DBWorker(self._db, filename, self._profile)
        self._db_worker.result.connect(self._on_db_result)
        self._db_worker.start()

    def _on_db_result(self, queried: str, match: object, markers: list) -> None:
        # Discard stale results if the user loaded a different file already.
        if os.path.basename(self._file_path) != queried:
            return
        if match:
            self._show_status(f"⚠ Similar to already processed: {match}")
        else:
            self._lbl_status.clear()
        self._timeline.set_markers(markers)

    def _refresh_markers(self) -> None:
        filename = os.path.basename(self._file_path)
        markers = self._db.get_markers(filename, self._profile)
        self._timeline.set_markers(markers)

    def _refresh_playlist_checks(self) -> None:
        """Re-evaluate marks on every playlist item for the current profile."""
        profile = self._profile
        for path in self._playlist._paths:
            markers = self._db.get_markers(os.path.basename(path), profile)
            if markers:
                self._playlist.mark_done(path, len(markers))
            else:
                self._playlist.unmark_done(path)

    def _on_delete_marker(self, output_path: str) -> None:
        deleted = self._db.delete_group(output_path)
        if not deleted:
            self._db.delete_by_output_path(output_path)
        self._refresh_markers()
        self._refresh_playlist_checks()
        self._update_next_label()
        n = len(deleted) if deleted else 1
        _log(f"Deleted marker: {n} clip(s) from DB")
        self._show_status(
            f"Deleted marker ({n} clip{'s' if n != 1 else ''})", 4000
        )

    def _on_delete_keyframe(self, time: float) -> None:
        self._crop_keyframes = [
            kf for kf in self._crop_keyframes
            if abs(kf[0] - time) > 0.05
        ]
        self._timeline.set_crop_keyframes(self._crop_keyframes)
        _log(f"Deleted crop keyframe @ {format_time(time)} ({len(self._crop_keyframes)} remaining)")
        self._show_status(f"Deleted keyframe @ {format_time(time)}", 3000)

    def _on_marker_clicked(self, start_time: float, output_path: str) -> None:
        # In lock mode, move cursor to the end of this marker's span.
        if self._btn_lock.isChecked():
            meta = self._db.get_by_output_path(output_path)
            clip_count = meta["clip_count"] or self._spn_clips.value() if meta else self._spn_clips.value()
            spread = meta["spread"] or self._spn_spread.value() if meta else self._spn_spread.value()
            next_pos = start_time + 8.0 + (clip_count - 1) * spread
            self._cursor = next_pos
            self._timeline.set_cursor(next_pos)
            self._mpv.seek(next_pos)
            self._lbl_time.setText(f"{format_time(next_pos)} / {format_time(self._mpv.get_duration())}")
            self._update_next_label()
            self._preview_timer.start()
            self._show_status(f"Cursor → end of {os.path.basename(os.path.dirname(output_path))}", 3000)
            return
        self._overwrite_path = output_path
        self._overwrite_group = self._db.get_group(output_path)
        n = len(self._overwrite_group)
        group_dir = os.path.basename(os.path.dirname(output_path))
        if n > 1:
            self._lbl_next.setText(f"↺ {group_dir} ({n} clips)")
            self._btn_delete.setText(f"Delete {group_dir} ({n})")
        else:
            self._lbl_next.setText(f"↺ {os.path.basename(output_path)}")
            self._btn_delete.setText(f"Delete {os.path.basename(output_path)}")
        self._btn_export.setText("Overwrite")
        self._btn_export.setStyleSheet("QPushButton { background: #6a3030; border-color: #a04040; }")
        self._btn_delete.setEnabled(True)
        # Restore config from the original export
        meta = self._db.get_by_output_path(output_path)
        if meta:
            if meta["label"]:
                self._txt_label.setCurrentText(meta["label"])
            if meta["category"]:
                idx = self._cmb_category.findText(meta["category"])
                if idx >= 0:
                    self._cmb_category.setCurrentIndex(idx)
            if meta["short_side"] is not None:
                self._spn_resize.setValue(meta["short_side"])
            ratio = meta["portrait_ratio"] or "Off"
            idx = self._cmb_portrait.findText(ratio)
            if idx >= 0:
                self._cmb_portrait.setCurrentIndex(idx)
            fmt = meta["format"] or "MP4"
            idx = self._cmb_format.findText(fmt)
            if idx >= 0:
                self._cmb_format.setCurrentIndex(idx)
            if meta["clip_count"] is not None:
                self._spn_clips.setValue(meta["clip_count"])
            if meta["spread"] is not None:
                self._spn_spread.setValue(meta["spread"])
            if meta["crop_center"] is not None:
                self._crop_center = meta["crop_center"]
                self._settings.setValue("crop_center", str(self._crop_center))
                self._crop_bar.set_crop_center(self._crop_center)
                if ratio != "Off":
                    self._mpv.set_crop_overlay(_RATIOS[ratio], self._crop_center)
        self._show_status(
            f"Overwrite mode: {group_dir} ({n} clip{'s' if n != 1 else ''}) — export to replace", 5000
        )

    def _on_marker_deselected(self) -> None:
        if self._overwrite_path:
            self._overwrite_path = ""
            self._overwrite_group = []
            self._btn_export.setText("Export")
            self._btn_export.setStyleSheet("")
            self._update_next_label()
            if not self._last_export_path:
                self._btn_delete.setEnabled(False)
            self._btn_delete.setText("Delete")

    def _on_delete_export(self) -> None:
        target = self._overwrite_path or self._last_export_path
        if not target:
            return
        # Resolve the full group (all sub-clips at the same start_time)
        all_paths = self._db.get_group(target)
        if not all_paths:
            all_paths = [target]
        n = len(all_paths)
        group_dir = os.path.basename(os.path.dirname(all_paths[0]))
        if n > 1:
            msg = f"Delete {n} clips in {group_dir} from disk and database?"
        else:
            msg = f"Delete {os.path.basename(target)} from disk and database?"
        reply = QMessageBox.question(
            self, "Delete clips", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Delete all group clips from disk
        folder = self._txt_folder.text()
        for path in all_paths:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                wav = path + ".wav"
                if os.path.exists(wav):
                    os.remove(wav)
            elif os.path.exists(path):
                os.remove(path)
            remove_clip_annotation(folder, path)
        # Remove empty group directory
        parent = os.path.dirname(all_paths[0])
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
        # Remove all from DB
        self._db.delete_group(target)
        # Reset state
        if self._overwrite_path:
            self._overwrite_path = ""
            self._overwrite_group = []
        if self._last_export_path in all_paths:
            self._last_export_path = ""
        self._btn_delete.setEnabled(False)
        self._btn_delete.setText("Delete")
        self._update_next_label()
        self._refresh_markers()
        self._refresh_playlist_checks()
        self._show_status(f"Deleted {n} clip{'s' if n != 1 else ''}: {group_dir}")

    def _on_portrait_ratio_changed(self, text: str) -> None:
        ratio = None if text == "Off" else text
        self._crop_bar.set_portrait_ratio(ratio)
        if ratio is not None:
            self._crop_bar.setVisible(True)
            self._mpv.set_crop_overlay(_RATIOS[ratio], self._crop_center)
        else:
            # Fall back to random overlay guides (or hide)
            self._update_rand_overlays()
        self._settings.setValue("portrait_ratio", text)
        self._update_preview_crop()

    def _on_rand_toggle(self, _checked: bool = False) -> None:
        if self._btn_lock.isChecked():
            self._set_or_remove_crop_keyframe()
        ratio_text = self._cmb_portrait.currentText()
        if ratio_text != "Off":
            return  # manual portrait already controls the overlay
        self._update_rand_overlays()

    def _set_or_remove_crop_keyframe(self) -> None:
        """In lock mode, create a keyframe at the current playback position.

        If the resulting keyframe carries no crop modifications (no ratio,
        no random flags), remove it instead — this handles the undo case
        where the user toggles back to the default state.
        """
        play_t = self._timeline._play_pos
        if play_t is None:
            play_t = self._cursor
        if play_t < 0.1:
            return
        ratio_text = self._cmb_portrait.currentText()
        kf_ratio = None if ratio_text == "Off" else ratio_text
        kf_rand_p = self._chk_rand_portrait.isChecked()
        kf_rand_s = self._chk_rand_square.isChecked()
        # Remove any existing keyframe at this time.
        self._crop_keyframes = [
            kf for kf in self._crop_keyframes
            if abs(kf[0] - play_t) > 0.05
        ]
        # Only insert if the keyframe carries crop modifications.
        if kf_ratio is not None or kf_rand_p or kf_rand_s:
            center = self._crop_center
            self._crop_keyframes.append(
                (play_t, center, kf_ratio, kf_rand_p, kf_rand_s))
            self._crop_keyframes.sort()
            _log(f"Auto keyframe: t={play_t:.2f}s ratio={kf_ratio} rp={kf_rand_p} rs={kf_rand_s}")
        else:
            _log(f"Removed keyframe @ {format_time(play_t)} (no crop modifications)")
        self._timeline.set_crop_keyframes(self._crop_keyframes)

    def _update_rand_overlays(self) -> None:
        """Show lines-only overlay guides for whichever random crop options are on."""
        portrait_on = self._chk_rand_portrait.isChecked()
        square_on = self._chk_rand_square.isChecked()
        overlays: list[tuple[tuple[int,int], float, bool, QColor | None]] = []
        if portrait_on:
            overlays.append((_RATIOS["9:16"], self._crop_center, True, QColor(220, 60, 60, 200)))
        if square_on:
            overlays.append((_RATIOS["1:1"], self._crop_center, True, QColor(60, 180, 220, 200)))
        if overlays:
            # Show the narrower ratio on the crop bar for reference
            bar_ratio = "9:16" if portrait_on else "1:1"
            self._crop_bar.set_portrait_ratio(bar_ratio)
            self._crop_bar.setVisible(True)
            self._mpv.set_crop_overlays(overlays)
        else:
            self._crop_bar.setVisible(False)
            self._mpv.set_crop_overlays([])
        self._update_preview_crop()

    def _on_crop_click(self, frac: float) -> None:
        ratio = self._cmb_portrait.currentText()
        any_rand = self._chk_rand_portrait.isChecked() or self._chk_rand_square.isChecked()
        if ratio == "Off" and not any_rand:
            return
        frac = max(0.0, min(1.0, frac))
        if self._btn_lock.isChecked():
            # Lock mode: set a crop keyframe at the current playback position.
            play_t = self._timeline._play_pos
            if play_t is None:
                play_t = self._cursor
            if play_t < 0.1:
                return
            # Replace existing keyframe at same time, or insert sorted.
            ratio_text = self._cmb_portrait.currentText()
            kf_ratio = None if ratio_text == "Off" else ratio_text
            kf_rand_p = self._chk_rand_portrait.isChecked()
            kf_rand_s = self._chk_rand_square.isChecked()
            self._crop_keyframes = [
                kf for kf in self._crop_keyframes
                if abs(kf[0] - play_t) > 0.05
            ]
            self._crop_keyframes.append((play_t, frac, kf_ratio, kf_rand_p, kf_rand_s))
            self._crop_keyframes.sort()
            self._timeline.set_crop_keyframes(self._crop_keyframes)
            _log(f"Crop keyframe: t={play_t:.2f}s center={frac:.3f} ratio={kf_ratio} rp={kf_rand_p} rs={kf_rand_s} ({len(self._crop_keyframes)} total)")
            self._crop_center = frac
            self._crop_bar.set_crop_center(frac)
            if ratio != "Off":
                self._mpv.set_crop_overlay(_RATIOS[ratio], frac)
            else:
                self._update_rand_overlays()
            self._update_preview_crop()
            return
        self._crop_center = frac
        self._settings.setValue("crop_center", str(self._crop_center))
        self._crop_bar.set_crop_center(self._crop_center)
        if ratio != "Off":
            self._mpv.set_crop_overlay(_RATIOS[ratio], self._crop_center)
        else:
            self._update_rand_overlays()
        self._update_preview_crop()

    # --- End-frame preview ---

    def _grab_end_frame(self):
        if not self._file_path:
            return
        if self._frame_grabber and self._frame_grabber.isRunning():
            # Previous grab still running — retry shortly.
            self._preview_timer.start()
            return
        end_t = self._cursor + self._clip_span
        dur = self._mpv.get_duration()
        if dur:
            end_t = min(end_t, dur)
        self._frame_grabber = FrameGrabber(self._file_path, end_t)
        self._frame_grabber.frame_ready.connect(self._show_end_frame)
        self._frame_grabber.start()

    def _show_end_frame(self, png_data: bytes):
        px = QPixmap()
        px.loadFromData(png_data)
        if not px.isNull():
            self._end_preview.setPixmap(px)
            self._update_preview_crop()

    def _update_preview_crop(self) -> None:
        overlays: list[tuple[tuple[int, int], float, QColor]] = []
        center = self._crop_bar._crop_center
        ratio_text = self._cmb_portrait.currentText()
        if ratio_text != "Off":
            # Manual portrait — red lines.
            overlays.append((_RATIOS[ratio_text], center, QColor(220, 60, 60, 200)))
        else:
            # Random modes.
            if self._chk_rand_portrait.isChecked():
                overlays.append((_RATIOS["9:16"], center, QColor(220, 60, 60, 200)))
            if self._chk_rand_square.isChecked():
                overlays.append((_RATIOS["1:1"], center, QColor(60, 180, 220, 200)))
        self._end_preview.set_overlays(overlays, self._crop_bar._source_ratio)

    # --- Playback ---

    def _on_lock_toggled(self, locked: bool):
        self._timeline._locked = locked
        self._btn_lock.setText("🔒 Lock" if locked else "🔓 Lock")
        if locked:
            self._btn_lock.setStyleSheet("background: #4a3000; border-color: #ffd230;")
        else:
            self._btn_lock.setStyleSheet("")
            # Clear keyframes when unlocking.
            if self._crop_keyframes:
                n = len(self._crop_keyframes)
                self._crop_keyframes.clear()
                self._timeline.set_crop_keyframes([])
                _log(f"Cleared {n} crop keyframe(s)")

    def _on_seek_changed(self, t: float):
        """Lock mode: scrub playback without moving the export cursor."""
        dur = self._mpv.get_duration()
        self._lbl_time.setText(f"{format_time(t)} / {format_time(dur)}")
        self._mpv.seek(t)
        # Update crop bar to show the effective center at this time.
        if self._crop_keyframes:
            kf = resolve_keyframe(self._crop_keyframes, t)
            if kf is not None:
                _, center, ratio, _rp, _rs = kf
                self._crop_bar.set_crop_center(center)
                if ratio is not None:
                    self._mpv.set_crop_overlay(_RATIOS[ratio], center)
                else:
                    self._update_rand_overlays()
            else:
                self._crop_bar.set_crop_center(self._crop_center)
                self._update_rand_overlays()

    def _on_cursor_changed(self, t: float):
        self._cursor = t
        dur = self._mpv.get_duration()
        self._lbl_time.setText(f"{format_time(t)} / {format_time(dur)}")
        self._preview_timer.start()
        if self._mpv.is_playing():
            self._mpv.play_loop(t, t + self._clip_span)
        else:
            self._mpv.seek(t)

    def _toggle_play(self):
        if not self._file_path:
            return
        if self._mpv.is_playing():
            self._on_pause()
        else:
            self._on_play(resume=True)

    @property
    def _clip_span(self) -> float:
        """Total time covered by the overlapping clips."""
        return 8.0 + (self._spn_clips.value() - 1) * self._spn_spread.value()

    def _on_play(self, resume: bool = False):
        if not self._file_path:
            return
        self._mpv.play_loop(self._cursor, self._cursor + self._clip_span, resume=resume)

    def _update_play_loop(self):
        if self._file_path and self._mpv.is_playing():
            self._mpv.update_loop_end(self._cursor + self._clip_span)

    def _on_pause(self):
        self._mpv.stop_loop()

    def _autoclip(self):
        """Set clip count to fit the current pause position."""
        if not self._file_path:
            return
        play_t = self._timeline._play_pos
        if play_t is None or play_t <= self._cursor:
            return
        elapsed = play_t - self._cursor
        spread = self._spn_spread.value()
        # n clips span 8 + (n-1)*spread seconds
        n = int((elapsed - 8.0) / spread) + 1
        n = max(1, n)
        self._spn_clips.setValue(n)

    def _step_cursor(self, delta: float) -> None:
        if not self._file_path:
            return
        dur = self._mpv.get_duration()
        new_t = max(0.0, min(self._cursor + delta, max(0.0, dur - self._clip_span)))
        # Update label and internal state immediately; route the seek through
        # the timeline's debounce timer so rapid key repeats don't hammer mpv.
        self._cursor = new_t
        dur = self._mpv.get_duration()
        self._lbl_time.setText(f"{format_time(new_t)} / {format_time(dur)}")
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

    def _cleanup_scan_worker(self) -> None:
        """Disconnect signals and schedule deletion of old scan worker."""
        if self._scan_worker is not None:
            try:
                self._scan_worker.scan_done.disconnect()
                self._scan_worker.error.disconnect()
                self._scan_worker.progress.disconnect()
            except TypeError:
                pass  # already disconnected
            if self._scan_worker.isRunning():
                # QThread.finished fires when run() returns, even on cancel
                self._scan_worker.finished.connect(self._scan_worker.deleteLater)
            else:
                self._scan_worker.deleteLater()
            self._scan_worker = None

    def _start_scan(self) -> None:
        if not self._file_path:
            self._show_status("No video loaded")
            return
        if self._scan_worker and self._scan_worker.isRunning():
            self._show_status("Scan already running")
            return

        # Clean up previous worker
        self._cleanup_scan_worker()

        threshold = self._sld_threshold.value()

        from core.audio_scan import load_classifier, default_model_path
        model_path = default_model_path(self._profile)
        model = load_classifier(model_path)

        if model is None:
            self._show_status("No trained model — click Train first")
            return

        self._btn_scan.setEnabled(False)
        self._scan_file_path = self._file_path
        self._show_status("Scanning...")
        self._scan_worker = ScanWorker(
            self._file_path, model=model, threshold=threshold,
        )
        self._scan_worker.scan_done.connect(self._on_scan_done)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.progress.connect(self._show_status)
        self._scan_worker.start()

    def _on_scan_done(self, regions: list) -> None:
        self._btn_scan.setEnabled(True)
        self._btn_auto_export.setEnabled(True)
        # Ignore stale results if the user switched files during scan
        if self._file_path != getattr(self, '_scan_file_path', None):
            return
        self._timeline.set_scan_regions(regions)
        self._show_status(f"Scan complete: {len(regions)} matching regions")

    def _on_scan_error(self, msg: str) -> None:
        self._btn_scan.setEnabled(True)
        self._btn_auto_export.setEnabled(True)
        self._show_status(f"Scan error: {msg}")

    # ── Training ────────────────────────────────────────────────

    def _cleanup_train_worker(self) -> None:
        """Disconnect signals and schedule deletion of old train worker."""
        if self._train_worker is not None:
            try:
                self._train_worker.train_done.disconnect()
                self._train_worker.error.disconnect()
                self._train_worker.progress.disconnect()
            except TypeError:
                pass
            if self._train_worker.isRunning():
                self._train_worker.cancel()
                self._train_worker.finished.connect(self._train_worker.deleteLater)
            else:
                self._train_worker.deleteLater()
            self._train_worker = None

    def _open_train_dialog(self):
        """Show the training config dialog and start training if accepted."""
        if self._train_worker and self._train_worker.isRunning():
            self._show_status("Training already in progress…")
            return

        # Default video dir: parent of currently loaded file, or saved setting
        default_dir = ""
        if self._file_path:
            default_dir = os.path.dirname(self._file_path)
        saved_dir = self._settings.value("train_video_dir", default_dir)

        dlg = TrainDialog(self._db, self._profile,
                          video_dir=saved_dir or default_dir, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pos_folder = dlg.positive_folder
        embed_model = dlg.embed_model
        video_dir = dlg.video_dir
        if not pos_folder:
            self._show_status("No positive class selected")
            return

        # Persist video dir for next time
        if video_dir:
            self._settings.setValue("train_video_dir", video_dir)

        video_infos = self._db.get_training_data(
            self._profile, pos_folder, fallback_video_dir=video_dir,
        )
        if not video_infos:
            self._show_status("No training data found for this subprofile")
            return

        from core.audio_scan import default_model_path
        model_path = default_model_path(self._profile)

        self._cleanup_train_worker()
        self._btn_train.setEnabled(False)
        self._show_status(f"Training {embed_model} on {len(video_infos)} videos...")

        self._train_worker = TrainWorker(video_infos, model_path, embed_model)
        self._train_worker.train_done.connect(self._on_train_done)
        self._train_worker.error.connect(self._on_train_error)
        self._train_worker.progress.connect(self._show_status)
        self._train_worker.start()

    def _on_train_done(self, model_path: str):
        self._btn_train.setEnabled(True)
        self._show_status(f"Model trained and saved")
        _log(f"Training complete: {model_path}")

    def _on_train_error(self, msg: str):
        self._btn_train.setEnabled(True)
        self._show_status(f"Training error: {msg}")

    # ── Auto-export ─────────────────────────────────────────────

    def _auto_export(self) -> None:
        """Scan → NMS → export one 8s clip per selected position."""
        if not self._file_path:
            self._show_status("No video loaded")
            return
        if self._export_worker and self._export_worker.isRunning():
            self._show_status("Export already running…")
            return
        if self._scan_worker and self._scan_worker.isRunning():
            self._show_status("Scan already running")
            return

        self._cleanup_scan_worker()
        self._btn_auto_export.setEnabled(False)
        self._btn_scan.setEnabled(False)

        threshold = self._sld_threshold.value()

        from core.audio_scan import load_classifier, default_model_path
        model_path = default_model_path(self._profile)
        model = load_classifier(model_path)

        if model is not None:
            self._scan_file_path = self._file_path
            self._show_status("Auto: scanning with classifier...")
            self._scan_worker = ScanWorker(
                self._file_path, model=model, threshold=threshold,
            )
        else:
            self._show_status("Auto: no trained model — click Train first")
            self._btn_auto_export.setEnabled(True)
            self._btn_scan.setEnabled(True)
            return

        self._scan_worker.scan_done.connect(self._on_auto_scan_done)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.progress.connect(self._show_status)
        self._scan_worker.start()

    @staticmethod
    def _select_export_positions(regions: list[tuple[float, float, float]],
                                 min_gap: float = 2.0,
                                 cluster_fuse: float = 30.0,
                                 ) -> list[float]:
        """Cluster scan regions, then fill each cluster with clips spaced min_gap apart.

        1. Merge overlapping regions into clusters, fusing clusters <cluster_fuse apart.
        2. Within each cluster, greedily pick positions by score, min_gap apart.
        """
        if not regions:
            return []

        # Build clusters — merge overlapping + fuse if gap < cluster_fuse
        sorted_r = sorted(regions, key=lambda r: r[0])
        clusters: list[list[tuple[float, float, float]]] = []
        cur_start, cur_end = sorted_r[0][0], sorted_r[0][1]
        cur_regions = [sorted_r[0]]

        for start, end, score in sorted_r[1:]:
            if start - cur_end <= cluster_fuse:
                cur_end = max(cur_end, end)
                cur_regions.append((start, end, score))
            else:
                clusters.append(cur_regions)
                cur_start, cur_end = start, end
                cur_regions = [(start, end, score)]
        clusters.append(cur_regions)

        # Within each cluster, NMS by score with min_gap
        picked: list[float] = []
        for cluster in clusters:
            by_score = sorted(cluster, key=lambda r: -r[2])
            cluster_picks: list[float] = []
            for start, _end, _score in by_score:
                if all(abs(start - p) >= min_gap for p in cluster_picks):
                    cluster_picks.append(start)
            picked.extend(cluster_picks)

        return sorted(picked)

    def _on_auto_scan_done(self, regions: list) -> None:
        self._btn_scan.setEnabled(True)
        if self._file_path != getattr(self, '_scan_file_path', None):
            self._btn_auto_export.setEnabled(True)
            return

        self._timeline.set_scan_regions(regions)

        if not regions:
            self._show_status("Auto: no regions found")
            self._btn_auto_export.setEnabled(True)
            return

        positions = self._select_export_positions(
            regions, min_gap=2.0, cluster_fuse=self._spn_auto_fuse.value(),
        )
        if not positions:
            self._show_status("Auto: no positions after NMS")
            self._btn_auto_export.setEnabled(True)
            return

        # Build export jobs — one 8s clip per position
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        fmt = self._cmb_format.currentText()
        image_sequence = fmt == "WebP sequence"
        os.makedirs(folder, exist_ok=True)

        # Find starting counter
        counter = 1
        while True:
            if image_sequence:
                p = build_sequence_dir(folder, name, counter, sub=0)
            else:
                p = build_export_path(folder, name, counter, sub=0)
            if not os.path.exists(p):
                break
            counter += 1

        jobs = []
        self._auto_export_positions = []  # stash for DB writes
        for start_t in positions:
            group_dir = os.path.join(folder, f"{name}_{counter:03d}")
            os.makedirs(group_dir, exist_ok=True)
            if image_sequence:
                out = build_sequence_dir(folder, name, counter, sub=0)
            else:
                out = build_export_path(folder, name, counter, sub=0)
            jobs.append((start_t, out, None, 0.5))
            self._auto_export_positions.append((start_t, counter))
            counter += 1

        self._show_status(f"Auto: exporting {len(jobs)} clips...")

        short_side = self._spn_resize.value() or None
        self._export_short_side = short_side
        self._export_portrait = "Off"
        self._export_format = fmt
        self._export_clip_count = 1
        self._export_spread = 0
        self._export_folder = folder
        self._export_folder_suffix = ""

        hw_on = self._chk_hw.isChecked() and self._hw_encoders
        encoder = self._hw_encoders[0] if hw_on else "libx264"
        max_workers = min(self._spn_workers.value(), 3) if hw_on else self._spn_workers.value()

        self._export_worker = ExportWorker(
            self._file_path, jobs,
            short_side=short_side,
            image_sequence=image_sequence,
            max_workers=max_workers,
            encoder=encoder,
        )
        self._export_worker.finished.connect(self._on_auto_clip_done)
        self._export_worker.all_done.connect(self._on_auto_batch_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.cancelled.connect(self._on_export_cancelled)
        self._btn_cancel.setEnabled(True)
        self._btn_export.setEnabled(False)
        self._set_subprofile_btns_enabled(False)
        self._export_worker.start()

    def _on_auto_clip_done(self, path: str):
        """Record each auto-exported clip to DB."""
        # Find the start_time for this clip from stashed positions
        counter_str = os.path.basename(os.path.dirname(path))  # e.g. "clip_042"
        name = self._txt_name.text() or "clip"
        start_t = None
        for t, c in self._auto_export_positions:
            if counter_str == f"{name}_{c:03d}":
                start_t = t
                break

        label = self._txt_label.currentText().strip()
        category = self._cmb_category.currentText()
        self._db.add(
            os.path.basename(self._file_path),
            start_t or 0.0,
            path,
            label=label,
            category=category,
            short_side=self._export_short_side,
            portrait_ratio="",
            crop_center=0.5,
            fmt=self._export_format,
            clip_count=1,
            spread=0,
            profile=self._profile,
            source_path=self._file_path,
        )
        upsert_clip_annotation(self._export_folder, path, label)
        self._show_status(f"Auto: {os.path.basename(path)}")
        _log(f"  auto clip done: {os.path.basename(path)}")

    def _on_auto_batch_done(self):
        n = len(self._auto_export_positions)
        self._btn_auto_export.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._btn_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        self._refresh_markers()
        markers = self._db.get_markers(os.path.basename(self._file_path), self._profile)
        self._playlist.mark_done(self._file_path, len(markers))
        self._update_next_label()
        self._show_status(f"Auto export complete: {n} clips")
        _log(f"Auto export complete: {n} clips")

    def _jump_to_next_scan_region(self) -> None:
        regions = sorted(self._timeline._scan_regions, key=lambda r: r[0])
        if not regions:
            return
        # Merge overlapping regions into clusters so S jumps past each group
        clusters: list[tuple[float, float]] = []
        for (start, end, _score) in regions:
            if clusters and start <= clusters[-1][1]:
                clusters[-1] = (clusters[-1][0], max(clusters[-1][1], end))
            else:
                clusters.append((start, end))
        # Jump to the start of the next cluster after cursor
        for (start, _end) in clusters:
            if start > self._cursor + 0.1:
                self._step_cursor(start - self._cursor)
                return
        # Wrap to first cluster
        self._step_cursor(clusters[0][0] - self._cursor)

    # --- Export ---

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self._txt_folder.setText(folder)  # textChanged fires _reset_counter

    def _reset_counter(self):
        self._update_next_label()

    def _update_next_label(self):
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        is_seq = self._cmb_format.currentText() == "WebP sequence"
        # Find the first counter whose sub-clip _0 does not exist on disk.
        self._export_counter = 1
        while True:
            if is_seq:
                path = build_sequence_dir(folder, name, self._export_counter, sub=0)
            else:
                path = build_export_path(folder, name, self._export_counter, sub=0)
            if not os.path.exists(path):
                break
            self._export_counter += 1
        n = self._spn_clips.value()
        base = f"{name}_{self._export_counter:03d}"
        if n == 1:
            self._lbl_next.setText(f"→ {base}_0")
        else:
            self._lbl_next.setText(f"→ {base}_0..{n - 1}")

    def _on_export(self, _=None, folder_suffix: str = ""):
        if not self._file_path:
            return
        if self._export_worker and self._export_worker.isRunning():
            self._show_status("Export already running…")
            return

        fmt = self._cmb_format.currentText()
        image_sequence = fmt == "WebP sequence"
        folder = self._txt_folder.text()
        if folder_suffix:
            folder = folder.rstrip(os.sep) + "_" + folder_suffix
        os.makedirs(folder, exist_ok=True)
        spread = self._spn_spread.value()

        ratio_text = self._cmb_portrait.currentText()
        base_ratio = None if ratio_text == "Off" else ratio_text
        base_center = self._crop_center
        counter = self._export_counter

        if self._overwrite_path:
            # Group overwrite mode — re-export all sub-clips at this marker.
            # Delete old DB rows first to avoid duplicates on re-insert.
            group_paths = sorted(self._overwrite_group) if self._overwrite_group else [self._overwrite_path]
            for path in group_paths:
                self._db.delete_by_output_path(path)
            jobs = []
            for i, path in enumerate(group_paths):
                start = self._cursor + i * spread
                jobs.append((start, path, base_ratio, base_center))
            self._overwrite_path = ""
            self._overwrite_group = []
            rand_portrait = self._chk_rand_portrait.isChecked()
            rand_square = self._chk_rand_square.isChecked()
            if self._crop_keyframes:
                widened = apply_keyframes_to_jobs(
                    jobs, self._crop_keyframes,
                    base_center=base_center, base_ratio=base_ratio,
                    base_rand_p=rand_portrait, base_rand_s=rand_square,
                )
                # Overwrite re-exports use the keyframe's ratio directly
                # (no random sampling) to reproduce the original output.
                jobs = [(s, o, r, c) for s, o, r, c, _rp, _rs in widened]
        else:
            name = self._txt_name.text() or "clip"
            n_clips = self._spn_clips.value()
            # For subprofile exports, calculate counter independently.
            if folder_suffix:
                counter = 1
                while True:
                    if image_sequence:
                        p = build_sequence_dir(folder, name, counter, sub=0)
                    else:
                        p = build_export_path(folder, name, counter, sub=0)
                    if not os.path.exists(p):
                        break
                    counter += 1
            else:
                counter = self._export_counter
            # Create the group subfolder
            group_dir = os.path.join(folder, f"{name}_{counter:03d}")
            os.makedirs(group_dir, exist_ok=True)
            jobs = []
            for sub in range(n_clips):
                start = self._cursor + sub * spread
                if image_sequence:
                    out = build_sequence_dir(folder, name, counter, sub=sub)
                else:
                    out = build_export_path(folder, name, counter, sub=sub)
                jobs.append((start, out, base_ratio, base_center))

            # Apply crop keyframes (or fall back to base state).
            rand_portrait = self._chk_rand_portrait.isChecked()
            rand_square = self._chk_rand_square.isChecked()
            widened = apply_keyframes_to_jobs(
                jobs, self._crop_keyframes,
                base_center=base_center, base_ratio=base_ratio,
                base_rand_p=rand_portrait, base_rand_s=rand_square,
            )

            # Random crop: eligible clips (per their keyframe flags) have
            # ~1 in 3 chance of getting a random ratio applied.
            portrait_eligible = [i for i, w in enumerate(widened) if w[4]]
            square_eligible = [i for i, w in enumerate(widened) if w[5]]
            rand_indices: dict[int, list[str]] = {}
            if portrait_eligible and n_clips > 1:
                n = max(1, len(portrait_eligible) // 3)
                for i in random.sample(portrait_eligible, min(n, len(portrait_eligible))):
                    rand_indices.setdefault(i, []).append("9:16")
            if square_eligible and n_clips > 1:
                n = max(1, len(square_eligible) // 3)
                for i in random.sample(square_eligible, min(n, len(square_eligible))):
                    rand_indices.setdefault(i, []).append("1:1")

            jobs = []
            for i, (s, o, ratio, center, _rp, _rs) in enumerate(widened):
                if i in rand_indices:
                    ratio = random.choice(rand_indices[i])
                jobs.append((s, o, ratio, center))

        # Subject tracking: re-detect crop center per sub-clip.
        if self._chk_track.isChecked() and any(j[2] for j in jobs):
            starts = [j[0] for j in jobs]
            self._show_status(f"Tracking subject across {len(jobs)} clip(s)…")
            QApplication.processEvents()
            centers = track_centers_for_jobs(
                self._file_path, self._cursor, base_center, starts,
            )
            jobs = [
                (s, o, r, centers[i] if r else c)
                for i, (s, o, r, c) in enumerate(jobs)
            ]

        short_side = self._spn_resize.value() or None

        # Stash export config for _on_clip_done DB writes.
        # Cursor is frozen here — user may move it during async export.
        self._export_cursor = self._cursor
        self._export_short_side = short_side
        self._export_portrait = self._cmb_portrait.currentText()
        self._export_format = fmt
        self._export_clip_count = self._spn_clips.value()
        self._export_spread = self._spn_spread.value()
        self._export_folder = folder
        self._export_folder_suffix = folder_suffix

        self._btn_export.setEnabled(False)
        self._set_subprofile_btns_enabled(False)
        suffix_tag = f" [{folder_suffix}]" if folder_suffix else ""
        self._show_status(f"Exporting {len(jobs)} clip(s){suffix_tag}…")

        # Show one pending marker at the cursor position for the whole batch.
        first_out = jobs[0][1]
        pending = list(self._timeline._markers)
        pending.append((self._cursor, counter, first_out))
        self._timeline.set_markers(pending)

        hw_on = self._chk_hw.isChecked() and self._hw_encoders
        encoder = self._hw_encoders[0] if hw_on else "libx264"
        # GPU encoders have a limited number of concurrent sessions
        # (typically 3–5 on consumer NVIDIA cards), so cap workers.
        max_workers = min(self._spn_workers.value(), 3) if hw_on else self._spn_workers.value()
        _log(f"Export: {len(jobs)} clip(s), encoder={encoder}, workers={max_workers}, "
             f"resize={short_side}, format={fmt}")
        self._export_worker = ExportWorker(
            self._file_path, jobs,
            short_side=short_side,
            image_sequence=image_sequence,
            max_workers=max_workers,
            encoder=encoder,
        )
        self._export_worker.finished.connect(self._on_clip_done)
        self._export_worker.all_done.connect(self._on_batch_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.cancelled.connect(self._on_export_cancelled)
        self._btn_cancel.setEnabled(True)
        self._export_worker.start()

    def _on_clip_done(self, path: str):
        """Called per clip as each finishes."""
        label = self._txt_label.currentText().strip()
        category = self._cmb_category.currentText()
        portrait = self._export_portrait if self._export_portrait != "Off" else ""
        self._db.add(
            os.path.basename(self._file_path),
            self._export_cursor,
            path,
            label=label,
            category=category,
            short_side=self._export_short_side,
            portrait_ratio=portrait,
            crop_center=self._crop_center,
            fmt=self._export_format,
            clip_count=self._export_clip_count,
            spread=self._export_spread,
            profile=self._profile,
            source_path=self._file_path,
        )
        upsert_clip_annotation(self._export_folder, path, label)
        self._last_export_path = path
        _log(f"  clip done: {os.path.basename(path)}")
        self._show_status(f"Exported: {os.path.basename(path)}")

    def _on_batch_done(self):
        """Called once after all clips in the batch are done."""
        _log("Batch complete")
        self._btn_cancel.setEnabled(False)
        self._update_next_label()
        self._btn_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        self._btn_export.setText("Export")
        self._btn_export.setStyleSheet("")
        if self._last_export_path:
            group = os.path.basename(os.path.dirname(self._last_export_path))
            self._show_status(f"Export complete: {group}")
        else:
            self._show_status("Export complete")
        self._btn_delete.setEnabled(True)
        self._btn_delete.setText("Delete")
        self._refresh_markers()
        markers = self._db.get_markers(os.path.basename(self._file_path), self._profile)
        self._playlist.mark_done(self._file_path, len(markers))
        # Refresh label history so the new label is immediately selectable.
        current = self._txt_label.currentText()
        self._txt_label.blockSignals(True)
        self._txt_label.clear()
        self._txt_label.addItems(self._db.get_labels())
        self._txt_label.setCurrentText(current)
        self._txt_label.blockSignals(False)
        # Refresh profile list so new profiles appear in the dropdown.
        self._populate_profile_combo()

    def _on_export_error(self, msg: str):
        _log(f"Export error: {msg}")
        self._btn_cancel.setEnabled(False)
        self._btn_export.setEnabled(True)
        self._btn_auto_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        self._btn_export.setText("Export")
        self._btn_export.setStyleSheet("")
        self._refresh_markers()  # remove stale pending marker
        self._show_status(f"Export error: {msg}")

    def _on_cancel_export(self):
        if self._export_worker and self._export_worker.isRunning():
            self._btn_cancel.setEnabled(False)
            self._export_worker.cancel()
            self._show_status("Cancelling export…")

    def _on_export_cancelled(self):
        _log("Export cancelled")
        self._btn_export.setEnabled(True)
        self._btn_auto_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        self._btn_export.setText("Export")
        self._btn_export.setStyleSheet("")
        self._update_next_label()
        self._refresh_markers()
        markers = self._db.get_markers(os.path.basename(self._file_path), self._profile)
        if markers:
            self._playlist.mark_done(self._file_path, len(markers))
        self._show_status("Export cancelled", 4000)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange and self.isActiveWindow():
            if self._preview_win.isVisible():
                self._preview_win.raise_()

    def closeEvent(self, event):
        _log("Shutting down…")
        # Save session playlist for resume.
        self._settings.setValue("session_files", self._playlist._paths)
        # Cancel background workers to prevent callbacks into dead objects.
        self._cleanup_scan_worker()
        self._cleanup_train_worker()
        # Stop timers first to prevent callbacks into dead objects.
        self._preview_timer.stop()
        self._mpv._render_timer.stop()
        # Free the OpenGL render context before Qt tears down the GL surface.
        if self._mpv._render_ctx:
            self._mpv._render_ctx.free()
            self._mpv._render_ctx = None
        # Terminate the mpv player (joins its background threads).
        if self._mpv._player:
            self._mpv._player.terminate()
            self._mpv._player = None
        self._mpv._fbo = None
        self._preview_win.close()
        _log("Shutdown complete")
        super().closeEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        # Defer follow_main so the window manager has committed the new geometry.
        QTimer.singleShot(0, self._preview_win.follow_main)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._preview_win.follow_main)

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
            self._apply_playlist_filters()

if __name__ == "__main__":
    main()
