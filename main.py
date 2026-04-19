#!/usr/bin/env python3
import locale
locale.setlocale(locale.LC_NUMERIC, "C")  # required by libmpv before any import

import sys
import os
import random
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog,
    QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QToolTip,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QFormLayout,
    QTableWidget, QTableWidgetItem, QTabWidget, QHeaderView,
)
from PyQt6.QtCore import Qt, QObject, QThread, QTimer, QRect, QSize, pyqtSignal, QSettings
from PyQt6.QtGui import QPainter, QColor, QPen, QPixmap, QDragEnterEvent, QDropEvent, QCursor, QFont, QKeySequence, QShortcut
if sys.platform == "win32":
    # Help ctypes find libmpv-2.dll next to main.py or in frozen bundle
    _dll_dir = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
    os.add_dll_directory(str(_dll_dir))
    os.environ["PATH"] = str(_dll_dir) + os.pathsep + os.environ.get("PATH", "")
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
                 threshold: float = 0.30,
                 prefetched_audio=None):
        super().__init__()
        self._video_path = video_path
        self._model = model
        self._threshold = threshold
        self._prefetched_audio = prefetched_audio
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
                prefetched_audio=self._prefetched_audio,
            )
            self._prefetched_audio = None  # free memory
            if not self._cancel:
                self.scan_done.emit(regions)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))


class DatasetStatsDialog(QDialog):
    """Per-video dataset breakdown with class balance visualization."""

    def __init__(self, video_infos: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dataset Statistics")
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout(self)

        # ── Totals ────────────────────────────────────────────
        n_pos = sum(len(vi[1]) for vi in video_infos)
        n_soft = sum(len(vi[2]) for vi in video_infos)
        n_neg = sum(len(vi[3]) for vi in video_infos)
        n_total = n_pos + n_soft + n_neg

        totals = QLabel(
            f"<b>{len(video_infos)}</b> videos &nbsp;|&nbsp; "
            f"<b>{n_total}</b> total clips &nbsp;|&nbsp; "
            f"<span style='color:#4a4'>■</span> {n_pos} positive &nbsp; "
            f"<span style='color:#aa4'>■</span> {n_soft} soft &nbsp; "
            f"<span style='color:#a44'>■</span> {n_neg} negative"
        )
        layout.addWidget(totals)

        # ── Class balance bar ─────────────────────────────────
        if n_total > 0:
            class _BalanceBar(QWidget):
                def __init__(self, pos, soft, neg, total):
                    super().__init__()
                    self._fracs = (pos / total, soft / total, neg / total)
                    self.setFixedHeight(20)

                def paintEvent(self, _ev):
                    p = QPainter(self)
                    w = self.width()
                    colors = [QColor(80, 170, 80), QColor(170, 170, 60), QColor(170, 70, 70)]
                    x = 0
                    for frac, col in zip(self._fracs, colors):
                        bw = int(frac * w)
                        if bw > 0:
                            p.fillRect(x, 0, bw, 20, col)
                            x += bw
                    p.end()

            balance = _BalanceBar(n_pos, n_soft, n_neg, n_total)
            layout.addWidget(balance)

        # ── Per-video table ───────────────────────────────────
        table = QTableWidget(len(video_infos), 5)
        table.setHorizontalHeaderLabels(["Video", "Pos", "Soft", "Neg", "Total"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 5):
            table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)

        for row, (path, pos, soft, neg) in enumerate(video_infos):
            name = os.path.basename(path)
            table.setItem(row, 0, QTableWidgetItem(name))
            for col, val in enumerate([len(pos), len(soft), len(neg),
                                       len(pos) + len(soft) + len(neg)], 1):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        table.sortItems(1, Qt.SortOrder.DescendingOrder)
        layout.addWidget(table)

        # ── Warnings ──────────────────────────────────────────
        warnings = []
        if n_pos == 0:
            warnings.append("No positive clips — export some clips first.")
        elif n_pos < 20:
            warnings.append(f"Only {n_pos} positive clips — aim for 20+ for decent results.")
        # Check for videos with zero positives (only negatives)
        neg_only = sum(1 for vi in video_infos if len(vi[1]) == 0 and len(vi[3]) > 0)
        if neg_only:
            warnings.append(f"{neg_only} video(s) have only negatives, no positives.")
        # Check balance ratio
        if n_pos > 0 and n_neg > 0 and (n_neg / n_pos > 5 or n_pos / n_neg > 5):
            warnings.append("Class imbalance >5:1 — consider adding more of the minority class.")
        if warnings:
            lbl = QLabel("<br>".join(f"⚠ {w}" for w in warnings))
            lbl.setStyleSheet("color: #cc8800;")
            layout.addWidget(lbl)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.close)
        layout.addWidget(btns)


class HardNegativesDialog(QDialog):
    """View and manage hard negative training examples."""

    def __init__(self, db: ProcessedDB, profile: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hard Negatives")
        self.setMinimumSize(600, 400)
        self._db = db
        self._profile = profile

        layout = QVBoxLayout(self)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter model:"))
        self._cmb_filter = QComboBox()
        self._cmb_filter.addItem("(all)")
        self._cmb_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._cmb_filter, 1)
        layout.addLayout(filter_row)

        # Summary
        self._lbl_summary = QLabel()
        layout.addWidget(self._lbl_summary)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["File", "Time", "Source Model", "ID"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnHidden(3, True)  # hide ID column
        layout.addWidget(self._table)

        # Buttons
        btn_row = QHBoxLayout()
        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(btn_delete)
        btn_clear = QPushButton("Clear All")
        btn_clear.clicked.connect(self._clear_all)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._load()

    def _load(self):
        rows = self._db.get_hard_negatives(self._profile)
        models = sorted(set(r["source_model"] for r in rows if r["source_model"]))
        self._cmb_filter.blockSignals(True)
        self._cmb_filter.clear()
        self._cmb_filter.addItem("(all)")
        for m in models:
            self._cmb_filter.addItem(m)
        self._cmb_filter.blockSignals(False)

        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._table.setItem(i, 0, QTableWidgetItem(r["filename"]))
            self._table.setItem(i, 1, QTableWidgetItem(f'{r["start_time"]:.1f}s'))
            self._table.setItem(i, 2, QTableWidgetItem(r["source_model"]))
            self._table.setItem(i, 3, QTableWidgetItem(str(r["id"])))
        self._lbl_summary.setText(f"<b>{len(rows)}</b> hard negatives")

    def _apply_filter(self):
        model = self._cmb_filter.currentText()
        for row in range(self._table.rowCount()):
            if model == "(all)":
                self._table.setRowHidden(row, False)
            else:
                src = self._table.item(row, 2).text()
                self._table.setRowHidden(row, src != model)

    def _delete_selected(self):
        ids = []
        for row in sorted(set(i.row() for i in self._table.selectedItems()), reverse=True):
            if not self._table.isRowHidden(row):
                ids.append(int(self._table.item(row, 3).text()))
        if ids:
            self._db.delete_hard_negatives_by_ids(ids)
            self._load()

    def _clear_all(self):
        all_rows = self._db.get_hard_negatives(self._profile)
        model_filter = self._cmb_filter.currentText()
        if model_filter != "(all)":
            target = [r for r in all_rows if r["source_model"] == model_filter]
            msg = f"Delete {len(target)} hard negatives for model '{model_filter}'?"
        else:
            target = all_rows
            msg = f"Delete all {len(target)} hard negatives for profile '{self._profile}'?"
        if not target:
            return
        reply = QMessageBox.question(
            self, "Clear All", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_hard_negatives_by_ids([r["id"] for r in target])
            self._load()


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
        self._cmb_negative = QComboBox()
        self._cmb_negative.addItem("(auto only)", userData="")
        self._populate_folder_combos()
        if self._cmb_positive.count() == 0:
            form.addRow("", QLabel("No exported clips found for this profile."))
        form.addRow("Positive class:", self._cmb_positive)

        # Negative class selector (optional)
        self._cmb_negative.currentIndexChanged.connect(lambda: self._debounce.start())
        form.addRow("Negative class:", self._cmb_negative)

        # Model selector
        self._cmb_model = QComboBox()
        for name in _EMBED_MODELS:
            self._cmb_model.addItem(name)
        self._cmb_model.setCurrentText("HUBERT_XLARGE")
        form.addRow("Model:", self._cmb_model)

        # Auto-negative margin (0 = disabled)
        self._spn_neg_margin = QDoubleSpinBox()
        self._spn_neg_margin.setDecimals(0)
        self._spn_neg_margin.setRange(0.0, 600.0)
        self._spn_neg_margin.setSingleStep(10.0)
        self._spn_neg_margin.setValue(30.0)
        self._spn_neg_margin.setSuffix("s")
        self._spn_neg_margin.setSpecialValueText("Disabled")
        self._spn_neg_margin.setToolTip(
            "Auto-sample negatives from regions this far from any marker. 0 = disabled.")
        form.addRow("Auto-neg margin:", self._spn_neg_margin)

        self._chk_scan_exports = QCheckBox("Include scan-exported clips in training")
        self._chk_scan_exports.setToolTip("When checked, clips auto-exported from scan results are included as training data")
        self._chk_scan_exports.stateChanged.connect(lambda: self._debounce.start())
        form.addRow("", self._chk_scan_exports)

        self._chk_hard_negatives = QCheckBox("Use hard negatives in training")
        self._chk_hard_negatives.setChecked(True)
        self._chk_hard_negatives.setToolTip(
            "When unchecked, manually marked hard negatives are excluded from training.\n"
            "Useful when training a new model type where old negatives may not apply.")
        self._chk_hard_negatives.stateChanged.connect(lambda: self._debounce.start())
        neg_row = QHBoxLayout()
        neg_row.addWidget(self._chk_hard_negatives)
        btn_manage_neg = QPushButton("Manage\u2026")
        btn_manage_neg.setFixedWidth(80)
        btn_manage_neg.clicked.connect(self._manage_negatives)
        neg_row.addWidget(btn_manage_neg)
        form.addRow("", neg_row)

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
        self._lbl_video_dir = QLabel("Video dir:")
        self._video_dir_widget = QWidget()
        self._video_dir_widget.setLayout(vid_row)
        form.addRow(self._lbl_video_dir, self._video_dir_widget)
        # Hidden by default — shown only if some videos are missing source_path
        self._lbl_video_dir.setVisible(False)
        self._video_dir_widget.setVisible(False)

        layout.addLayout(form)

        # Stats summary with details button
        stats_row = QHBoxLayout()
        self._lbl_stats = QLabel()
        stats_row.addWidget(self._lbl_stats, 1)
        self._btn_details = QPushButton("Details…")
        self._btn_details.setFixedWidth(70)
        self._btn_details.clicked.connect(self._show_details)
        self._btn_details.setEnabled(False)
        stats_row.addWidget(self._btn_details, 0, Qt.AlignmentFlag.AlignTop)
        self._video_infos: list = []
        self._update_stats()
        self._cmb_positive.currentIndexChanged.connect(self._update_stats)
        layout.addLayout(stats_row)

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

    def _manage_negatives(self):
        dlg = HardNegativesDialog(self._db, self._profile, parent=self)
        dlg.exec()
        self._debounce.start()  # refresh stats after potential deletions

    def _populate_folder_combos(self):
        """Rebuild positive/negative combo box items from DB stats."""
        inc_scan = getattr(self, '_chk_scan_exports', None)
        inc = inc_scan.isChecked() if inc_scan else False
        prev_pos = self._cmb_positive.currentData()
        prev_neg = self._cmb_negative.currentData()
        self._cmb_positive.blockSignals(True)
        self._cmb_negative.blockSignals(True)
        self._cmb_positive.clear()
        # Keep "(auto only)" as first item in negative, remove the rest
        while self._cmb_negative.count() > 1:
            self._cmb_negative.removeItem(1)
        stats = self._db.get_training_stats(self._profile, include_scan_exports=inc)
        for folder_name, info in stats.items():
            label = f"{folder_name}  ({info['videos']} videos, {info['clips']} clips)"
            self._cmb_positive.addItem(label, userData=folder_name)
            self._cmb_negative.addItem(label, userData=folder_name)
        # Restore previous selection if still present
        if prev_pos:
            idx = self._cmb_positive.findData(prev_pos)
            if idx >= 0:
                self._cmb_positive.setCurrentIndex(idx)
        if prev_neg:
            idx = self._cmb_negative.findData(prev_neg)
            if idx >= 0:
                self._cmb_negative.setCurrentIndex(idx)
        self._cmb_positive.blockSignals(False)
        self._cmb_negative.blockSignals(False)

    def _update_stats(self):
        self._populate_folder_combos()
        folder = self._cmb_positive.currentData()
        if not folder:
            self._lbl_stats.setText("No export folder data available.")
            return
        neg_folder = self._cmb_negative.currentData() or ""
        inc_scan = self._chk_scan_exports.isChecked()
        use_neg = self._chk_hard_negatives.isChecked()
        # First check without fallback to see if source_paths are sufficient
        video_infos_no_fb = self._db.get_training_data(
            self._profile, folder, negative_folder=neg_folder,
            include_scan_exports=inc_scan,
            use_hard_negatives=use_neg,
        )
        video_infos = self._db.get_training_data(
            self._profile, folder, negative_folder=neg_folder,
            fallback_video_dir=self._txt_video_dir.text(),
            include_scan_exports=inc_scan,
            use_hard_negatives=use_neg,
        )
        # Show video dir field only when the fallback helps find extra videos
        needs_fallback = len(video_infos) > len(video_infos_no_fb) or len(video_infos_no_fb) == 0
        self._lbl_video_dir.setVisible(needs_fallback)
        self._video_dir_widget.setVisible(needs_fallback)

        self._video_infos = video_infos
        self._btn_details.setEnabled(len(video_infos) > 0)
        n_videos = len(video_infos)
        n_pos = sum(len(vi[1]) for vi in video_infos)
        n_soft = sum(len(vi[2]) for vi in video_infos)
        n_neg = sum(len(vi[3]) for vi in video_infos)
        lines = [f"<b>{n_videos}</b> videos"]
        lines.append(f"<b>{n_pos}</b> positive, <b>{n_soft}</b> soft/buffer"
                     + (f", <b>{n_neg}</b> manual negative" if n_neg else "")
                     + " markers")
        if n_videos == 0:
            lines.append("<i>No source videos found. Set Video dir below.</i>")
            self._lbl_video_dir.setVisible(True)
            self._video_dir_widget.setVisible(True)
        elif n_videos < 3:
            lines.append("<i>Recommend at least 3 videos for decent results.</i>")
        self._lbl_stats.setText("<br>".join(lines))

    def _show_details(self):
        if self._video_infos:
            dlg = DatasetStatsDialog(self._video_infos, parent=self)
            dlg.exec()

    @property
    def positive_folder(self) -> str:
        return self._cmb_positive.currentData() or ""

    @property
    def negative_folder(self) -> str:
        return self._cmb_negative.currentData() or ""

    @property
    def neg_margin(self) -> float:
        return self._spn_neg_margin.value()

    @property
    def embed_model(self) -> str:
        return self._cmb_model.currentText()

    @property
    def video_dir(self) -> str:
        return self._txt_video_dir.text()

    @property
    def include_scan_exports(self) -> bool:
        return self._chk_scan_exports.isChecked()

    @property
    def use_hard_negatives(self) -> bool:
        return self._chk_hard_negatives.isChecked()


class TrainWorker(QThread):
    """Trains an audio classifier off the main thread."""
    train_done = pyqtSignal(str)   # emits model path on success
    error = pyqtSignal(str)
    progress = pyqtSignal(str)     # per-video status

    def __init__(self, video_infos: list, model_path: str,
                 embed_model: str | None = None, n_workers: int = 4,
                 neg_margin: float = 120.0):
        super().__init__()
        self._video_infos = video_infos
        self._model_path = model_path
        self._embed_model = embed_model
        self._n_workers = n_workers
        self._neg_margin = neg_margin
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
                neg_margin=self._neg_margin,
                embed_model=self._embed_model,
                cancel_flag=self,
                n_workers=self._n_workers,
                progress_cb=self.progress.emit,
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


class ScanResultsPanel(QWidget):
    """Tabbed panel showing scan results per model, with disable/resize/negatives."""
    seek_requested = pyqtSignal(float)   # request main window to seek to time
    export_requested = pyqtSignal(list)  # emit list of (start, end, score) to export
    negatives_requested = pyqtSignal(list)  # emit list of start times to mark as hard negatives
    negatives_removed = pyqtSignal(list)   # emit list of start times to un-mark as negatives
    tab_changed = pyqtSignal()           # active tab changed
    regions_edited = pyqtSignal()        # a region was resized or toggled

    # UserRole slots per item:
    #   col 0: UserRole   = row_id (int)
    #   col 0: UserRole+1 = start_time (float)
    #   col 0: UserRole+2 = disabled (bool)
    #   col 1: UserRole   = end_time (float)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self._filename = ""
        self._profile = ""
        self._neg_times: set[float] = set()
        self._editing = False  # guard against cellChanged during programmatic updates
        self._undo_stack: list[tuple] = []  # list of (action, *data)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(False)
        self._tabs.currentChanged.connect(lambda: self.tab_changed.emit())
        layout.addWidget(self._tabs)

        btn_row = QHBoxLayout()
        self._btn_neg = QPushButton("Add to Negatives")
        self._btn_neg.setToolTip("Mark selected rows as hard-negative training examples")
        self._btn_neg.clicked.connect(self._on_add_negatives)
        self._btn_export = QPushButton("Export Scan Results")
        self._btn_export.setToolTip("Export clips from the active tab's scan results")
        self._btn_export.clicked.connect(self._on_export)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_neg)
        btn_row.addWidget(self._btn_export)
        layout.addLayout(btn_row)

    @staticmethod
    def _parse_time(text: str) -> float | None:
        """Parse 'M:SS.S' or 'H:MM:SS.S' back to seconds. Returns None on failure."""
        try:
            parts = text.strip().split(":")
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except (ValueError, IndexError):
            pass
        return None

    def _current_table(self) -> QTableWidget | None:
        """Return the QTableWidget from the active tab (unwrapping container)."""
        w = self._tabs.currentWidget()
        if isinstance(w, QTableWidget):
            return w
        if w is not None:
            table = w.findChild(QTableWidget)
            if table is not None:
                return table
        return None

    def _tab_table(self, index: int) -> QTableWidget | None:
        """Return the QTableWidget from a tab by index."""
        w = self._tabs.widget(index)
        if isinstance(w, QTableWidget):
            return w
        if w is not None:
            table = w.findChild(QTableWidget)
            if table is not None:
                return table
        return None

    def load_for_file(self, filename: str, profile: str) -> None:
        """Load saved scan results from DB for a file."""
        self._filename = filename
        self._profile = profile
        self._neg_times = self._db.get_hard_negative_times(filename, profile)
        self._tabs.clear()
        results = self._db.get_scan_results(filename, profile)
        for model, rows in results.items():
            self._add_tab(model, rows)
        self._populate_version_combos()

    def add_scan_results(self, model: str,
                         regions: list[tuple[float, float, float]]) -> None:
        """Add/replace a tab with new scan results and save to DB."""
        self._db.save_scan_results(self._filename, self._profile, model, regions)
        db_results = self._db.get_scan_results(self._filename, self._profile)
        rows = db_results.get(model, [])
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).rsplit(" (", 1)[0] == model:
                self._tabs.removeTab(i)
                break
        self._add_tab(model, rows)
        self._populate_version_combos()
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).rsplit(" (", 1)[0] == model:
                self._tabs.setCurrentIndex(i)
                break

    def _add_tab(self, model: str,
                 rows: list[tuple[int, float, float, float, bool, float, float]]) -> None:
        """Create a table tab wrapped in a container with a version combo.

        rows: [(row_id, start, end, score, disabled, orig_start, orig_end), ...]
        """
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(2)

        cmb_version = QComboBox()
        cmb_version.setMaximumWidth(260)
        cmb_version.setToolTip("Scan version history")
        cmb_version.hide()  # Hidden when only 1 version
        cmb_version.currentIndexChanged.connect(
            lambda idx, m=model: self._on_version_changed(m, idx))
        container_layout.addWidget(cmb_version)

        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["Time", "End", "Score"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # Allow double-click editing on Time/End columns only
        table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        red = QColor(220, 60, 60)
        gray = QColor(100, 100, 100)
        self._editing = True
        for i, (row_id, start, end, score, disabled, os_, oe) in enumerate(rows):
            t_item = QTableWidgetItem(format_time(start))
            t_item.setData(Qt.ItemDataRole.UserRole, row_id)
            t_item.setData(Qt.ItemDataRole.UserRole + 1, start)
            t_item.setData(Qt.ItemDataRole.UserRole + 2, disabled)
            t_item.setData(Qt.ItemDataRole.UserRole + 3, os_)  # orig_start
            t_item.setData(Qt.ItemDataRole.UserRole + 4, oe)   # orig_end
            table.setItem(i, 0, t_item)

            e_item = QTableWidgetItem(format_time(end))
            e_item.setData(Qt.ItemDataRole.UserRole, end)
            table.setItem(i, 1, e_item)

            sc_item = QTableWidgetItem(f"{score:.2f}")
            sc_item.setFlags(sc_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(i, 2, sc_item)

            # Color: disabled (gray) > negative (red) > default
            if disabled:
                for col in range(3):
                    table.item(i, col).setForeground(gray)
            elif start in self._neg_times:
                for col in range(3):
                    table.item(i, col).setForeground(red)
        self._editing = False

        table.itemSelectionChanged.connect(
            lambda t=table: self._on_selection_changed(t))
        table.cellChanged.connect(
            lambda r, c, t=table: self._on_cell_changed(t, r, c))
        container_layout.addWidget(table)
        self._tabs.addTab(container, f"{model} ({len(rows)})")

    def _populate_version_combos(self) -> None:
        """Populate version combo boxes for all tabs from DB."""
        for i in range(self._tabs.count()):
            w = self._tabs.widget(i)
            if w is None:
                continue
            cmb = w.findChild(QComboBox)
            if cmb is None:
                continue
            model = self._tabs.tabText(i).rsplit(" (", 1)[0]
            versions = self._db.get_scan_versions(
                self._filename, self._profile, model)
            cmb.blockSignals(True)
            cmb.clear()
            for v in versions:
                ts = v["timestamp"]
                # Parse timestamp to readable date string
                try:
                    dt = datetime.strptime(ts[:15], "%Y%m%d_%H%M%S")
                    date_str = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, IndexError):
                    date_str = ts
                label = (f"{date_str}"
                         f" ({v['count']} regions, best: {v['max_score']:.2f})")
                cmb.addItem(label, userData=ts)
            cmb.blockSignals(False)
            cmb.setVisible(cmb.count() > 1)

    def _on_version_changed(self, model: str, idx: int) -> None:
        """Reload a tab's results when the user selects a different version."""
        if idx < 0:
            return
        self._undo_stack.clear()  # version context changed, old undo entries invalid
        # Find the tab for this model
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).rsplit(" (", 1)[0] == model:
                w = self._tabs.widget(i)
                cmb = w.findChild(QComboBox) if w else None
                if cmb is None:
                    return
                ts = cmb.itemData(idx)
                if ts is None:
                    return
                results = self._db.get_scan_results(
                    self._filename, self._profile, scan_timestamp=ts)
                rows = results.get(model, [])
                # Replace the table contents
                table = self._tab_table(i)
                if table is None:
                    return
                self._editing = True
                table.setRowCount(len(rows))
                red = QColor(220, 60, 60)
                gray = QColor(100, 100, 100)
                for r, (row_id, start, end, score, disabled, os_, oe) in enumerate(rows):
                    t_item = QTableWidgetItem(format_time(start))
                    t_item.setData(Qt.ItemDataRole.UserRole, row_id)
                    t_item.setData(Qt.ItemDataRole.UserRole + 1, start)
                    t_item.setData(Qt.ItemDataRole.UserRole + 2, disabled)
                    t_item.setData(Qt.ItemDataRole.UserRole + 3, os_)
                    t_item.setData(Qt.ItemDataRole.UserRole + 4, oe)
                    table.setItem(r, 0, t_item)
                    e_item = QTableWidgetItem(format_time(end))
                    e_item.setData(Qt.ItemDataRole.UserRole, end)
                    table.setItem(r, 1, e_item)
                    sc_item = QTableWidgetItem(f"{score:.2f}")
                    sc_item.setFlags(sc_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(r, 2, sc_item)
                    if disabled:
                        for col in range(3):
                            table.item(r, col).setForeground(gray)
                    elif start in self._neg_times:
                        for col in range(3):
                            table.item(r, col).setForeground(red)
                self._editing = False
                self._tabs.setTabText(i, f"{model} ({len(rows)})")
                self.regions_edited.emit()
                return

    def current_model_name(self) -> str:
        """Return the model name of the currently active tab."""
        idx = self._tabs.currentIndex()
        if idx >= 0:
            return self._tabs.tabText(idx).split(" (")[0]
        return ""

    def _on_selection_changed(self, table: QTableWidget) -> None:
        items = table.selectedItems()
        if items:
            row = items[0].row()
            start = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
            if start is not None:
                self.seek_requested.emit(float(start))

    def _on_cell_changed(self, table: QTableWidget, row: int, col: int) -> None:
        """Handle user editing a Time or End cell — parse and update DB."""
        if self._editing or col > 1:
            return
        item = table.item(row, col)
        if item is None:
            return
        # Capture old value before parsing
        if col == 0:
            old_val = item.data(Qt.ItemDataRole.UserRole + 1)
        else:
            old_val = item.data(Qt.ItemDataRole.UserRole)
        new_val = self._parse_time(item.text())
        if new_val is None:
            self._editing = True
            item.setText(format_time(old_val))
            self._editing = False
            return
        # Record undo: (action, tab_index, row, col, old_value)
        tab_idx = self._tabs.indexOf(table.parent() or table)
        self._undo_stack.append(("resize", tab_idx, row, col, float(old_val)))
        # Update stored data
        self._editing = True
        item.setText(format_time(new_val))
        if col == 0:
            item.setData(Qt.ItemDataRole.UserRole + 1, new_val)
        else:
            item.setData(Qt.ItemDataRole.UserRole, new_val)
        self._editing = False
        # Persist to DB
        row_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        start = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        end = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        if row_id is not None:
            self._db.update_scan_result_times(row_id, float(start), float(end))
        self.regions_edited.emit()

    def toggle_disable_selected(self) -> None:
        """Toggle disabled state on selected rows."""
        table = self._current_table()
        if table is None:
            return
        selected_rows = sorted({idx.row() for idx in table.selectedIndexes()})
        if not selected_rows:
            return
        # Record undo: (action, tab_index, [(row, old_disabled), ...])
        prev = [(r, table.item(r, 0).data(Qt.ItemDataRole.UserRole + 2) or False)
                for r in selected_rows]
        self._undo_stack.append(("disable", self._tabs.currentIndex(), prev))

        gray = QColor(100, 100, 100)
        red = QColor(220, 60, 60)
        default_fg = table.palette().color(table.foregroundRole())
        for row in selected_rows:
            item0 = table.item(row, 0)
            row_id = item0.data(Qt.ItemDataRole.UserRole)
            start = item0.data(Qt.ItemDataRole.UserRole + 1)
            currently_disabled = item0.data(Qt.ItemDataRole.UserRole + 2) or False
            new_disabled = not currently_disabled
            item0.setData(Qt.ItemDataRole.UserRole + 2, new_disabled)
            if row_id is not None:
                self._db.toggle_scan_result_disabled(row_id, new_disabled)
            # Update visual
            if new_disabled:
                fg = gray
            elif start is not None and float(start) in self._neg_times:
                fg = red
            else:
                fg = default_fg
            for col in range(3):
                table.item(row, col).setForeground(fg)
        self.regions_edited.emit()

    def delete_selected(self) -> None:
        """Permanently delete selected rows from active tab and DB."""
        table = self._current_table()
        if table is None:
            return
        rows_to_delete = sorted(
            {idx.row() for idx in table.selectedIndexes()}, reverse=True)
        tab_idx = self._tabs.currentIndex()
        model = self._tabs.tabText(tab_idx).rsplit(" (", 1)[0]
        for row in rows_to_delete:
            row_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if row_id is not None:
                self._db.delete_scan_result(row_id)
            table.removeRow(row)
        count = table.rowCount()
        self._tabs.setTabText(tab_idx, f"{model} ({count})")
        self.tab_changed.emit()

    def filter_by_threshold(self, threshold: float) -> None:
        """Show/hide rows based on score threshold across all tabs."""
        for i in range(self._tabs.count()):
            table = self._tab_table(i)
            if table is None:
                continue
            visible = 0
            for row in range(table.rowCount()):
                score = float(table.item(row, 2).text())
                hide = score < threshold
                table.setRowHidden(row, hide)
                if not hide:
                    visible += 1
            model = self._tabs.tabText(i).rsplit(" (", 1)[0]
            self._tabs.setTabText(i, f"{model} ({visible})")
        self.regions_edited.emit()

    def _get_tab_regions(self, table: QTableWidget,
                         include_disabled: bool = False
                         ) -> list[tuple[float, float, float]]:
        """Extract (start, end, score) from a table widget, skipping disabled/hidden rows."""
        regions = []
        for row in range(table.rowCount()):
            if table.isRowHidden(row):
                continue
            if not include_disabled:
                disabled = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2)
                if disabled:
                    continue
            start = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
            end = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            score = float(table.item(row, 2).text())
            regions.append((float(start), float(end), score))
        return regions

    def current_regions_with_orig(self) -> list[tuple[float, float, float, float, float]]:
        """Return (start, end, score, orig_start, orig_end) for enabled, visible rows."""
        table = self._current_table()
        if table is None:
            return []
        regions = []
        for row in range(table.rowCount()):
            if table.isRowHidden(row):
                continue
            item0 = table.item(row, 0)
            disabled = item0.data(Qt.ItemDataRole.UserRole + 2)
            if disabled:
                continue
            start = item0.data(Qt.ItemDataRole.UserRole + 1)
            end = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            score = float(table.item(row, 2).text())
            os_ = item0.data(Qt.ItemDataRole.UserRole + 3)
            oe = item0.data(Qt.ItemDataRole.UserRole + 4)
            if os_ is None:
                os_ = start
            if oe is None:
                oe = end
            regions.append((float(start), float(end), score, float(os_), float(oe)))
        return regions

    def update_region_times(self, start_match: float, end_match: float,
                            new_start: float, new_end: float) -> None:
        """Update the table row matching (start, end) with new times. Called from timeline drag."""
        table = self._current_table()
        if table is None:
            return
        for row in range(table.rowCount()):
            item0 = table.item(row, 0)
            s = item0.data(Qt.ItemDataRole.UserRole + 1)
            e = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            if s is None or e is None:
                continue
            if abs(float(s) - start_match) < 0.01 and abs(float(e) - end_match) < 0.01:
                # Record undo
                tab_idx = self._tabs.currentIndex()
                self._undo_stack.append(("drag", tab_idx, row, float(s), float(e)))
                # Update stored values
                self._editing = True
                item0.setData(Qt.ItemDataRole.UserRole + 1, new_start)
                item0.setText(format_time(new_start))
                table.item(row, 1).setData(Qt.ItemDataRole.UserRole, new_end)
                table.item(row, 1).setText(format_time(new_end))
                self._editing = False
                # Persist to DB
                row_id = item0.data(Qt.ItemDataRole.UserRole)
                if row_id is not None:
                    self._db.update_scan_result_times(row_id, new_start, new_end)
                return

    def _on_add_negatives(self) -> None:
        """Toggle selected rows as hard negatives (red = negative, toggle off to remove)."""
        table = self._current_table()
        if table is None:
            return
        selected_rows = sorted({idx.row() for idx in table.selectedIndexes()})
        if not selected_rows:
            return
        # Record undo: which times were in neg before
        prev_neg = [(r, table.item(r, 0).data(Qt.ItemDataRole.UserRole + 1))
                     for r in selected_rows]
        was_neg = [(r, t, float(t) in self._neg_times) for r, t in prev_neg if t is not None]
        self._undo_stack.append(("neg", self._tabs.currentIndex(), was_neg))

        add_times: list[float] = []
        remove_times: list[float] = []
        red = QColor(220, 60, 60)
        gray = QColor(100, 100, 100)
        default_fg = table.palette().color(table.foregroundRole())
        for row in selected_rows:
            item0 = table.item(row, 0)
            start = item0.data(Qt.ItemDataRole.UserRole + 1)
            disabled = item0.data(Qt.ItemDataRole.UserRole + 2) or False
            if start is None:
                continue
            t = float(start)
            if t in self._neg_times:
                remove_times.append(t)
                self._neg_times.discard(t)
                fg = gray if disabled else default_fg
            else:
                add_times.append(t)
                self._neg_times.add(t)
                fg = gray if disabled else red
            for col in range(3):
                table.item(row, col).setForeground(fg)
        if add_times:
            self.negatives_requested.emit(add_times)
        if remove_times:
            self.negatives_removed.emit(remove_times)

    def _on_export(self) -> None:
        table = self._current_table()
        if table is None:
            return
        # _get_tab_regions already skips disabled; also skip negatives
        regions = [r for r in self._get_tab_regions(table) if r[0] not in self._neg_times]
        if regions:
            self.export_requested.emit(regions)

    def current_regions(self) -> list[tuple[float, float, float]]:
        """Return (start, end, score) for enabled rows in the active tab."""
        table = self._current_table()
        if table is None:
            return []
        return self._get_tab_regions(table)

    def all_regions(self) -> list[tuple[float, float, float]]:
        """Return (start, end, score) for ALL rows including disabled."""
        table = self._current_table()
        if table is None:
            return []
        return self._get_tab_regions(table, include_disabled=True)

    def highlight_time(self, t: float) -> None:
        """Select the row containing time t, scrolling to it."""
        table = self._current_table()
        if table is None:
            return
        for row in range(table.rowCount()):
            start = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
            end = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            if start is not None and end is not None and start <= t <= end:
                if table.currentRow() != row:
                    table.blockSignals(True)
                    table.selectRow(row)
                    table.scrollToItem(table.item(row, 0))
                    table.blockSignals(False)
                return

    def set_export_count(self, n: int) -> None:
        """Update the export button label with estimated clip count."""
        if n > 0:
            self._btn_export.setText(f"Export Scan Results ({n})")
        else:
            self._btn_export.setText("Export Scan Results")

    def has_results(self) -> bool:
        return self._tabs.count() > 0

    def undo(self) -> None:
        """Pop the last action from the undo stack and revert it."""
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()
        kind = action[0]
        if kind == "disable":
            _, tab_idx, prev = action
            table = self._tab_table(tab_idx)
            if table is None:
                return
            gray = QColor(100, 100, 100)
            red = QColor(220, 60, 60)
            default_fg = table.palette().color(table.foregroundRole())
            for row, was_disabled in prev:
                if row >= table.rowCount():
                    continue
                item0 = table.item(row, 0)
                item0.setData(Qt.ItemDataRole.UserRole + 2, was_disabled)
                row_id = item0.data(Qt.ItemDataRole.UserRole)
                if row_id is not None:
                    self._db.toggle_scan_result_disabled(row_id, was_disabled)
                start = item0.data(Qt.ItemDataRole.UserRole + 1)
                if was_disabled:
                    fg = gray
                elif start is not None and float(start) in self._neg_times:
                    fg = red
                else:
                    fg = default_fg
                for col in range(3):
                    table.item(row, col).setForeground(fg)
            self.regions_edited.emit()

        elif kind == "resize":
            _, tab_idx, row, col, old_val = action
            table = self._tab_table(tab_idx)
            if table is None or row >= table.rowCount():
                return
            self._editing = True
            if col == 0:
                table.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, old_val)
                table.item(row, 0).setText(format_time(old_val))
            else:
                table.item(row, 1).setData(Qt.ItemDataRole.UserRole, old_val)
                table.item(row, 1).setText(format_time(old_val))
            self._editing = False
            row_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            start = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
            end = table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            if row_id is not None:
                self._db.update_scan_result_times(row_id, float(start), float(end))
            self.regions_edited.emit()

        elif kind == "drag":
            _, tab_idx, row, old_start, old_end = action
            table = self._tab_table(tab_idx)
            if table is None or row >= table.rowCount():
                return
            self._editing = True
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, old_start)
            table.item(row, 0).setText(format_time(old_start))
            table.item(row, 1).setData(Qt.ItemDataRole.UserRole, old_end)
            table.item(row, 1).setText(format_time(old_end))
            self._editing = False
            row_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if row_id is not None:
                self._db.update_scan_result_times(row_id, old_start, old_end)
            self.regions_edited.emit()

        elif kind == "neg":
            _, tab_idx, was_neg = action
            table = self._tab_table(tab_idx)
            if table is None:
                return
            add_back: list[float] = []
            remove_back: list[float] = []
            gray = QColor(100, 100, 100)
            red = QColor(220, 60, 60)
            default_fg = table.palette().color(table.foregroundRole())
            for row, t_val, was_in_neg in was_neg:
                if row >= table.rowCount():
                    continue
                t = float(t_val)
                disabled = table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2) or False
                if was_in_neg and t not in self._neg_times:
                    self._neg_times.add(t)
                    add_back.append(t)
                    fg = gray if disabled else red
                elif not was_in_neg and t in self._neg_times:
                    self._neg_times.discard(t)
                    remove_back.append(t)
                    fg = gray if disabled else default_fg
                else:
                    continue
                for col in range(3):
                    table.item(row, col).setForeground(fg)
            if add_back:
                self.negatives_requested.emit(add_back)
            if remove_back:
                self.negatives_removed.emit(remove_back)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.undo()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.toggle_disable_selected()
        elif event.key() == Qt.Key.Key_N:
            self._on_add_negatives()
        else:
            super().keyPressEvent(event)


class WaveformWorker(QThread):
    """Extract a low-res waveform envelope in the background."""
    done = pyqtSignal(object)  # emits numpy array of peak values

    def __init__(self, video_path: str, n_bins: int = 2000):
        super().__init__()
        self._path = video_path
        self._n_bins = n_bins

    def run(self):
        import numpy as np
        try:
            cmd = [
                _bin("ffmpeg"), "-i", self._path,
                "-vn", "-ac", "1", "-ar", "8000",
                "-f", "f32le", "-loglevel", "error", "pipe:1",
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
            if proc.returncode != 0:
                return
            samples = np.frombuffer(proc.stdout, dtype=np.float32)
            if len(samples) == 0:
                return
            # Downsample to n_bins peak values
            bin_size = max(1, len(samples) // self._n_bins)
            n = (len(samples) // bin_size) * bin_size
            peaks = np.abs(samples[:n].reshape(-1, bin_size)).max(axis=1)
            # Normalize to 0-1
            mx = peaks.max()
            if mx > 0:
                peaks = peaks / mx
            self.done.emit(peaks)
        except Exception:
            pass


class TimelineWidget(QWidget):
    cursor_changed = pyqtSignal(float)              # emits position in seconds
    seek_changed = pyqtSignal(float)                # emits seek position (lock mode)
    marker_delete_requested = pyqtSignal(str)       # emits output_path
    markers_clear_requested = pyqtSignal()          # clear all markers
    keyframe_delete_requested = pyqtSignal(float)   # emits keyframe time
    marker_clicked = pyqtSignal(float, str)         # emits (start_time, output_path)
    marker_deselected = pyqtSignal()                # double-click on empty space
    # (index, new_start, new_end, old_start, old_end)
    scan_region_resized = pyqtSignal(int, float, float, float, float)

    _RULER_H = 22   # pixels reserved for the time ruler
    _HANDLE_H = 8   # height of the playhead triangle
    _EDGE_PX = 3    # pixel tolerance for edge hit detection

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(80)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._cursor = 0.0
        self._clip_span = 14.0  # 8 + 2*spread, updated from MainWindow
        self._scan_mode = False
        self._play_pos: float | None = None  # current playback position (seconds)
        self._locked = False                 # when True, clicks scrub playback, not cursor
        self._crop_keyframes: list[tuple[float, float, str | None, bool, bool]] = []
        self._markers: list[tuple[float, int, str]] = []
        self._hover_cache: list[tuple[float, str]] = []  # (t/duration, path)
        # (start, end, score, orig_start, orig_end)
        self._scan_regions: list[tuple[float, float, float, float, float]] = []
        self._scan_neg_times: set[float] = set()

        # Waveform data (numpy array of 0-1 peak values, or None)
        self._waveform = None

        # Edge-drag state for scan regions
        self._drag_idx: int | None = None       # which region
        self._drag_edge: str | None = None      # "left" or "right"
        self._drag_start_val: float = 0.0       # value before drag
        self._drag_end_val: float = 0.0

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

    def set_waveform(self, peaks) -> None:
        self._waveform = peaks
        self.update()

    def set_clip_span(self, span: float):
        self._clip_span = span
        self.update()

    def set_cursor(self, seconds: float):
        if self._scan_mode:
            clamped = max(0.0, min(seconds, self._duration))
        else:
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

    def set_scan_regions(self, regions: list, neg_times: set[float] | None = None) -> None:
        """regions: list of (start, end, score) or (start, end, score, orig_start, orig_end)"""
        normed: list[tuple[float, float, float, float, float]] = []
        for r in regions:
            if len(r) >= 5:
                normed.append((r[0], r[1], r[2], r[3], r[4]))
            else:
                normed.append((r[0], r[1], r[2], r[0], r[1]))
        self._scan_regions = normed
        self._scan_neg_times = neg_times or set()
        self._drag_idx = None
        self.update()

    def clear_scan_regions(self) -> None:
        self._scan_regions = []
        self._drag_idx = None
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

    def _hit_scan_edge(self, x: float) -> tuple[int, str] | None:
        """Return (region_index, 'left'|'right') if x is near a scan region edge."""
        if not self._scan_regions or self._duration <= 0:
            return None
        w = self.width()
        for i, (start, end, score, os_, oe) in enumerate(self._scan_regions):
            x1 = start / self._duration * w
            x2 = end / self._duration * w
            if abs(x - x1) <= self._EDGE_PX:
                return (i, "left")
            if abs(x - x2) <= self._EDGE_PX:
                return (i, "right")
        return None

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

            # ── waveform ──────────────────────────────────────────────────
            if self._waveform is not None and len(self._waveform) > 0:
                n = len(self._waveform)
                mid_y = rh + th // 2
                half_h = th * 0.4  # waveform uses 80% of track height
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(80, 180, 80, 50))
                from PyQt6.QtGui import QPolygonF
                from PyQt6.QtCore import QPointF
                pts = []
                # Top half (positive peaks)
                for i in range(n):
                    x = i * w / n
                    y = mid_y - self._waveform[i] * half_h
                    pts.append(QPointF(x, y))
                # Bottom half (mirror)
                for i in range(n - 1, -1, -1):
                    x = i * w / n
                    y = mid_y + self._waveform[i] * half_h
                    pts.append(QPointF(x, y))
                p.drawPolygon(QPolygonF(pts))

            # ── selection region (full clip span) ─────────────────────────
            x_start = int(self._cursor / self._duration * w)
            if not self._scan_mode:
                x_end   = int(min(self._cursor + self._clip_span, self._duration) / self._duration * w)
                sel_w   = max(x_end - x_start, 1)
                p.fillRect(x_start, rh, sel_w, th, QColor(60, 130, 220, 90))

            # ── playback progress fill ────────────────────────────────────
            if not self._scan_mode and self._play_pos is not None and self._play_pos > self._cursor:
                prog_end = min(self._play_pos, self._cursor + self._clip_span, self._duration)
                x_prog = int(prog_end / self._duration * w)
                prog_w = max(x_prog - x_start, 0)
                if prog_w > 0:
                    p.fillRect(x_start, rh, prog_w, th, QColor(100, 200, 255, 60))

            # left/right edges of selection
            if not self._scan_mode:
                p.setPen(QPen(QColor(60, 130, 220, 180), 1))
                p.drawLine(x_start, rh, x_start, h)
                p.drawLine(x_end,   rh, x_end,   h)

            # ── scan regions ──────────────────────────────────────────────
            if self._scan_regions and self._duration > 0:
                for (start, end, score, os_, oe) in self._scan_regions:
                    x1 = int(start / self._duration * w)
                    x2 = int(end / self._duration * w)
                    alpha = int(40 + score * 80)  # 40–120 opacity
                    # Grey ghost for trimmed portions
                    ox1 = int(os_ / self._duration * w)
                    ox2 = int(oe / self._duration * w)
                    if ox1 < x1:
                        p.fillRect(ox1, rh, x1 - ox1, h - rh, QColor(120, 120, 120, 40))
                    if ox2 > x2:
                        p.fillRect(x2, rh, ox2 - x2, h - rh, QColor(120, 120, 120, 40))
                    # Active region
                    if start in self._scan_neg_times:
                        p.fillRect(x1, rh, x2 - x1, h - rh, QColor(220, 60, 60, alpha))
                    else:
                        p.fillRect(x1, rh, x2 - x1, h - rh, QColor(100, 200, 255, alpha))
                    # Edge handles (thin lines at edges)
                    p.setPen(QPen(QColor(255, 255, 255, 140), 1))
                    p.drawLine(x1, rh, x1, h)
                    p.drawLine(x2, rh, x2, h)

            # ── export markers ────────────────────────────────────────────
            if not self._scan_mode:
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

            # ── scan mode cursor + playback line ─────────────────────────
            if self._scan_mode:
                # Export cursor (dim)
                p.setPen(QPen(QColor(255, 255, 255, 80), 1))
                p.drawLine(x_start, rh, x_start, h)
                # Playback position (bright green)
                if self._play_pos is not None and self._play_pos >= 0:
                    px = int(self._play_pos / self._duration * w)
                    p.setPen(QPen(QColor(80, 255, 80, 220), 2))
                    p.drawLine(px, rh, px, h)

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
        x = event.position().x()
        # Check for scan region edge drag — require Shift to avoid accidental resizes
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            hit = self._hit_scan_edge(x)
            if hit is not None:
                idx, edge = hit
                r = self._scan_regions[idx]
                self._drag_idx = idx
                self._drag_edge = edge
                self._drag_start_val = r[0]
                self._drag_end_val = r[1]
                return
        self._seek(x)

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

        # Active edge drag
        if self._drag_idx is not None and event.buttons():
            t = self._pos_to_time(int(x))
            r = self._scan_regions[self._drag_idx]
            start, end, score, os_, oe = r
            if self._drag_edge == "left":
                new_start = max(0.0, min(t, end - 0.5))
                self._scan_regions[self._drag_idx] = (new_start, end, score, os_, oe)
            else:
                new_end = max(start + 0.5, min(t, self._duration))
                self._scan_regions[self._drag_idx] = (start, new_end, score, os_, oe)
            self.update()
            return

        # Hover cursor: resize arrow near edges (only with Shift held)
        mods = event.modifiers()
        if (mods & Qt.KeyboardModifier.ShiftModifier) and self._hit_scan_edge(x):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()

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
        if self._drag_idx is not None:
            # Emit resize signal with old and new bounds
            idx = self._drag_idx
            r = self._scan_regions[idx]
            self.scan_region_resized.emit(
                idx, r[0], r[1], self._drag_start_val, self._drag_end_val)
            self._drag_idx = None
            self._drag_edge = None
            return
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
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        act_kf = None
        act_marker = None
        act_clear = None
        if hit_kf_time is not None:
            act_kf = menu.addAction(f"Delete keyframe @ {format_time(hit_kf_time)}")
        if hit_path is not None:
            act_marker = menu.addAction(f"Delete marker: {os.path.basename(hit_path)}")
        if self._markers:
            if hit_kf_time is not None or hit_path is not None:
                menu.addSeparator()
            act_clear = menu.addAction(f"Clear all markers ({len(self._markers)})")
        if menu.isEmpty():
            return
        chosen = menu.exec(event.globalPos())
        if chosen and chosen == act_kf:
            self.keyframe_delete_requested.emit(hit_kf_time)
        elif chosen and chosen == act_marker:
            self.marker_delete_requested.emit(hit_path)
        elif chosen and chosen == act_clear:
            self.markers_clear_requested.emit()

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


def _log_env():
    """Log Python environment info at startup."""
    _log(f"Python {sys.version}")
    _log(f"venv: {sys.prefix}")
    try:
        import torch
        cuda = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "not available"
        _log(f"PyTorch {torch.__version__} — CUDA {torch.version.cuda or 'n/a'} — GPU: {cuda}")
    except ImportError:
        _log("PyTorch: not installed")
    try:
        import sklearn
        _log(f"scikit-learn {sklearn.__version__}")
    except ImportError:
        _log("scikit-learn: not installed (training will fail)")
    try:
        import librosa
        _log(f"librosa {librosa.__version__}")
    except ImportError:
        _log("librosa: not installed")


def main():
    _log_env()
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
        self._timeline.markers_clear_requested.connect(self._on_clear_markers)
        self._timeline.keyframe_delete_requested.connect(self._on_delete_keyframe)
        self._mpv.time_pos_changed.connect(self._timeline.set_play_position)
        self._mpv.time_pos_changed.connect(self._on_playback_pos_changed)
        self._timeline.marker_clicked.connect(self._on_marker_clicked)
        self._timeline.marker_deselected.connect(self._on_marker_deselected)
        self._timeline.scan_region_resized.connect(self._on_scan_region_resized)

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
        self._spn_spread.valueChanged.connect(lambda: self._update_scan_export_count())

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
        self._btn_scan_mode = QPushButton("Review")
        self._btn_scan_mode.setCheckable(True)
        self._btn_scan_mode.setToolTip("Scan review mode: hide spread/markers, free cursor movement")
        self._btn_scan_mode.toggled.connect(self._toggle_scan_mode)

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

        self._btn_scan_all = QPushButton("Scan All")
        self._btn_scan_all.setToolTip("Scan all playlist videos that haven't been scanned yet")
        self._btn_scan_all.clicked.connect(self._start_scan_all)
        self._scan_all_queue: list[str] = []

        self._cmb_scan_model = QComboBox()
        self._cmb_scan_model.setToolTip("Trained embedding model to use for scanning")
        self._cmb_scan_model.setMinimumWidth(120)
        self._cmb_scan_model.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._cmb_scan_model.customContextMenuRequested.connect(self._show_model_versions_menu)
        self._btn_model_history = QPushButton("\u23f2")
        self._btn_model_history.setFixedWidth(28)
        self._btn_model_history.setToolTip("Rollback to a previous model version")
        self._btn_model_history.clicked.connect(
            lambda: self._show_model_versions_menu(None)
        )

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
        self._spn_auto_fuse.valueChanged.connect(self._on_fuse_changed)

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
        self._refresh_scan_models()

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
        settings_row.addWidget(self._cmb_scan_model)
        settings_row.addWidget(self._btn_model_history)
        settings_row.addWidget(self._btn_scan)
        settings_row.addWidget(self._btn_scan_mode)
        settings_row.addWidget(self._btn_auto_export)
        settings_row.addWidget(self._spn_auto_fuse)
        settings_row.addWidget(self._sld_threshold)
        settings_row.addWidget(self._btn_train)
        settings_row.addWidget(self._btn_scan_all)
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

        # Scan results panel (right side)
        self._scan_panel = ScanResultsPanel(self._db)
        self._scan_panel.seek_requested.connect(self._on_scan_seek)
        self._scan_panel.export_requested.connect(self._on_scan_export)
        self._scan_panel.negatives_requested.connect(self._on_scan_negatives)
        self._scan_panel.negatives_removed.connect(self._on_scan_negatives_removed)
        self._scan_panel.tab_changed.connect(self._on_scan_regions_edited)
        self._scan_panel.regions_edited.connect(self._on_scan_regions_edited)
        self._sld_threshold.valueChanged.connect(self._on_threshold_changed)

        # Root: horizontal splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.addWidget(self._scan_panel)
        splitter.setSizes([200, 900, 200])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, True)

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

        self._show_changelog()

    # ── Changelog ────────────────────────────────────────────

    APP_VERSION = "1.0"
    CHANGELOG: list[tuple[str, list[str]]] = [
        ("1.0", [
            "<b>New export layout</b> — clips are now stored in per-video "
            "<code>vid_NNN/</code> folders instead of per-clip "
            "<code>clip_NNN/</code> group dirs. "
            "Each source video gets its own folder with flat clip files inside "
            "(e.g. <code>mp4/vid_001/clip_001_0.mp4</code>). "
            "Old databases are migrated automatically on startup: "
            "DB paths are rewritten and files are moved to the new layout.",
            "<b>Counter is now per-video</b> — clip numbering restarts in each "
            "vid folder, and the DB is cross-checked to prevent overwrites "
            "even if the export folder is temporarily empty.",
            "<b>Audio detection models</b> — three new embedding models for "
            "audio scanning: <b>AST</b> (Audio Spectrogram Transformer), "
            "<b>EAT</b> (Efficient Audio Transformer), and <b>multi-layer "
            "HuBERT/Wav2Vec2</b> extraction. Classifier probabilities are now "
            "calibrated with isotonic regression for more meaningful scores.",
            "<b>Scan result history</b> — scan results are versioned per "
            "(file, model); switch between past scan versions from a dropdown.",
            "<b>Hard negatives</b> — management dialog to review, filter, and "
            "bulk-delete hard negatives; source model is tracked per negative.",
            "<b>Scan workflow</b> — disable/resize scan regions, undo edits, "
            "interruptible Scan All with resume, audio prefetch, review mode.",
            "<b>Dataset statistics</b> — dialog showing per-video clip breakdown "
            "and class balance.",
            "<b>Waveform overlay</b> on timeline.",
        ]),
    ]

    def _show_changelog(self) -> None:
        last = self._settings.value("last_seen_version", "")
        if last == self.APP_VERSION:
            return
        # Collect entries newer than last seen
        lines: list[str] = []
        for ver, items in self.CHANGELOG:
            if ver == last:
                break
            lines.append(f"<h3>v{ver}</h3><ul>")
            for item in items:
                lines.append(f"<li>{item}</li>")
            lines.append("</ul>")
        if not lines:
            self._settings.setValue("last_seen_version", self.APP_VERSION)
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("What's new")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText("".join(lines))
        cb = QCheckBox("Don't show again for this version")
        msg.setCheckBox(cb)
        msg.exec()
        if cb.isChecked():
            self._settings.setValue("last_seen_version", self.APP_VERSION)

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
            "<tr><td><b>Delete / Backspace</b></td><td>Toggle disable on selected scan regions</td></tr>"
            "<tr><td><b>N</b></td><td>Toggle hard negative on selected scan regions</td></tr>"
            "<tr><td><b>Ctrl+Z</b></td><td>Undo last scan panel action</td></tr>"
            "<tr><td><b>? / F1</b></td><td>This help</td></tr>"
            "<tr><td colspan='2'><hr></td></tr>"
            "<tr><td><b>Double-click marker</b></td><td>Enter overwrite mode (locked: jump to end of clip span)</td></tr>"
            "<tr><td><b>Right-click marker</b></td><td>Delete clip group</td></tr>"
            "<tr><td><b>Click video / crop bar</b></td><td>Reposition portrait crop</td></tr>"
            "<tr><td><b>Shift+drag scan region edge</b></td><td>Resize scan region</td></tr>"
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
        self._refresh_scan_models()
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
        # Don't interrupt Scan All when switching files — only cancel solo scans
        if not self._scan_all_queue and not getattr(self, '_scan_all_stopping', False):
            if self._scan_worker and self._scan_worker.isRunning():
                self._scan_worker.cancel()
            self._cleanup_scan_worker()
            self._btn_scan.setEnabled(True)
            self._btn_scan_all.setText("Scan All")
            self._btn_scan_all.setEnabled(True)
        # Load saved scan results for this file
        if self._file_path:
            filename = os.path.basename(self._file_path)
            self._scan_panel.load_for_file(filename, self._profile)
            self._timeline.set_scan_regions(
                self._scan_panel.current_regions_with_orig(),
                neg_times=self._scan_panel._neg_times,
            )
            self._update_scan_export_count()

        # Start waveform extraction in background
        self._timeline.set_waveform(None)
        if hasattr(self, '_waveform_worker') and self._waveform_worker is not None:
            self._safe_disconnect(self._waveform_worker.done)
            self._waveform_worker.quit()
            self._waveform_worker.wait(1000)
        self._waveform_worker = WaveformWorker(self._file_path)
        self._waveform_worker.done.connect(self._timeline.set_waveform)
        self._waveform_worker.start()

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

        # Recalculate vid folder & counter for the new video.
        self._update_next_label()

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
            n = self._db.get_clip_count(os.path.basename(path), profile)
            if n:
                self._playlist.mark_done(path, n)
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

    def _on_clear_markers(self) -> None:
        """Delete all markers for the current file."""
        if not self._file_path:
            return
        filename = os.path.basename(self._file_path)
        markers = self._db.get_markers(filename, self._profile)
        for _, _, output_path in markers:
            self._db.delete_by_output_path(output_path)
        self._refresh_markers()
        self._refresh_playlist_checks()
        self._update_next_label()
        self._show_status(f"Cleared {len(markers)} marker(s)", 4000)

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
            stem = os.path.splitext(os.path.basename(output_path))[0]
            group_label = stem.rsplit("_", 1)[0]
            self._show_status(f"Cursor → end of {group_label}", 3000)
            return
        self._overwrite_path = output_path
        self._overwrite_group = self._db.get_group(output_path)
        n = len(self._overwrite_group)
        stem = os.path.splitext(os.path.basename(output_path))[0]
        group_label = stem.rsplit("_", 1)[0]
        if n > 1:
            self._lbl_next.setText(f"↺ {group_label} ({n} clips)")
            self._btn_delete.setText(f"Delete {group_label} ({n})")
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
            f"Overwrite mode: {group_label} ({n} clip{'s' if n != 1 else ''}) — export to replace", 5000
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
        stem = os.path.splitext(os.path.basename(all_paths[0]))[0]
        group_label = stem.rsplit("_", 1)[0]
        if n > 1:
            msg = f"Delete {n} clips in {group_label} from disk and database?"
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
        self._show_status(f"Deleted {n} clip{'s' if n != 1 else ''}: {group_label}")

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
        if self._timeline._scan_mode:
            self._scan_panel.highlight_time(t)
            self._mpv.seek(t)
        elif self._mpv.is_playing():
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

    def _load_selected_scan_model(self) -> tuple:
        """Load the classifier selected in the scan model combo.

        Returns (model_dict, label_str) or (None, "") on failure.
        """
        from core.audio_scan import load_classifier, default_model_path
        sel = self._cmb_scan_model.currentText()
        if not sel or sel == "(no model)":
            self._show_status("No trained model — click Train first")
            return None, ""
        embed_name = None if sel == "(legacy)" else sel
        model_path = default_model_path(self._profile, embed_name)
        model = load_classifier(model_path)
        if model is None:
            self._show_status(f"Model file missing: {model_path}")
            return None, ""
        return model, sel

    def _refresh_scan_models(self) -> None:
        """Populate the scan model combo with trained models for the current profile."""
        from core.audio_scan import list_trained_models
        prev = self._cmb_scan_model.currentText()
        self._cmb_scan_model.clear()
        models = list_trained_models(self._profile)
        if not models:
            self._cmb_scan_model.addItem("(no model)")
        else:
            for m in models:
                self._cmb_scan_model.addItem(m if m else "(legacy)")
        # Restore previous selection if still available
        idx = self._cmb_scan_model.findText(prev)
        if idx >= 0:
            self._cmb_scan_model.setCurrentIndex(idx)

    def _show_model_versions_menu(self, pos) -> None:
        """Show context menu with model version history for rollback."""
        from core.audio_scan import list_model_versions, restore_model_version
        sel = self._cmb_scan_model.currentText()
        if not sel or sel == "(no model)":
            return
        embed_name = None if sel == "(legacy)" else sel
        versions = list_model_versions(self._profile, embed_name)
        if len(versions) <= 1:
            self._show_status("No previous versions available")
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        for label, path in versions:
            if label == "current":
                act = menu.addAction(f"current (active)")
                act.setEnabled(False)
            else:
                # Format timestamp for display: 20260418_170800 → 2026-04-18 17:08
                display = f"{label[:4]}-{label[4:6]}-{label[6:8]} {label[9:11]}:{label[11:13]}"
                act = menu.addAction(f"Restore {display}")
                act.setData(path)
        global_pos = (self._btn_model_history.mapToGlobal(self._btn_model_history.rect().bottomLeft())
                      if pos is None
                      else self._cmb_scan_model.mapToGlobal(pos))
        chosen = menu.exec(global_pos)
        if chosen and chosen.data():
            restore_model_version(chosen.data(), self._profile, embed_name)
            self._start_scan()

    @staticmethod
    def _safe_disconnect(*signals) -> None:
        for sig in signals:
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def _cleanup_scan_worker(self) -> None:
        """Disconnect signals, cancel, and schedule deletion of old scan worker."""
        if self._scan_worker is not None:
            self._safe_disconnect(
                self._scan_worker.scan_done,
                self._scan_worker.error,
                self._scan_worker.progress,
            )
            self._scan_worker.cancel()
            if self._scan_worker.isRunning():
                self._scan_worker.finished.connect(self._scan_worker.deleteLater)
            else:
                self._scan_worker.deleteLater()
            self._scan_worker = None

    def _on_fuse_changed(self) -> None:
        """Re-fuse displayed scan regions and update export count."""
        self._update_scan_export_count()
        # Re-fuse the timeline regions using the new fuse gap
        all_regions = self._scan_panel.current_regions_with_orig()
        if all_regions:
            fuse_gap = self._spn_auto_fuse.value()
            sorted_r = sorted(all_regions, key=lambda r: r[0])
            fused: list[tuple[float, float, float, float, float]] = []
            s, e, sc, os_, oe = sorted_r[0]
            for s2, e2, sc2, os2, oe2 in sorted_r[1:]:
                if s2 - e <= fuse_gap:
                    e = max(e, e2)
                    sc = max(sc, sc2)
                    os_ = min(os_, os2)
                    oe = max(oe, oe2)
                else:
                    fused.append((s, e, sc, os_, oe))
                    s, e, sc, os_, oe = s2, e2, sc2, os2, oe2
            fused.append((s, e, sc, os_, oe))
            self._timeline.set_scan_regions(
                fused, neg_times=self._scan_panel._neg_times)
        else:
            self._timeline.set_scan_regions([])

    def _on_playback_pos_changed(self, t: float) -> None:
        """In review mode, highlight the scan result matching the playback position."""
        if self._timeline._scan_mode:
            self._scan_panel.highlight_time(t)

    def _toggle_scan_mode(self, on: bool) -> None:
        """Toggle scan review mode — clean timeline, free cursor."""
        self._timeline._scan_mode = on
        self._timeline.update()

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

        model, model_label = self._load_selected_scan_model()
        if model is None:
            return

        self._btn_scan.setEnabled(False)
        self._scan_file_path = self._file_path
        self._scan_model_label = model_label
        self._show_status(f"Scanning ({model_label})...")
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
        model_label = getattr(self, '_scan_model_label', '')
        if model_label and self._file_path:
            filename = os.path.basename(self._file_path)
            self._scan_panel.add_scan_results(model_label, regions)
        self._update_scan_export_count()
        self._show_status(f"Scan complete: {len(regions)} matching regions")

    def _on_scan_error(self, msg: str) -> None:
        self._btn_scan.setEnabled(True)
        self._btn_auto_export.setEnabled(True)
        self._show_status(f"Scan error: {msg}")

    def _on_scan_seek(self, t: float) -> None:
        """Seek player when a scan result row is clicked."""
        if self._file_path:
            self._cursor = t
            self._mpv.seek(t)
            self._timeline.set_cursor(t)
            dur = self._mpv.get_duration()
            self._lbl_time.setText(f"{format_time(t)} / {format_time(dur)}")

    def _update_scan_export_count(self) -> None:
        """Recalculate and display estimated clip count on the export button."""
        neg = self._scan_panel._neg_times
        regions = [r for r in self._scan_panel.current_regions() if r[0] not in neg]
        if not regions:
            self._scan_panel.set_export_count(0)
            return
        groups = self._build_export_spans(
            regions, fuse_gap=self._spn_auto_fuse.value(),
            spread=self._spn_spread.value(),
        )
        n = sum(len(g) for g in groups)
        self._scan_panel.set_export_count(n)

    def _on_scan_export(self, regions: list) -> None:
        """Export clips from scan results panel."""
        if not self._file_path or not regions:
            return
        if self._export_worker and self._export_worker.isRunning():
            self._show_status("Export already running…")
            return
        self._auto_export_no_markers = True
        self._auto_export_regions(regions)

    def _on_scan_negatives(self, times: list) -> None:
        """Save selected scan result timestamps as hard negatives for training."""
        if not self._file_path:
            return
        filename = os.path.basename(self._file_path)
        source_model = self._scan_panel.current_model_name()
        self._db.add_hard_negatives(filename, self._profile, times,
                                    source_path=self._file_path,
                                    source_model=source_model)
        self._timeline.set_scan_regions(
            self._scan_panel.current_regions_with_orig(),
            neg_times=self._scan_panel._neg_times,
        )
        self._update_scan_export_count()
        self._show_status(f"Added {len(times)} hard negative(s) for training")

    def _on_scan_negatives_removed(self, times: list) -> None:
        """Remove hard negatives that were toggled off."""
        if not self._file_path:
            return
        filename = os.path.basename(self._file_path)
        self._db.remove_hard_negatives(filename, self._profile, times)
        self._timeline.set_scan_regions(
            self._scan_panel.current_regions_with_orig(),
            neg_times=self._scan_panel._neg_times,
        )
        self._update_scan_export_count()
        self._show_status(f"Removed {len(times)} hard negative(s)")

    def _on_threshold_changed(self, value: float) -> None:
        """Filter existing scan results by threshold without rescanning."""
        self._scan_panel.filter_by_threshold(value)

    def _on_scan_regions_edited(self) -> None:
        """A scan region was disabled/enabled or resized — refresh timeline and count."""
        self._timeline.set_scan_regions(
            self._scan_panel.current_regions_with_orig(),
            neg_times=self._scan_panel._neg_times,
        )
        self._update_scan_export_count()

    def _on_scan_region_resized(self, idx: int, new_start: float, new_end: float,
                                old_start: float, old_end: float) -> None:
        """A scan region edge was dragged on the timeline — update panel + DB."""
        self._scan_panel.update_region_times(old_start, old_end, new_start, new_end)
        self._update_scan_export_count()

    # ── Scan All ───────────────────────────────────────────────

    def _start_scan_all(self) -> None:
        """Scan all playlist videos not yet scanned with the selected model."""
        # If already running, stop after current video finishes
        if self._scan_all_queue or getattr(self, '_scan_all_stopping', False):
            if self._scan_worker and self._scan_worker.isRunning():
                self._scan_all_stopping = True
                self._scan_all_queue.clear()
                self._btn_scan_all.setEnabled(False)
                self._show_status("Scan All: stopping after current video…")
                return
        if self._scan_worker and self._scan_worker.isRunning():
            self._show_status("Scan already running")
            return

        model, model_label = self._load_selected_scan_model()
        if model is None:
            return

        # Build queue: playlist files minus already-scanned and training files
        all_paths = self._playlist._paths
        scanned = self._db.get_scanned_filenames(self._profile, model_label)
        training = self._db.get_training_filenames(self._profile)
        skip = scanned | training

        self._scan_all_queue = [
            p for p in all_paths if os.path.basename(p) not in skip
        ]
        if not self._scan_all_queue:
            self._show_status("All videos already scanned or used for training")
            return

        self._scan_all_model = model
        self._scan_all_model_label = model_label
        self._scan_all_profile = self._profile
        self._scan_all_total = len(self._scan_all_queue)
        self._scan_all_stopping = False
        self._btn_scan_all.setText("Stop")
        self._btn_scan.setEnabled(False)
        self._show_status(
            f"Scan All: 0/{self._scan_all_total} ({model_label})")
        self._scan_all_next()

    def _scan_all_next(self) -> None:
        """Start scanning the next video in the queue."""
        if not self._scan_all_queue:
            self._btn_scan_all.setText("Scan All")
            self._btn_scan_all.setEnabled(True)
            self._btn_scan.setEnabled(True)
            if getattr(self, '_scan_all_stopping', False):
                done = self._scan_all_total - len(self._scan_all_queue)
                self._show_status(f"Scan All stopped — {done}/{self._scan_all_total} videos scanned")
            else:
                self._show_status(f"Scan All complete: {self._scan_all_total} videos scanned")
            self._scan_all_stopping = False
            self._scan_all_prefetched = {}
            return

        self._cleanup_scan_worker()
        path = self._scan_all_queue.pop(0)
        remaining = self._scan_all_total - len(self._scan_all_queue)
        self._scan_all_current_path = path
        self._show_status(
            f"Scan All: {remaining}/{self._scan_all_total} — "
            f"{os.path.basename(path)}")

        # Use prefetched audio if available
        prefetched = getattr(self, '_scan_all_prefetched', {}).pop(path, None)

        threshold = self._sld_threshold.value()
        self._scan_worker = ScanWorker(
            path, model=self._scan_all_model, threshold=threshold,
            prefetched_audio=prefetched,
        )
        self._scan_worker.scan_done.connect(self._on_scan_all_done)
        self._scan_worker.error.connect(self._on_scan_all_error)
        self._scan_worker.start()

        # Prefetch audio for the next video while GPU is busy
        self._prefetch_next()

    def _prefetch_next(self) -> None:
        """Prefetch audio for the next queued video in a background thread."""
        if not self._scan_all_queue:
            return
        next_path = self._scan_all_queue[0]
        if not hasattr(self, '_scan_all_prefetched'):
            self._scan_all_prefetched = {}
        if next_path in self._scan_all_prefetched:
            return
        embed_model = self._scan_all_model.get("embed_model")
        from concurrent.futures import ThreadPoolExecutor
        if not hasattr(self, '_prefetch_pool'):
            self._prefetch_pool = ThreadPoolExecutor(max_workers=1)
        def _do_prefetch(p, em):
            from core.audio_scan import prefetch_audio
            return p, prefetch_audio(p, embed_model=em)
        future = self._prefetch_pool.submit(_do_prefetch, next_path, embed_model)
        future.add_done_callback(self._on_prefetch_done)

    def _on_prefetch_done(self, future) -> None:
        """Store prefetched audio data (called from thread pool)."""
        try:
            path, audio = future.result()
            if audio is not None:
                if not hasattr(self, '_scan_all_prefetched'):
                    self._scan_all_prefetched = {}
                self._scan_all_prefetched[path] = audio
        except Exception as e:
            _log(f"Prefetch error: {e}")

    def _on_scan_all_done(self, regions: list) -> None:
        """Save batch scan results and continue to next video."""
        path = getattr(self, '_scan_all_current_path', '')
        model_label = getattr(self, '_scan_all_model_label', '')
        if path and model_label:
            filename = os.path.basename(path)
            profile = getattr(self, '_scan_all_profile', self._profile)
            self._db.save_scan_results(
                filename, profile, model_label, regions)
            done = self._scan_all_total - len(self._scan_all_queue)
            _log(f"Scan All: {done}/{self._scan_all_total} done — "
                 f"{filename}: {len(regions)} regions")
            # If this is the currently loaded file, update the panel
            if self._file_path and os.path.basename(self._file_path) == filename:
                self._scan_panel.load_for_file(filename, profile)
                self._timeline.set_scan_regions(regions)
        self._scan_all_next()

    def _on_scan_all_error(self, msg: str) -> None:
        """Log error and continue to next video."""
        path = getattr(self, '_scan_all_current_path', '')
        _log(f"Scan All error on {os.path.basename(path)}: {msg}")
        self._scan_all_next()

    # ── Training ────────────────────────────────────────────────

    def _cleanup_train_worker(self) -> None:
        """Disconnect signals and schedule deletion of old train worker."""
        if self._train_worker is not None:
            self._safe_disconnect(
                self._train_worker.train_done,
                self._train_worker.error,
                self._train_worker.progress,
            )
            if self._train_worker.isRunning():
                self._train_worker.cancel()
                self._train_worker.finished.connect(self._train_worker.deleteLater)
            else:
                self._train_worker.deleteLater()
            self._train_worker = None

    def _open_train_dialog(self):
        """Show the training config dialog and start training if accepted."""
        if self._train_worker and self._train_worker.isRunning():
            self._train_worker.cancel()
            self._btn_train.setText("Train")
            self._btn_train.setEnabled(False)
            self._show_status("Cancelling training…")
            self._train_worker.finished.connect(
                lambda: self._btn_train.setEnabled(True))
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
        neg_folder = dlg.negative_folder
        neg_margin = dlg.neg_margin
        embed_model = dlg.embed_model
        video_dir = dlg.video_dir
        inc_scan = dlg.include_scan_exports
        use_neg = dlg.use_hard_negatives
        if not pos_folder:
            self._show_status("No positive class selected")
            return

        # Persist video dir for next time
        if video_dir:
            self._settings.setValue("train_video_dir", video_dir)

        video_infos = self._db.get_training_data(
            self._profile, pos_folder, negative_folder=neg_folder,
            fallback_video_dir=video_dir,
            include_scan_exports=inc_scan,
            use_hard_negatives=use_neg,
        )
        if not video_infos:
            self._show_status("No training data found for this subprofile")
            return

        from core.audio_scan import default_model_path
        model_path = default_model_path(self._profile, embed_model)

        self._cleanup_train_worker()
        self._btn_train.setText("Cancel")
        self._show_status(f"Training {embed_model} on {len(video_infos)} videos...")

        n_workers = self._spn_workers.value()
        self._train_worker = TrainWorker(video_infos, model_path, embed_model, n_workers, neg_margin)
        self._train_worker.train_done.connect(self._on_train_done)
        self._train_worker.error.connect(self._on_train_error)
        self._train_worker.progress.connect(self._show_status)
        self._train_worker.start()

    def _on_train_done(self, model_path: str):
        self._btn_train.setText("Train")
        self._btn_train.setEnabled(True)
        self._refresh_scan_models()
        self._show_status(f"Model trained and saved")
        _log(f"Training complete: {model_path}")

    def _on_train_error(self, msg: str):
        self._btn_train.setText("Train")
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

        model, model_label = self._load_selected_scan_model()
        if model is None:
            self._btn_auto_export.setEnabled(True)
            self._btn_scan.setEnabled(True)
            return

        self._scan_file_path = self._file_path
        self._scan_model_label = model_label
        self._show_status(f"Auto: scanning ({model_label})...")
        self._scan_worker = ScanWorker(
            self._file_path, model=model, threshold=threshold,
        )

        self._scan_worker.scan_done.connect(self._on_auto_scan_done)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.progress.connect(self._show_status)
        self._scan_worker.start()

    @staticmethod
    def _build_export_spans(regions: list[tuple[float, float, float]],
                            fuse_gap: float = 30.0,
                            spread: float = 3.0,
                            min_dur: float = 8.0,
                            ) -> list[list[float]]:
        """Build export position groups from fused scan regions.

        1. Merge regions closer than fuse_gap into spans.
        2. Drop spans shorter than min_dur.
        3. Place clips at spread intervals within each span.

        Returns list of groups, each group is a list of start times.
        """
        if not regions:
            return []

        # Merge nearby regions into spans
        sorted_r = sorted(regions, key=lambda r: r[0])
        spans: list[tuple[float, float]] = []
        s, e = sorted_r[0][0], sorted_r[0][1]
        for s2, e2, _ in sorted_r[1:]:
            if s2 - e <= fuse_gap:
                e = max(e, e2)
            else:
                spans.append((s, e))
                s, e = s2, e2
        spans.append((s, e))

        # Place clips within each span
        groups: list[list[float]] = []
        step = max(spread, 1.0)
        for s, e in spans:
            dur = e - s
            if dur < min_dur:
                continue
            clips: list[float] = []
            t = s
            while t + min_dur <= e:
                clips.append(t)
                t += step
            if clips:
                groups.append(clips)

        return groups

    def _on_auto_scan_done(self, regions: list) -> None:
        self._btn_scan.setEnabled(True)
        if self._file_path != getattr(self, '_scan_file_path', None):
            self._btn_auto_export.setEnabled(True)
            return

        self._timeline.set_scan_regions(regions)
        # Also save to scan panel
        model_label = getattr(self, '_scan_model_label', '')
        if model_label and self._file_path:
            self._scan_panel.add_scan_results(model_label, regions)

        self._auto_export_no_markers = True
        self._auto_export_regions(regions)

    def _auto_export_regions(self, regions: list) -> None:
        """Export clips from a list of (start, end, score) regions."""
        if not regions:
            self._show_status("Auto: no regions found")
            self._btn_auto_export.setEnabled(True)
            return

        spread = self._spn_spread.value()
        groups = self._build_export_spans(
            regions, fuse_gap=self._spn_auto_fuse.value(),
            spread=spread,
        )
        if not groups:
            self._show_status("Auto: no regions >= 8s")
            self._btn_auto_export.setEnabled(True)
            return

        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        fmt = self._cmb_format.currentText()
        image_sequence = fmt == "WebP sequence"
        ext = "" if image_sequence else ".mp4"
        vid_name = self._get_vid_folder(folder)
        vid_folder = os.path.join(folder, vid_name)
        os.makedirs(vid_folder, exist_ok=True)

        # Find next counter within the vid folder
        db_max = self._db.get_max_counter(vid_folder, name) if self._db else 0
        counter = max(1, db_max + 1)
        while os.path.exists(build_export_path(vid_folder, name, counter, sub=0)):
            counter += 1

        # Clips go flat inside vid folder, numbered sequentially
        jobs = []
        self._auto_export_positions = []
        for area_idx, group in enumerate(groups):
            group_name = f"{name}_{counter:03d}"
            for sub, start_t in enumerate(group):
                fname = f"{group_name}_a{area_idx + 1}_{sub}{ext}"
                out = os.path.join(vid_folder, fname)
                jobs.append((start_t, out, None, 0.5))
                self._auto_export_positions.append((start_t, out))
            counter += 1

        self._show_status(f"Auto: exporting {len(jobs)} clips...")

        short_side = self._spn_resize.value() or None
        self._export_short_side = short_side
        self._export_portrait = "Off"
        self._export_crop_center = 0.5
        self._export_format = fmt
        self._export_clip_count = 1
        self._export_spread = spread
        self._export_folder = folder
        self._export_folder_suffix = ""
        self._export_profile = self._profile

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
        start_t = 0.0
        for t, out in self._auto_export_positions:
            if os.path.normpath(out) == os.path.normpath(path):
                start_t = t
                break
        is_scan = getattr(self, '_auto_export_no_markers', False)
        label = self._txt_label.currentText().strip()
        category = self._cmb_category.currentText()
        self._db.add(
            os.path.basename(self._file_path),
            start_t,
            path,
            label=label,
            category=category,
            short_side=self._export_short_side,
            portrait_ratio="",
            crop_center=0.5,
            fmt=self._export_format,
            clip_count=1,
            spread=self._export_spread,
            profile=self._export_profile,
            source_path=self._file_path,
            scan_export=is_scan,
        )
        if not is_scan:
            upsert_clip_annotation(self._export_folder, path, label)
        self._show_status(f"Auto: {os.path.basename(path)}")
        _log(f"  auto clip done: {os.path.basename(path)}")

    def _on_auto_batch_done(self):
        n = len(self._auto_export_positions)
        self._btn_auto_export.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._btn_export.setEnabled(True)
        self._set_subprofile_btns_enabled(True)
        self._auto_export_no_markers = False
        self._refresh_markers()
        n_clips = self._db.get_clip_count(os.path.basename(self._file_path), self._profile)
        self._playlist.mark_done(self._file_path, n_clips)
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

    def _get_vid_folder(self, folder: str) -> str:
        """Return vid_NNN folder name for the currently loaded video."""
        if not self._file_path or not self._db:
            return "vid_001"
        return self._db.get_vid_folder(
            os.path.basename(self._file_path), self._profile, folder,
        )

    def _update_next_label(self):
        folder = self._txt_folder.text()
        name = self._txt_name.text() or "clip"
        vid_name = self._get_vid_folder(folder)
        vid_folder = os.path.join(folder, vid_name)
        # Start from the highest counter the DB knows about, so we never
        # reuse a counter if the folder is temporarily empty / unmounted.
        db_max = self._db.get_max_counter(vid_folder, name) if self._db else 0
        self._export_counter = max(1, db_max + 1)
        # Then also skip any files that exist on disk.
        while True:
            test_path = build_export_path(vid_folder, name, self._export_counter, sub=0)
            if not os.path.exists(test_path):
                break
            self._export_counter += 1
        n = self._spn_clips.value()
        base = f"{name}_{self._export_counter:03d}"
        if n == 1:
            self._lbl_next.setText(f"→ {vid_name}/{base}_0")
        else:
            self._lbl_next.setText(f"→ {vid_name}/{base}_0..{n - 1}")

    def _on_export(self, _=None, folder_suffix: str = ""):
        if not self._file_path:
            return
        if self._export_worker and self._export_worker.isRunning():
            self._show_status("Export already running…")
            return

        # Check for overlapping existing markers
        if not self._overwrite_path:
            clip_end = self._cursor + 8.0 + (self._spn_clips.value() - 1) * self._spn_spread.value()
            for t, _num, _path in self._timeline._markers:
                if abs(t - self._cursor) < 0.1:
                    continue  # same position (overwrite case)
                marker_end = t + 8.0
                if self._cursor < marker_end and clip_end > t:
                    self._show_status("Warning: overlaps with existing export", 3000)
                    break

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
            vid_name = self._get_vid_folder(folder)
            vid_folder = os.path.join(folder, vid_name)
            os.makedirs(vid_folder, exist_ok=True)
            # For subprofile exports, calculate counter independently.
            if folder_suffix:
                db_max_sub = self._db.get_max_counter(vid_folder, name) if self._db else 0
                counter = max(1, db_max_sub + 1)
                while True:
                    if image_sequence:
                        p = build_sequence_dir(vid_folder, name, counter, sub=0)
                    else:
                        p = build_export_path(vid_folder, name, counter, sub=0)
                    if not os.path.exists(p):
                        break
                    counter += 1
            else:
                counter = self._export_counter
            jobs = []
            for sub in range(n_clips):
                start = self._cursor + sub * spread
                if image_sequence:
                    out = build_sequence_dir(vid_folder, name, counter, sub=sub)
                else:
                    out = build_export_path(vid_folder, name, counter, sub=sub)
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
        self._export_crop_center = self._crop_center
        self._export_format = fmt
        self._export_clip_count = self._spn_clips.value()
        self._export_spread = self._spn_spread.value()
        self._export_folder = folder
        self._export_folder_suffix = folder_suffix
        self._export_profile = self._profile

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
            crop_center=self._export_crop_center,
            fmt=self._export_format,
            clip_count=self._export_clip_count,
            spread=self._export_spread,
            profile=self._export_profile,
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
        n_clips = self._db.get_clip_count(os.path.basename(self._file_path), self._profile)
        self._playlist.mark_done(self._file_path, n_clips)
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
        n_clips = self._db.get_clip_count(os.path.basename(self._file_path), self._profile)
        if n_clips:
            self._playlist.mark_done(self._file_path, n_clips)
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
        if hasattr(self, '_waveform_worker') and self._waveform_worker is not None:
            self._safe_disconnect(self._waveform_worker.done)
            self._waveform_worker.quit()
            self._waveform_worker.wait(2000)
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.cancel()
            self._export_worker.wait(3000)
        if hasattr(self, '_db_worker') and self._db_worker and self._db_worker.isRunning():
            self._db_worker.wait(1000)
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
