import os
import sys
from PyQt6.QtWidgets import QApplication, QMainWindow


def build_export_path(folder: str, basename: str, counter: int) -> str:
    filename = f"{basename}_{counter:03d}.mp4"
    return os.path.join(folder, filename)


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60 * 10) / 10  # floor-truncate to 1dp, prevents "X:60.0" rollover
    return f"{m}:{s:04.1f}"


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
