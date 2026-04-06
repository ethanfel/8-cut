import os
import subprocess
import sys
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMainWindow


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
