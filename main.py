import sys
from PyQt6.QtWidgets import QApplication, QMainWindow


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
