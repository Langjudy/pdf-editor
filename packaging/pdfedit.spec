# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：生成 dist\PdfEditor\PdfEditor.exe（单目录，启动快、不易被杀软误报）
# 用法：venv\python.exe -m PyInstaller packaging\pdfedit.spec --distpath packaging\dist --workpath packaging\build
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = collect_data_files("rapidocr_onnxruntime")          # OCR 模型（onnx）与配置
datas += [(os.path.join(ROOT, "pdfedit", "resources"), os.path.join("pdfedit", "resources"))]

excludes = [
    "tkinter", "pytest", "_pytest", "IPython", "matplotlib", "pymupdf4llm", "pymupdf_layout",
    "pymupdf.layout",                       # 可选的版面分析扩展（本程序不用，PyMuPDF 缺失时自动跳过）
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=[],
    excludes=excludes,
    noarchive=False,
)
# 精简：去掉用不到的大文件（视频解码、软件 OpenGL、Qt Quick / QML / PDF 模块、网络插件、多余翻译）
DROP = ("opencv_videoio_ffmpeg", "opengl32sw.dll", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "qpdf.dll",
        "Qt6VirtualKeyboard", os.path.join("plugins", "networkinformation"), os.path.join("plugins", "tls"))


def keep(entry):
    dest = entry[0]
    if any(k.lower() in dest.lower() for k in DROP):
        return False
    if os.path.join("PySide6", "translations") in dest and "zh_CN" not in dest:
        return False
    return True


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PdfEditor",
    console=False,
    icon=os.path.join(ROOT, "packaging", "app.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PdfEditor", upx=False)
