"""侧边面板：书签目录、搜索、区域选取。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..app.regions import Region, RegionStore
from ..core.engine import ENGINE_LOCK, GEN_SOURCE_ID, DocumentStore
from ..core.writer import RegionOptions

MM_PER_PT = 25.4 / 72.0


# ================================================================ 书签
class BookmarkPanel(QTreeWidget):
    jumpRequested = Signal(str, int)     # sid, pno

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.itemActivated.connect(self._activate)
        self.itemClicked.connect(self._activate)

    def populate(self, store: DocumentStore):
        self.clear()
        for src in store.sources.values():
            if src.id == GEN_SOURCE_ID:
                continue
            with ENGINE_LOCK:
                try:
                    toc = src.doc.get_toc(simple=True)
                except Exception:  # noqa: BLE001
                    toc = []
            root = QTreeWidgetItem([f"{src.name}（{src.page_count} 页）"])
            root.setData(0, Qt.UserRole, (src.id, 0))
            root.setForeground(0, Qt.darkGray)
            self.addTopLevelItem(root)
            stack = [root]
            for lvl, title, page in toc:
                item = QTreeWidgetItem([title])
                item.setData(0, Qt.UserRole, (src.id, max(0, page - 1)))
                while len(stack) > lvl:
                    stack.pop()
                stack[-1].addChild(item)
                stack.append(item)
            root.setExpanded(True)

    def _activate(self, item, _col=0):
        data = item.data(0, Qt.UserRole)
        if data:
            self.jumpRequested.emit(*data)


# ================================================================ 搜索 / 替换
class SearchPanel(QWidget):
    searchRequested = Signal(str, bool, bool, bool, str)   # 文本, 区分大小写, 全字, 正则, 范围
    replaceRequested = Signal(str, str)                    # 方式（current / checked）, 替换为
    hitActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.edit = QLineEdit(placeholderText="查找内容，回车搜索")
        self.edit.returnPressed.connect(self._go)
        btn = QPushButton("搜索")
        btn.clicked.connect(self._go)
        row.addWidget(self.edit)
        row.addWidget(btn)
        lay.addLayout(row)
        self.repl = QLineEdit(placeholderText="替换为（正则模式下可用 \\1 引用分组）")
        lay.addWidget(self.repl)
        opts = QHBoxLayout()
        self.case = QCheckBox("区分大小写")
        self.whole = QCheckBox("全字匹配")
        self.regex = QCheckBox("正则")
        self.scope = QComboBox()
        self.scope.addItems(["全部页面", "当前页", "选中页"])
        for w in (self.case, self.whole, self.regex, self.scope):
            opts.addWidget(w)
        lay.addLayout(opts)
        nav = QHBoxLayout()
        self.count = QLabel("")
        prev_b, next_b = QPushButton("上一个"), QPushButton("下一个")
        prev_b.clicked.connect(lambda: self.step(-1))
        next_b.clicked.connect(lambda: self.step(1))
        nav.addWidget(self.count, 1)
        nav.addWidget(prev_b)
        nav.addWidget(next_b)
        lay.addLayout(nav)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._row)
        lay.addWidget(self.list, 1)
        rb = QHBoxLayout()
        b1 = QPushButton("替换当前")
        b1.clicked.connect(lambda: self.replaceRequested.emit("current", self.repl.text()))
        b2 = QPushButton("全部替换（勾选项）")
        b2.clicked.connect(lambda: self.replaceRequested.emit("checked", self.repl.text()))
        b3 = QPushButton("全选/全不选")
        b3.clicked.connect(self._toggle_all)
        rb.addWidget(b1)
        rb.addWidget(b2)
        rb.addWidget(b3)
        lay.addLayout(rb)
        tip = QLabel("替换前可在列表中取消勾选不想替换的项。整行会按原字号、颜色重写；"
                     "扫描页需先完成 OCR 才能被搜索（阶段三）。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#5f6368")
        lay.addWidget(tip)

    def _go(self):
        scope = {0: "all", 1: "current", 2: "selected"}[self.scope.currentIndex()]
        self.searchRequested.emit(self.edit.text(), self.case.isChecked(), self.whole.isChecked(),
                                  self.regex.isChecked(), scope)

    def show_hits(self, hits):
        self.list.blockSignals(True)
        self.list.clear()
        for h in hits:
            it = QListWidgetItem(f"第 {h.index + 1} 页  {h.context}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.count.setText(f"共 {len(hits)} 处" if hits else "未找到")
        if hits:
            self.list.setCurrentRow(0)

    def checked_rows(self) -> list[int]:
        return [i for i in range(self.list.count()) if self.list.item(i).checkState() == Qt.Checked]

    def _toggle_all(self):
        state = Qt.Unchecked if len(self.checked_rows()) == self.list.count() else Qt.Checked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def step(self, d):
        n = self.list.count()
        if n:
            self.list.setCurrentRow((self.list.currentRow() + d) % n)

    def _row(self, row):
        if row >= 0:
            self.count.setText(f"{row + 1} / {self.list.count()}")
            self.hitActivated.emit(row)

    def focus_input(self, replace: bool = False):
        w = self.repl if replace and self.edit.text() else self.edit
        w.setFocus()
        w.selectAll()


# ================================================================ 文字属性
class TextPanel(QWidget):
    toolRequested = Signal(str)
    applyStyleRequested = Signal()
    deleteRequested = Signal()
    fontSettingsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        tools = QHBoxLayout()
        for key, label in (("text", "编辑文字"), ("addtext", "添加文字"), ("hand", "浏览")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.toolRequested.emit(k))
            tools.addWidget(b)
        lay.addLayout(tools)
        hint = QLabel("编辑文字：单击选中文字块，拖动可移动，Delete 删除；双击或回车进入编辑\n"
                      "添加文字：在页面任意位置单击输入\n"
                      "编辑框中 Esc 取消，Ctrl+Enter 提交（单行 Enter 提交）")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#5f6368")
        lay.addWidget(hint)

        self.info = QLabel("未选中文字块")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        box = QGroupBox("文字样式")
        f = QFormLayout(box)
        self.font = QComboBox()
        self.font.addItem("（保持原字体）", None)
        self.size = QDoubleSpinBox(decimals=1, minimum=1.0, maximum=500.0, value=12.0, suffix=" pt")
        self.color_btn = QPushButton()
        self.color = (0.0, 0.0, 0.0)
        self.color_btn.clicked.connect(self._pick_color)
        self.bold = QCheckBox("加粗")
        self.spacing = QDoubleSpinBox(decimals=1, minimum=-5.0, maximum=50.0, value=0.0, suffix=" pt")
        self.line = QDoubleSpinBox(decimals=2, minimum=0.5, maximum=5.0, value=1.0, singleStep=0.1)
        self.line.setSuffix(" 倍")
        self.reflow = QCheckBox("段落内自动换行（不超出原宽度）")
        self.reflow.setChecked(True)
        f.addRow("字体", self.font)
        f.addRow("字号", self.size)
        f.addRow("颜色", self.color_btn)
        f.addRow("", self.bold)
        f.addRow("字间距", self.spacing)
        f.addRow("行距", self.line)
        f.addRow(self.reflow)
        lay.addWidget(box)
        self._update_color_btn()

        sbox = QGroupBox("扫描页修改（OCR 后可用）")
        sf = QFormLayout(sbox)
        self.scan_how = QComboBox()
        self.scan_how.addItem("擦除原字并原位重写", False)
        self.scan_how.addItem("仅校对识别结果（只改文字层，不改图像）", True)
        self.scan_mode = QComboBox()
        self.scan_mode.addItem("矢量文字（清晰、可搜索）", "vector")
        self.scan_mode.addItem("渲染进图片（与原图风格一致）", "image")
        self.scan_texture = QCheckBox("模拟扫描质感（轻微模糊、噪点）")
        self.scan_info = QLabel("")
        self.scan_info.setWordWrap(True)
        self.scan_info.setStyleSheet("color:#1967d2")
        sf.addRow("方式", self.scan_how)
        sf.addRow("写入", self.scan_mode)
        sf.addRow(self.scan_texture)
        sf.addRow(self.scan_info)
        note = QLabel("说明：覆盖式修改在字体和墨色上无法与原件完全一致，近看可分辨修改痕迹。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#80868b")
        sf.addRow(note)
        lay.addWidget(sbox)

        b_apply = QPushButton("应用样式到选中文字块")
        b_apply.clicked.connect(self.applyStyleRequested.emit)
        b_del = QPushButton("删除选中文字块")
        b_del.clicked.connect(self.deleteRequested.emit)
        b_font = QPushButton("字体设置…")
        b_font.clicked.connect(self.fontSettingsRequested.emit)
        lay.addWidget(b_apply)
        lay.addWidget(b_del)
        lay.addWidget(b_font)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        lay.addStretch(1)

    def set_families(self, entries):
        cur = self.font.currentData()
        self.font.blockSignals(True)
        while self.font.count() > 1:
            self.font.removeItem(1)
        for e in entries:
            self.font.addItem(e.display, e.family)
        self.font.blockSignals(False)
        self.select_font(cur)

    def select_font(self, family):
        i = self.font.findData(family)
        self.font.setCurrentIndex(max(0, i))

    def _pick_color(self):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor.fromRgbF(*self.color), self, "文字颜色")
        if c.isValid():
            self.color = (c.redF(), c.greenF(), c.blueF())
            self._update_color_btn()

    def _update_color_btn(self):
        r, g, b = (int(v * 255) for v in self.color)
        self.color_btn.setStyleSheet(f"background: rgb({r},{g},{b}); min-height: 18px;")

    def load_block(self, block, display_font: str = ""):
        if block is None:
            self.info.setText("未选中文字块")
            return
        st = block.style()
        self.info.setText(f"已选中文字块（{len(block.lines)} 行）\n原字体：{display_font or block.font}")
        self.select_font(None)
        self.size.setValue(st.size)
        self.color = st.color
        self._update_color_btn()
        self.bold.setChecked(st.bold)
        self.spacing.setValue(0.0)
        self.line.setValue(1.0)

    def load_scan_style(self, st, scan_block=True):
        """扫描页文字块：把自动估算的字体风格、字号、颜色填入面板。"""
        if st is None:
            self.scan_info.setText("未能估算原文字样式，将使用当前面板设置")
            return
        self.select_font(st.family)
        self.size.setValue(st.size)
        self.color = tuple(st.color)
        self._update_color_btn()
        self.bold.setChecked(False)
        self.spacing.setValue(0.0)
        self.line.setValue(1.0)
        r, g, b = (int(v * 255) for v in st.color)
        self.scan_info.setText(f"识别为：{st.label}，约 {st.size:g} pt，墨色 #{r:02x}{g:02x}{b:02x}"
                               "（可在上方调整后再修改）")

    def scan_options(self):
        from ..core.scanedit import ScanEditOptions
        return ScanEditOptions(mode=self.scan_mode.currentData(), texture=self.scan_texture.isChecked(),
                               proofread_only=bool(self.scan_how.currentData()),
                               family=self.font.currentData(), size=self.size.value(), color=self.color)

    def style(self):
        from ..core.textedit import TextStyle
        return TextStyle(font=self.font.currentData(), size=self.size.value(), color=self.color,
                         bold=self.bold.isChecked(), char_spacing=self.spacing.value(),
                         line_spacing=self.line.value(), reflow=self.reflow.isChecked())

    def show_result(self, font_name: str, substituted: bool):
        if substituted:
            self.result.setText(f"⚠ 已使用替代字体：{font_name}\n（原字体缺少新输入的字符或未嵌入）")
            self.result.setStyleSheet("color:#e37400")
        else:
            self.result.setText(f"使用字体：{font_name}")
            self.result.setStyleSheet("color:#188038")


# ================================================================ 区域
class RegionPanel(QWidget):
    toolRequested = Signal(str)
    regionSelected = Signal(int, int)               # uid, rid
    generateRequested = Signal(int, list, object)   # uid, rids, RegionOptions
    exportRequested = Signal(int, list, object)
    templateRequested = Signal(int, list)
    eraseRequested = Signal(int, list)

    def __init__(self, regions: RegionStore, parent=None):
        super().__init__(parent)
        self.regions = regions
        self.uid = -1
        self._updating = False
        lay = QVBoxLayout(self)

        tools = QHBoxLayout()
        for key, label in (("rect", "矩形"), ("poly", "多边形"), ("free", "手绘"), ("hand", "浏览")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.toolRequested.emit(k))
            tools.addWidget(b)
        lay.addLayout(tools)
        hint = QLabel("矩形：拖拽画框，选中后可拖动边框/角点调整\n"
                      "多边形：逐点单击，双击或回车闭合，Esc 取消\n手绘：按住拖动画闭合轮廓")
        hint.setStyleSheet("color:#5f6368")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.title = QLabel("当前页选区")
        lay.addWidget(self.title)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemChanged.connect(self._renamed)
        self.list.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.list, 1)

        geo = QGroupBox("位置与尺寸（毫米，原始页面坐标）")
        form = QFormLayout(geo)
        self.spins = {}
        for key, label in (("x", "X"), ("y", "Y"), ("w", "宽"), ("h", "高")):
            s = QDoubleSpinBox(decimals=1, maximum=100000.0, minimum=0.0, singleStep=1.0)
            s.setKeyboardTracking(False)
            s.valueChanged.connect(self._geo_changed)
            self.spins[key] = s
            form.addRow(label, s)
        lay.addWidget(geo)

        row = QHBoxLayout()
        b_del = QPushButton("删除选区")
        b_del.clicked.connect(self._delete)
        b_clear = QPushButton("清除本页")
        b_clear.clicked.connect(lambda: self.regions.clear(self.uid))
        row.addWidget(b_del)
        row.addWidget(b_clear)
        lay.addLayout(row)

        opt = QGroupBox("新页面选项")
        f2 = QFormLayout(opt)
        self.size = QComboBox()
        self.size.addItem("与选区等大（1:1）", "original")
        for k in ("A4", "A3", "A2", "A1", "A0", "Letter"):
            self.size.addItem(f"缩放适配 {k}", k)
        self.margin = QDoubleSpinBox(decimals=1, maximum=100.0, value=10.0, suffix=" mm")
        self.rotate = QComboBox()
        self.rotate.addItems(["0°", "90°", "180°", "270°"])
        self.remove_out = QCheckBox("彻底移除选区外内容")
        self.remove_out.setToolTip("开启后选区外的文字、图形、图像会从新页面中真正删除，而不只是隐藏")
        self.mark = QCheckBox("角落标注来源（文件名、页码、比例）")
        f2.addRow("页面尺寸", self.size)
        f2.addRow("边距", self.margin)
        f2.addRow("旋转", self.rotate)
        f2.addRow(self.remove_out)
        f2.addRow(self.mark)
        lay.addWidget(opt)

        b_gen = QPushButton("生成区域页（插入工作区）")
        b_gen.setStyleSheet("font-weight:bold")
        b_gen.clicked.connect(lambda: self._emit(self.generateRequested))
        b_exp = QPushButton("直接导出为 PDF…")
        b_exp.clicked.connect(lambda: self._emit(self.exportRequested))
        b_tpl = QPushButton("将选区应用到选中页面（模板）")
        b_tpl.setToolTip("把当前页的选区复制到缩略图中选中的其他页面，适用于同一套图纸的标题栏、详图位置")
        b_tpl.clicked.connect(lambda: self.templateRequested.emit(self.uid, self.selected_ids()))
        b_erase = QPushButton("擦除选区内容（扫描页）")
        b_erase.setToolTip("擦除扫描图片中选区内的所有墨迹（手写字、污点等），并删除该处的 OCR 文字")
        b_erase.clicked.connect(lambda: self.eraseRequested.emit(self.uid, self.selected_ids()))
        lay.addWidget(b_gen)
        lay.addWidget(b_exp)
        lay.addWidget(b_tpl)
        lay.addWidget(b_erase)
        regions.listeners.append(self._on_regions_changed)
        self._update_geo_enabled()

    def options(self) -> RegionOptions:
        return RegionOptions(
            size_mode=self.size.currentData(),
            margin_mm=self.margin.value(),
            rotate=self.rotate.currentIndex() * 90,
            remove_outside=self.remove_out.isChecked(),
            mark_source=self.mark.isChecked(),
        )

    def _emit(self, sig):
        sig.emit(self.uid, self.selected_ids(), self.options())

    def selected_ids(self) -> list[int]:
        ids = [it.data(Qt.UserRole) for it in self.list.selectedItems()]
        return ids or [r.id for r in self.regions.get(self.uid)]

    def set_page(self, uid: int, page_no: int | None = None):
        self.uid = uid
        if page_no is not None:
            self.title.setText(f"第 {page_no} 页的选区")
        self._reload()

    def _reload(self, keep: list[int] | None = None):
        keep = keep if keep is not None else [it.data(Qt.UserRole) for it in self.list.selectedItems()]
        self._updating = True
        self.list.clear()
        for r in self.regions.get(self.uid):
            it = QListWidgetItem(r.name)
            it.setData(Qt.UserRole, r.id)
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setToolTip("矩形选区" if r.is_rect else "画笔（不规则）选区")
            self.list.addItem(it)
            if r.id in keep:
                it.setSelected(True)
        self._updating = False
        self._show_geo()

    def _on_regions_changed(self, uid):
        if uid == self.uid:
            self._reload()

    def select_region(self, rid: int):
        self._updating = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setSelected(it.data(Qt.UserRole) == rid)
        self._updating = False
        self._show_geo()

    def _current_region(self) -> Region | None:
        items = self.list.selectedItems()
        if len(items) != 1:
            return None
        return self.regions.find(self.uid, items[0].data(Qt.UserRole))

    def _sel_changed(self):
        if self._updating:
            return
        self._show_geo()
        r = self._current_region()
        if r:
            self.regionSelected.emit(self.uid, r.id)

    def _update_geo_enabled(self):
        r = self._current_region()
        for s in self.spins.values():
            s.setEnabled(bool(r and r.is_rect))

    def _show_geo(self):
        r = self._current_region()
        self._updating = True
        vals = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
        if r:
            x0, y0, x1, y1 = r.bbox()
            vals = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        for k, s in self.spins.items():
            s.blockSignals(True)
            s.setValue(vals[k] * MM_PER_PT)
            s.blockSignals(False)
        self._updating = False
        self._update_geo_enabled()

    def _geo_changed(self, _v):
        r = self._current_region()
        if self._updating or not r or not r.is_rect:
            return
        x, y = self.spins["x"].value() / MM_PER_PT, self.spins["y"].value() / MM_PER_PT
        w, h = self.spins["w"].value() / MM_PER_PT, self.spins["h"].value() / MM_PER_PT
        if w < 1 or h < 1:
            return
        self.regions.set_points(self.uid, r.id, Region.rect_points(x, y, x + w, y + h))

    def _renamed(self, item):
        if not self._updating:
            self.regions.rename(self.uid, item.data(Qt.UserRole), item.text())

    def _delete(self):
        for it in self.list.selectedItems():
            self.regions.remove(self.uid, it.data(Qt.UserRole))
