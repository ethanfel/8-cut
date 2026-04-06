import os
import subprocess
import sys
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget


def build_export_path(folder: str, basename: str, counter: int) -> str:
    filename = f"{basename}_{counter:03d}.mp4"
    return os.path.join(folder, filename)


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60 * 10) / 10  # floor-truncate to 1dp, prevents "X:60.0" rollover
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
