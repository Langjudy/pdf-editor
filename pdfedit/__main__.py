"""启动入口：python -m pdfedit [文件...]"""
import os
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .ui.main_window import APP_NAME, MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    win = MainWindow()
    win.show()
    files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if files:
        win.import_files(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
