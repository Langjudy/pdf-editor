"""对话框：导出设置、后台任务进度。"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
                               QMessageBox, QProgressDialog, QPushButton, QRadioButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..app.exporter import MODES
from ..core.naming import DEFAULT_TEMPLATE, VARIABLES, validate_template
from ..core.writer import SaveOptions


# ================================================================ 后台任务
def run_task(parent, title: str, fn, *args, cancellable: bool = True):
    """在后台线程执行 fn(*args, progress)，期间显示进度对话框，返回结果或抛出异常。

    progress(done, total, text="") 返回 False 表示用户已取消。
    """
    state = {"done": 0, "total": 0, "text": "", "cancel": False, "result": None,
             "error": None, "finished": False}

    def progress(done, total, text=""):
        state.update(done=done, total=total, text=text)
        return not state["cancel"]

    def worker():
        try:
            state["result"] = fn(*args, progress)
        except BaseException as e:  # noqa: BLE001
            state["error"] = e
        finally:
            state["finished"] = True

    dlg = QProgressDialog(title, "取消" if cancellable else None, 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(400)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    loop = QEventLoop()
    timer = QTimer(interval=80)

    def poll():
        if dlg.wasCanceled():
            state["cancel"] = True
            dlg.setLabelText("正在取消…")
        if state["total"]:
            dlg.setMaximum(state["total"])
            dlg.setValue(min(state["done"], state["total"]))
            if state["text"] and not state["cancel"]:
                dlg.setLabelText(f"{title}\n{state['text']}")
        if state["finished"]:
            timer.stop()
            loop.quit()

    timer.timeout.connect(poll)
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    timer.start()
    loop.exec()
    dlg.close()
    dlg.deleteLater()
    if state["error"] is not None:
        raise state["error"]
    return state["result"]


# ================================================================ 导出
class ExportDialog(QDialog):
    def __init__(self, parent, has_selection: bool, total: int, selected: int, default_dir: str,
                 default_name: str, has_redactions: bool = False):
        super().__init__(parent)
        self.setWindowTitle("导出")
        self.default_dir = default_dir
        self.default_name = default_name
        lay = QVBoxLayout(self)

        mode_box = QGroupBox("导出方式")
        ml = QVBoxLayout(mode_box)
        self.group = QButtonGroup(self)
        self.radios = {}
        for key, label in MODES.items():
            text = label
            if key.startswith("sel"):
                text += f"（已选 {selected} 页）"
            elif key in ("all_one", "all_each"):
                text += f"（共 {total} 页）"
            rb = QRadioButton(text)
            if key.startswith("sel") and not has_selection:
                rb.setEnabled(False)
            self.group.addButton(rb)
            self.radios[key] = rb
            ml.addWidget(rb)
        self.radios["all_one"].setChecked(True)
        self.group.buttonToggled.connect(lambda *_: self._update())
        lay.addWidget(mode_box)

        # 拆分 / 图片参数
        self.split_box = QGroupBox("拆分与命名")
        fl = QFormLayout(self.split_box)
        self.scope = QComboBox()
        self.scope.addItem("全部页面", "all")
        self.scope.addItem("选中页面", "sel")
        if not has_selection:
            self.scope.model().item(1).setEnabled(False)
        self.chunk = QSpinBox(minimum=1, maximum=100000, value=10, suffix=" 页 / 文件")
        self.template = QLineEdit(DEFAULT_TEMPLATE)
        self.template.setToolTip("可用变量：" + "、".join("{" + v + "}" for v in VARIABLES)
                                 + "\n数字可设位数，如 {序号:3} → 001")
        tl = QHBoxLayout()
        tl.addWidget(self.template)
        for v in VARIABLES:
            b = QPushButton("{" + v + "}")
            b.setFlat(True)
            b.clicked.connect(lambda _=False, v=v: self.template.insert("{" + v + "}"))
            tl.addWidget(b)
        tw = QWidget()
        tw.setLayout(tl)
        tl.setContentsMargins(0, 0, 0, 0)
        self.conflict = QComboBox()
        self.conflict.addItem("自动加后缀", "suffix")
        self.conflict.addItem("覆盖", "overwrite")
        self.conflict.addItem("跳过", "skip")
        self.img_fmt = QComboBox()
        self.img_fmt.addItems(["png", "jpg"])
        self.img_dpi = QSpinBox(minimum=72, maximum=600, value=150, suffix=" DPI")
        fl.addRow("页面范围", self.scope)
        fl.addRow("每个文件页数", self.chunk)
        fl.addRow("命名模板", tw)
        fl.addRow("重名时", self.conflict)
        fl.addRow("图片格式", self.img_fmt)
        fl.addRow("分辨率", self.img_dpi)
        lay.addWidget(self.split_box)

        self.pdf_box = QGroupBox("PDF 输出选项")
        pl = QFormLayout(self.pdf_box)
        self.compress = QComboBox()
        self.compress.addItem("平衡（推荐）", "balanced")
        self.compress.addItem("不压缩", "none")
        self.compress.addItem("最小体积（重采样图片）", "min")
        self.paper = QComboBox()
        self.paper.addItem("保持各页原尺寸", None)
        for k in ("A4", "A3", "A2", "A1", "A0", "Letter"):
            self.paper.addItem(f"统一缩放到 {k}", k)
        self.paper.setToolTip("统一缩放会把页面内容重新放置到新纸张上，批注和链接将被拼合或丢弃")
        self.user_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="留空则不加密")
        self.owner_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="留空则与打开密码相同")
        pl.addRow("压缩", self.compress)
        pl.addRow("页面尺寸", self.paper)
        pl.addRow("打开密码", self.user_pw)
        pl.addRow("权限密码", self.owner_pw)
        self.annots = QComboBox()
        self.annots.addItem("保留为可编辑批注", False)
        self.annots.addItem("拼合到页面（不可再编辑）", True)
        from PySide6.QtWidgets import QCheckBox
        self.scrub = QCheckBox("清理隐私信息（元数据、脚本、缩略图、附件等）")
        self.scrub.setChecked(has_redactions)
        if has_redactions:
            self.scrub.setToolTip("所选页面做过涂黑，建议清理，避免元数据中残留敏感信息")
        pl.addRow("批注", self.annots)
        pl.addRow(self.scrub)
        lay.addWidget(self.pdf_box)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("导出…")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.target = ""
        self._update()

    def mode(self) -> str:
        for k, rb in self.radios.items():
            if rb.isChecked():
                return k
        return "all_one"

    def _update(self):
        m = self.mode()
        multi = m not in ("all_one", "sel_one")
        self.split_box.setVisible(multi)
        self.scope.setEnabled(m in ("chunk", "by_source", "images"))
        self.chunk.setEnabled(m == "chunk")
        self.template.setEnabled(m != "by_source")
        self.img_fmt.setEnabled(m == "images")
        self.img_dpi.setEnabled(m == "images")
        self.pdf_box.setEnabled(m != "images")
        self.adjustSize()

    def use_selection(self) -> bool:
        m = self.mode()
        if m.startswith("sel"):
            return True
        if m.startswith("all"):
            return False
        return self.scope.currentData() == "sel"

    def save_options(self) -> SaveOptions:
        return SaveOptions(compression=self.compress.currentData(), paper=self.paper.currentData(),
                           user_pw=self.user_pw.text(), owner_pw=self.owner_pw.text(),
                           flatten_annots=bool(self.annots.currentData()), scrub=self.scrub.isChecked())

    def _accept(self):
        m = self.mode()
        if m not in ("all_one", "sel_one", "by_source"):
            try:
                validate_template(self.template.text())
            except ValueError as e:
                QMessageBox.warning(self, "命名模板有误", str(e))
                return
        if m in ("all_one", "sel_one"):
            path, _ = QFileDialog.getSaveFileName(
                self, "导出为", os.path.join(self.default_dir, self.default_name + ".pdf"), "PDF 文件 (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        else:
            path = QFileDialog.getExistingDirectory(self, "选择导出文件夹", self.default_dir)
            if not path:
                return
        self.target = path
        self.accept()


def ask_page_number(parent, title: str, label: str, maximum: int, value: int = 1, minimum: int = 1):
    from PySide6.QtWidgets import QInputDialog
    v, ok = QInputDialog.getInt(parent, title, label, value, minimum, maximum)
    return v if ok else None


def show_export_done(parent, files: list[str], skipped: list[str], folder: str):
    box = QMessageBox(parent)
    box.setWindowTitle("导出完成")
    text = f"已导出 {len(files)} 个文件到：\n{folder}"
    if len(files) == 1:
        text = f"已导出：\n{files[0]}"
    if skipped:
        text += f"\n\n跳过 {len(skipped)} 个重名文件。"
    box.setText(text)
    open_btn = box.addButton("打开所在文件夹", QMessageBox.ActionRole)
    box.addButton("关闭", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is open_btn:
        open_folder(files[0] if len(files) == 1 else folder)


def open_folder(path: str):
    """在系统文件管理器中打开（文件则定位选中）。"""
    import subprocess
    import sys
    path = os.path.normpath(path)
    is_file = os.path.isfile(path)
    if sys.platform == "win32":
        if is_file:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if is_file else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if is_file else path])



# ================================================================ 扫描件
class OcrDialog(QDialog):
    """选择 OCR 范围。"""

    def __init__(self, parent, n_selected: int, n_scan: int, n_total: int):
        super().__init__(parent)
        self.setWindowTitle("OCR 文字识别")
        lay = QVBoxLayout(self)
        box = QGroupBox("识别范围")
        bl = QVBoxLayout(box)
        self.group = QButtonGroup(self)
        self.radios = {}
        for key, text, ok in (("current", "当前页", True),
                              ("selected", f"选中的页面（{n_selected} 页）", n_selected > 0),
                              ("scan", f"全部扫描页（{n_scan} 页）", n_scan > 0),
                              ("all", f"全部页面（{n_total} 页）", True)):
            rb = QRadioButton(text)
            rb.setEnabled(ok)
            self.group.addButton(rb)
            self.radios[key] = rb
            bl.addWidget(rb)
        (self.radios["scan"] if n_scan else self.radios["current"]).setChecked(True)
        lay.addWidget(box)
        from PySide6.QtWidgets import QCheckBox, QLabel
        self.skip = QCheckBox("跳过已识别的页面")
        self.skip.setChecked(True)
        lay.addWidget(self.skip)
        tip = QLabel("识别在后台进行，可继续浏览和编辑；状态栏显示进度，可随时取消。\n"
                     "识别结果写成透明文字层：页面外观不变，可搜索、可复制。\n"
                     "倾斜明显的扫描件建议先做「图像预处理 → 自动纠偏」。")
        tip.setStyleSheet("color:#5f6368")
        lay.addWidget(tip)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始识别")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def scope(self) -> str:
        return next(k for k, rb in self.radios.items() if rb.isChecked())


def _to_pixmap(arr, max_w: int, max_h: int):
    import numpy as np
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QImage, QPixmap
    a = np.ascontiguousarray(arr)
    if a.ndim == 2:
        img = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_Grayscale8)
    else:
        img = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGB888)
    return QPixmap.fromImage(img.copy()).scaled(max_w, max_h, _Qt.KeepAspectRatio, _Qt.SmoothTransformation)


class PreprocessDialog(QDialog):
    """图像预处理（F-SCN-01~04）：选项 + 当前页前后对比预览，确认后应用。"""

    def __init__(self, parent, preview_fn, n_selected: int, n_scan: int):
        super().__init__(parent)
        from PySide6.QtWidgets import QCheckBox, QLabel
        from ..core.scan import PreOptions
        self.setWindowTitle("图像预处理")
        self.preview_fn = preview_fn
        lay = QVBoxLayout(self)
        opts = QGroupBox("处理项目")
        ol = QHBoxLayout(opts)
        d = PreOptions()
        self.cb = {}
        for key, text in (("deskew", "自动纠偏（±15°）"), ("border", "去黑边"), ("despeckle", "去噪点 / 污渍"),
                          ("enhance", "对比度增强"), ("binarize", "二值化（黑白文档）")):
            c = QCheckBox(text)
            c.setChecked(getattr(d, key))
            self.cb[key] = c
            ol.addWidget(c)
        lay.addWidget(opts)
        row = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("当前页", "current")
        self.scope.addItem(f"选中的页面（{n_selected} 页）", "selected")
        self.scope.addItem(f"全部扫描页（{n_scan} 页）", "scan")
        if not n_selected:
            self.scope.model().item(1).setEnabled(False)
        btn = QPushButton("预览当前页")
        btn.clicked.connect(self._preview)
        row.addWidget(QLabel("应用到"))
        row.addWidget(self.scope, 1)
        row.addWidget(btn)
        lay.addLayout(row)
        pv = QHBoxLayout()
        self.before = QLabel("处理前")
        self.after = QLabel("处理后（点击“预览当前页”）")
        for w in (self.before, self.after):
            w.setMinimumSize(360, 480)
            w.setAlignment(Qt.AlignCenter)
            w.setStyleSheet("background:#5f6368; color:white;")
            pv.addWidget(w)
        lay.addLayout(pv)
        self.info = QLabel("")
        lay.addWidget(self.info)
        warn = QLabel("注意：预处理会改变图像，已有的 OCR 文字层会被清除，需要重新识别。可撤销。")
        warn.setStyleSheet("color:#e37400")
        lay.addWidget(warn)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("应用")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.reocr = QCheckBox("应用后自动重新 OCR")
        self.reocr.setChecked(True)
        lay.addWidget(self.reocr)

    def options(self):
        from ..core.scan import PreOptions
        return PreOptions(**{k: c.isChecked() for k, c in self.cb.items()})

    def _preview(self):
        res = self.preview_fn(self.options())
        if res is None:
            self.info.setText("当前页没有扫描底图（扫描图片需覆盖页面大部分区域）")
            return
        before, after, info = res
        self.before.setPixmap(_to_pixmap(before, 360, 480))
        self.after.setPixmap(_to_pixmap(after, 360, 480))
        self.info.setText(f"检测到倾斜角 {info['angle']:+.2f}°" if self.cb["deskew"].isChecked() else "")
