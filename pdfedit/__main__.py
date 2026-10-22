"""启动入口：python -m pdfedit [文件...]"""
import os
import sys

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .ui.main_window import APP_NAME, MainWindow


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":      # 安装包自检（不显示窗口）
        from .selftest import run
        return run(sys.argv[2])
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "app.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    app.setStyle("Fusion")
    if sys.platform == "win32":          # 其他平台使用系统默认界面字体
        app.setFont(QFont("Microsoft YaHei UI", 9))
    win = MainWindow()
    win.show()
    files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    projects = [f for f in files if f.lower().endswith(".pdfproj")]
    if projects:                         # 双击项目文件：打开项目
        win.open_project(projects[0])
    elif files:
        win.import_files(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
