import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from doccollector.ui import MainWindow
from doccollector.ui.theme import apply_theme


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DocCollector")
    app.setApplicationVersion("1.0.0")
    apply_theme(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
