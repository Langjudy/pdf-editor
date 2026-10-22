"""打包入口（PyInstaller 需要顶层脚本）。"""
import sys

from pdfedit.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
