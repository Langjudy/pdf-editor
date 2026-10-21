"""主窗口：菜单、工具栏、缩略图面板、主视图、侧边面板，以及所有页面操作的入口。"""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                               QFileDialog, QFormLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QSpinBox,
                               QTabWidget, QToolBar)

from ..app import project
from ..app.exporter import ExportJob, origin_of, origin_source, run_export
from ..app.regions import RegionStore
from ..app.annotservice import AnnotService
from ..app.scanservice import ScanService
from ..app.textservice import EditLocked, TextService
from ..app.workspace import PageRef, Workspace, parse_range
from ..core import annots as A
from ..core import pagetype
from ..core.fonts import FontManager
from ..core.engine import (ENGINE_LOCK, GEN_SOURCE_ID, IMAGE_EXTS, DocumentStore, ImportError_,
                           PasswordRequired)
from ..core.writer import (ExportCancelled, RegionShape, SaveOptions, build_region_page,
                           save_document)
from .dialogs import (ExportDialog, OcrDialog, PreprocessDialog, ask_page_number, run_task,
                      show_export_done)
from .ocr_worker import OcrWorker
from .panels import AnnotPanel, BookmarkPanel, RegionPanel, SearchPanel, TextPanel
from .render_service import RenderService
from .thumbs import THUMB_SIZES, ThumbModel, ThumbView
from .viewer import PageView

APP_NAME = "PDF 编辑器"
FILE_FILTER = "PDF 与图片 (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.bmp);;PDF 文件 (*.pdf);;所有文件 (*)"
AUTOSAVE_MS = 5 * 60 * 1000
ZOOM_PRESETS = [10, 25, 50, 75, 100, 125, 150, 200, 300, 400, 800, 1600, 3200, 6400]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("PdfEdit", "PdfEdit")
        self.store = DocumentStore()
        self.ws = Workspace()
        self.regions = RegionStore()
        self.service = RenderService(self.store)
        self.project_path: str | None = None
        self._clipboard: list[PageRef] = []
        self._sel_uids: list[int] = []
        self._cur_uid: int | None = None
        self._syncing = False
        self._hits = []
        self._autosaved_dirty = False

        self._build_widgets()
        self._setup_text()
        self._setup_scan()
        self._setup_annots()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self.ws.listeners.append(self._on_ws_changed)
        self.service.typeReady.connect(lambda *_: self._update_status())
        self.service.classifyProgress.connect(self._on_classify_progress)

        self.setAcceptDrops(True)
        self.resize(1400, 900)
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._update_title()
        self._update_actions()

        self.autosave_timer = QTimer(self, interval=AUTOSAVE_MS)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start()
        QTimer.singleShot(0, self._check_recovery)

    # ================================================================ 构建界面
    def _build_widgets(self):
        self.viewer = PageView(self.ws, self.store, self.service, self.regions)
        self.setCentralWidget(self.viewer)
        self.viewer.currentChanged.connect(self._on_viewer_page)
        self.viewer.zoomChanged.connect(self._on_zoom_changed)
        self.viewer.regionDrawn.connect(self._on_region_drawn)
        self.viewer.regionSelected.connect(self._on_viewer_region_selected)
        self.viewer.statusMessage.connect(lambda m: self.pos_label.setText(m))

        self.thumb_model = ThumbModel(self.ws, self.store, self.service)
        self.thumbs = ThumbView(self.thumb_model)
        self.thumbs.pagesDropped.connect(self._on_pages_dropped)
        self.thumbs.filesDropped.connect(lambda paths, row: self.import_files(paths, row))
        self.thumbs.contextRequested.connect(self._thumb_menu)
        self.thumbs.selectionModel().selectionChanged.connect(self._on_thumb_selection)
        self.thumbs.selectionModel().currentChanged.connect(self._on_thumb_current)

        self.bookmarks = BookmarkPanel()
        self.bookmarks.jumpRequested.connect(self._jump_to_source_page)
        left = QTabWidget()
        left.addTab(self.thumbs, "缩略图")
        left.addTab(self.bookmarks, "书签")
        self.left_dock = QDockWidget("页面", self)
        self.left_dock.setObjectName("pages")
        self.left_dock.setWidget(left)
        self.left_dock.setMinimumWidth(200)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)
        self.resizeDocks([self.left_dock], [330], Qt.Horizontal)

        self.region_panel = RegionPanel(self.regions)
        self.region_panel.toolRequested.connect(self.set_tool)
        self.region_panel.regionSelected.connect(
            lambda uid, rid: self.viewer.select_region(uid, rid, scroll=False))
        self.region_panel.generateRequested.connect(self._generate_regions)
        self.region_panel.exportRequested.connect(self._export_regions)
        self.region_panel.templateRequested.connect(self._apply_region_template)
        self.region_panel.eraseRequested.connect(self._erase_regions)
        self.region_panel.redactRequested.connect(self._redact_regions)
        self.search_panel = SearchPanel()
        self.search_panel.searchRequested.connect(self._search)
        self.search_panel.replaceRequested.connect(self._replace)
        self.search_panel.hitActivated.connect(self._on_hit)
        self.text_panel = TextPanel()
        self.text_panel.toolRequested.connect(self.set_tool)
        self.text_panel.applyStyleRequested.connect(self._apply_text_style)
        self.text_panel.deleteRequested.connect(self._delete_selected_text)
        self.text_panel.fontSettingsRequested.connect(self.font_settings)
        self.annot_panel = AnnotPanel(self.settings.value("annot_author", "") or A.default_author())
        self.annot_panel.toolRequested.connect(self.set_tool)
        self.annot_panel.applyRequested.connect(self._apply_annot_style)
        self.annot_panel.deleteRequested.connect(self._delete_selected_annot)
        self.annot_panel.jumpRequested.connect(self._jump_annot)
        self.annot_panel.refreshRequested.connect(self._refresh_annot_list)
        self.annot_panel.exportSummaryRequested.connect(self._export_annot_summary)
        self.annot_panel.author.editingFinished.connect(
            lambda: self.settings.setValue("annot_author", self.annot_panel.author.text().strip()))
        right = QTabWidget()
        right.addTab(self._scrollable(self.text_panel), "文字")
        right.addTab(self._scrollable(self.annot_panel), "批注")
        right.addTab(self._scrollable(self.region_panel), "区域选取")
        right.addTab(self.search_panel, "查找替换")
        self.right_tabs = right
        right.currentChanged.connect(self._refresh_annot_list)
        self.right_dock = QDockWidget("工具", self)
        self.right_dock.setObjectName("tools")
        self.right_dock.setWidget(right)
        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.resizeDocks([self.right_dock], [380], Qt.Horizontal)

    def _show_tab(self, panel):
        for i in range(self.right_tabs.count()):
            w = self.right_tabs.widget(i)
            if w is panel or getattr(w, "widget", lambda: None)() is panel:
                self.right_tabs.setCurrentIndex(i)
                return

    def _current_panel(self):
        w = self.right_tabs.currentWidget()
        inner = getattr(w, "widget", None)
        return inner() if callable(inner) else w

    @staticmethod
    def _scrollable(panel):
        """面板内容较多时可以滚动（小屏幕、高分屏缩放下不会被挤压）。"""
        from PySide6.QtWidgets import QFrame, QScrollArea
        sa = QScrollArea()
        sa.setWidget(panel)
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # 只纵向滚动，宽度随停靠栏自适应
        panel.setMinimumHeight(panel.sizeHint().height())
        return sa

    def _act(self, text, slot, shortcut=None, tip=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcuts([QKeySequence(s) for s in (shortcut if isinstance(shortcut, list) else [shortcut])])
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        a.setCheckable(checkable)
        a.triggered.connect(lambda checked=False: slot())
        return a

    def _build_actions(self):
        A = self._act
        self.a_new = A("新建工作区", self.new_workspace, "Ctrl+N")
        self.a_import = A("导入 PDF / 图片…", self.import_dialog, "Ctrl+O", "导入文件，追加到工作区末尾")
        self.a_import_at = A("导入并插入到…", self.import_dialog_at, "Ctrl+Shift+O")
        self.a_open_proj = A("打开项目…", self.open_project)
        self.a_save_proj = A("保存项目", self.save_project, "Ctrl+S")
        self.a_save_proj_as = A("项目另存为…", lambda: self.save_project(True))
        self.a_export = A("导出…", self.export_dialog, "Ctrl+E", "导出合并 / 拆分 / 图片")
        self.a_quit = A("退出", self.close, "Ctrl+Q")

        self.a_undo = A("撤销", self.ws.undo, "Ctrl+Z")
        self.a_redo = A("重做", self.ws.redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        self.a_copy = A("复制页面", self.copy_pages, "Ctrl+C")
        self.a_paste = A("粘贴页面", self.paste_pages, "Ctrl+V")
        self.a_dup = A("创建副本", self.duplicate_pages, "Ctrl+D", "复制选中页，副本插入到原页面之后")
        self.a_delete = A("删除页面", self.delete_pages, "Delete")
        self.a_sel_all = A("全选", lambda: self.thumbs.selectAll(), "Ctrl+A")
        self.a_sel_inv = A("反选", self.invert_selection, "Ctrl+I")
        self.a_sel_range = A("按页码范围选择…", self.select_range, "Ctrl+G")
        self.a_find = A("查找…", self.show_search, "Ctrl+F")
        self.a_find_replace = A("查找替换…", lambda: self.show_search(True), "Ctrl+H")
        self.a_font_settings = A("字体设置…", self.font_settings)
        self.a_ocr = A("OCR 文字识别…", self.ocr_dialog, "Ctrl+Shift+R", "识别扫描页文字，生成可搜索的透明文字层")
        self.a_ocr_cancel = A("取消 OCR", self.cancel_ocr)
        self.a_auto_ocr = A("导入时自动 OCR 扫描页", lambda: self.settings.setValue(
            "auto_ocr", self.a_auto_ocr.isChecked()), checkable=True)
        self.a_auto_ocr.setChecked(str(self.settings.value("auto_ocr", "false")).lower() == "true")
        self.a_preprocess = A("图像预处理（纠偏 / 去黑边 / 去噪 / 增强 / 二值化）…", self.preprocess_dialog)
        self.a_restore = A("恢复原始页面", self.restore_original, None,
                           "撤销该页的所有扫描修改、文字编辑与 OCR，回到导入时的样子（可撤销）")

        self.a_rot_l = A("向左旋转 90°", lambda: self.rotate_pages(270), "Ctrl+L")
        self.a_rot_r = A("向右旋转 90°", lambda: self.rotate_pages(90), "Ctrl+R")
        self.a_rot_180 = A("旋转 180°", lambda: self.rotate_pages(180))
        self.a_swap = A("交换两页…", self.swap_pages, None, "选中两页直接交换，或输入两个页码")
        self.a_move_to = A("移动到页码…", self.move_pages_to, "Ctrl+M")
        self.a_replace = A("用外部 PDF 页替换…", self.replace_with_external)
        self.a_blank = A("插入空白页", self.insert_blank, None, "尺寸与当前页一致")
        self.a_reverse = A("倒序排列", self.reverse_pages)
        self.a_interleave = A("奇偶页重排（双面扫描整理）…", self.interleave_pages)

        self.a_mode_cont = A("连续滚动", lambda: self.viewer.set_mode("continuous"), checkable=True)
        self.a_mode_single = A("单页", lambda: self.viewer.set_mode("single"), checkable=True)
        g = QActionGroup(self)
        for a in (self.a_mode_cont, self.a_mode_single):
            g.addAction(a)
        self.a_mode_cont.setChecked(True)
        self.a_fit_w = A("适合宽度", lambda: self.viewer.set_fit("width"), "Ctrl+2")
        self.a_fit_p = A("适合页面", lambda: self.viewer.set_fit("page"), "Ctrl+1")
        self.a_actual = A("实际大小", lambda: self.viewer.set_zoom(1.0), "Ctrl+0")
        self.a_zoom_in = A("放大", lambda: self.viewer.zoom_by(1.25), ["Ctrl+=", "Ctrl++"])
        self.a_zoom_out = A("缩小", lambda: self.viewer.zoom_by(0.8), "Ctrl+-")

        self.tool_actions = {}
        tg = QActionGroup(self)
        for key, text, tip in (("hand", "浏览", "拖动平移页面；单击选区可选中并调整"),
                               ("rect", "矩形选区", "在页面上拖拽画出矩形选区"),
                               ("poly", "多边形选区", "逐点单击，双击或回车闭合"),
                               ("free", "手绘选区", "按住鼠标拖动画出闭合轮廓"),
                               ("text", "编辑文字", "单击选中文字块（可拖动、Delete 删除），双击编辑"),
                               ("addtext", "添加文字", "在页面任意位置单击添加文字")):
            a = A(text, lambda k=key: self.set_tool(k), tip=tip, checkable=True)
            tg.addAction(a)
            self.tool_actions[key] = a
        self.tool_actions["hand"].setChecked(True)

        self.page_actions = [self.a_copy, self.a_dup, self.a_delete, self.a_rot_l, self.a_rot_r,
                             self.a_rot_180, self.a_swap, self.a_move_to, self.a_replace,
                             self.a_blank, self.a_reverse, self.a_interleave, self.a_export,
                             self.a_sel_all, self.a_sel_inv, self.a_sel_range]

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("文件(&F)")
        for a in (self.a_new, None, self.a_import, self.a_import_at, None, self.a_open_proj,
                  self.a_save_proj, self.a_save_proj_as, None, self.a_export, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("编辑(&E)")
        for a in (self.a_undo, self.a_redo, None, self.a_copy, self.a_paste, self.a_dup,
                  self.a_delete, None, self.a_sel_all, self.a_sel_inv, self.a_sel_range):
            m.addSeparator() if a is None else m.addAction(a)
        self.m_sel_src = m.addMenu("按来源文件选择")
        self.m_sel_src.aboutToShow.connect(self._fill_source_menu)
        m_type = m.addMenu("按页面类型选择")
        for t in pagetype.ALL_TYPES + [pagetype.REGION]:
            m_type.addAction(f"所有{pagetype.LABELS[t]}页", lambda t=t: self.select_by_type(t))
        m.addSeparator()
        m.addAction(self.a_find)

        m = mb.addMenu("页面(&P)")
        for a in (self.a_rot_l, self.a_rot_r, self.a_rot_180, None, self.a_swap, self.a_move_to,
                  self.a_replace, self.a_blank, None, self.a_reverse, self.a_interleave):
            m.addSeparator() if a is None else m.addAction(a)
        m.addSeparator()
        self.m_set_type = m.addMenu("更正页面类型")
        for t in pagetype.ALL_TYPES:
            self.m_set_type.addAction(pagetype.LABELS[t], lambda t=t: self.override_type(t))
        self.m_set_type.addAction("恢复自动识别", lambda: self.override_type(None))

        m = mb.addMenu("视图(&V)")
        for a in (self.a_mode_cont, self.a_mode_single, None, self.a_fit_w, self.a_fit_p,
                  self.a_actual, self.a_zoom_in, self.a_zoom_out):
            m.addSeparator() if a is None else m.addAction(a)
        m.addSeparator()
        ms = m.addMenu("缩略图尺寸")
        g = QActionGroup(self)
        for name in THUMB_SIZES:
            a = ms.addAction(name, lambda n=name: self.set_thumb_size(n))
            a.setCheckable(True)
            a.setChecked(name == "中")
            g.addAction(a)
        m.addSeparator()
        m.addAction(self.left_dock.toggleViewAction())
        m.addAction(self.right_dock.toggleViewAction())

        m = mb.addMenu("文字(&T)")
        m.addAction(self.tool_actions["text"])
        m.addAction(self.tool_actions["addtext"])
        m.addSeparator()
        m.addAction(self.a_find_replace)
        m.addAction(self.a_font_settings)

        m = mb.addMenu("批注(&A)")
        for key, label in AnnotPanel.TOOLS[:-1]:
            m.addAction(label, lambda k=key: self.set_tool(k))
        m.addSeparator()
        m.addAction("批注列表", lambda: (self.right_dock.show(), self._show_tab(self.annot_panel)))
        m.addAction("导出批注摘要…", self._export_annot_summary)

        m = mb.addMenu("扫描(&S)")
        for a in (self.a_ocr, self.a_ocr_cancel, self.a_auto_ocr, None, self.a_preprocess, None,
                  self.a_restore):
            m.addSeparator() if a is None else m.addAction(a)
        m.addAction("擦除选区内容…", lambda: self._erase_regions(
            self.region_panel.uid, self.region_panel.selected_ids()))

        m = mb.addMenu("区域(&R)")
        for k in ("hand", "rect", "poly", "free"):
            m.addAction(self.tool_actions[k])

        m = mb.addMenu("帮助(&H)")
        m.addAction("关于", self._about)

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        for a in (self.a_import, self.a_export, None, self.a_undo, self.a_redo, None,
                  self.a_rot_l, self.a_rot_r, self.a_delete, None, self.a_zoom_out):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setInsertPolicy(QComboBox.NoInsert)
        self.zoom_box.addItems(["适合宽度", "适合页面"] + [f"{z}%" for z in ZOOM_PRESETS])
        self.zoom_box.setMinimumWidth(100)
        self.zoom_box.activated.connect(lambda _: self._zoom_from_box())
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_from_box)
        tb.addWidget(self.zoom_box)
        tb.addAction(self.a_zoom_in)
        tb.addSeparator()
        tb.addWidget(QLabel(" 第 "))
        self.page_spin = QSpinBox(minimum=0, maximum=0)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: v and self.viewer.goto(v - 1))
        tb.addWidget(self.page_spin)
        self.page_total = QLabel(" / 0 页 ")
        tb.addWidget(self.page_total)
        tb.addSeparator()
        for a in self.tool_actions.values():
            tb.addAction(a)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.info_label = QLabel("")
        self.pos_label = QLabel("")
        self.classify_label = QLabel("")
        sb.addWidget(self.info_label, 1)
        sb.addPermanentWidget(self.classify_label)
        from PySide6.QtWidgets import QToolButton
        self.ocr_label = QLabel("")
        self.ocr_cancel = QToolButton(text="取消 OCR")
        self.ocr_cancel.clicked.connect(self.cancel_ocr)
        self.ocr_label.hide()
        self.ocr_cancel.hide()
        sb.addPermanentWidget(self.ocr_label)
        sb.addPermanentWidget(self.ocr_cancel)
        sb.addPermanentWidget(self.pos_label)

    # ================================================================ 状态同步
    def _on_ws_changed(self):
        n = len(self.ws)
        self._syncing = True
        self.thumb_model.refresh()
        existing = {r.uid for r in self.ws.refs}
        self.thumbs.select_uids([u for u in self._sel_uids if u in existing])
        self._syncing = False
        idx = self.ws.index_of(self._cur_uid) if self._cur_uid is not None else -1
        self.viewer.relayout()
        if n:
            if idx < 0:
                idx = min(self.viewer.current, n - 1)
            if idx != self.viewer.current:
                self.viewer.goto(idx)
            self._on_viewer_page(idx)
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1 if n else 0, n)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" / {n} 页 ")
        self._update_title()
        self._update_actions()
        self._update_status()
        self._refresh_annot_list()

    def _update_title(self):
        name = os.path.basename(self.project_path) if self.project_path else "未命名工作区"
        mark = "*" if self.ws.dirty else ""
        self.setWindowTitle(f"{mark}{name} - {APP_NAME}")

    def _update_actions(self):
        has = len(self.ws) > 0
        for a in self.page_actions:
            a.setEnabled(has)
        self.a_undo.setEnabled(self.ws.can_undo())
        self.a_redo.setEnabled(self.ws.can_redo())
        self.a_undo.setText(f"撤销 {self.ws.undo_label()}".strip())
        self.a_redo.setText(f"重做 {self.ws.redo_label()}".strip())
        self.a_paste.setEnabled(bool(self._clipboard))

    def _update_status(self):
        n = len(self.ws)
        if not n:
            self.info_label.setText("工作区为空")
            return
        i = self.viewer.current
        ref = self.ws.refs[i]
        name, opno = origin_of(self.store, ref)
        t = self.thumb_model.page_type(ref)
        w, h = self.service.renderer.page_size(ref.src, ref.pno, ref.rot)
        sel = len(self._sel_uids)
        txt = (f"第 {i + 1} / {n} 页   来源：{name} 第 {opno} 页   "
               f"尺寸：{w * 25.4 / 72:.0f}×{h * 25.4 / 72:.0f} mm")
        if t:
            txt += f"   类型：{pagetype.LABELS.get(t, t)}"
        if sel:
            txt += f"   已选 {sel} 页"
        self.info_label.setText(txt)

    def _on_classify_progress(self, remaining):
        self.classify_label.setText(f"页面类型识别中，剩余 {remaining} 页" if remaining else "")

    def _on_viewer_page(self, i):
        if not (0 <= i < len(self.ws)):
            return
        ref = self.ws.refs[i]
        self._cur_uid = ref.uid
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(i + 1)
        self.page_spin.blockSignals(False)
        if not self._syncing:
            self._syncing = True
            from PySide6.QtCore import QItemSelectionModel
            idx = self.thumb_model.index(i)
            self.thumbs.selectionModel().setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
            self.thumbs.scrollTo(idx)
            self._syncing = False
        self.region_panel.set_page(ref.uid, i + 1)
        self._update_status()

    def _on_thumb_current(self, cur, _prev):
        if self._syncing or not cur.isValid():
            return
        self._syncing = True
        self.viewer.goto(cur.row())
        self._syncing = False

    def _on_thumb_selection(self, *_):
        self._sel_uids = self.thumbs.selected_uids()
        self._update_status()

    def _on_zoom_changed(self, z):
        self.zoom_box.blockSignals(True)
        self.zoom_box.setEditText(f"{z * 100:.0f}%")
        self.zoom_box.blockSignals(False)

    def _zoom_from_box(self):
        t = self.zoom_box.currentText().strip()
        if t == "适合宽度":
            self.viewer.set_fit("width")
        elif t == "适合页面":
            self.viewer.set_fit("page")
        else:
            try:
                v = float(t.rstrip("%").strip())
            except ValueError:
                return
            self.viewer.set_zoom(v / 100.0)

    def set_tool(self, key):
        if key in ("text", "addtext"):
            self._ensure_fonts()
        self.viewer.set_tool(key)
        act = self.tool_actions.get(key)
        if act is not None:
            act.setChecked(True)
        else:
            for a in self.tool_actions.values():
                a.setChecked(False)
        self.annot_panel.set_tool_checked(key)
        if key.startswith("a_"):
            self.annot_panel.apply_tool_default(key)
        if key.startswith("a_"):
            self.right_dock.show()
            self._show_tab(self.annot_panel)
        elif key in ("text", "addtext"):
            self.right_dock.show()
            self._show_tab(self.text_panel)
        elif key != "hand":
            self.right_dock.show()
            self._show_tab(self.region_panel)
        self.viewer.setFocus()

    def set_thumb_size(self, name):
        self.thumb_model.thumb_w = THUMB_SIZES[name]
        self.thumb_model.refresh()
        self.thumbs.select_uids(self._sel_uids)

    # ================================================================ 选择
    def target_uids(self) -> list[int]:
        """选中的页面；未选择时为当前页。"""
        uids = self.thumbs.selected_uids()
        if not uids and len(self.ws):
            uids = [self.ws.refs[self.viewer.current].uid]
        return uids

    def invert_selection(self):
        sel = set(self.thumbs.selected_rows())
        self.thumbs.select_rows([i for i in range(len(self.ws)) if i not in sel])

    def select_range(self):
        n = len(self.ws)
        text, ok = QInputDialog.getText(self, "按页码选择", f"页码范围（共 {n} 页），如 1-5, 8, 12-15：")
        if not ok or not text.strip():
            return
        try:
            rows = parse_range(text, n)
        except ValueError as e:
            QMessageBox.warning(self, "页码有误", str(e))
            return
        self.thumbs.select_rows(rows, rows[0])

    def _fill_source_menu(self):
        self.m_sel_src.clear()
        for s in self.store.sources.values():
            if s.id != GEN_SOURCE_ID:
                self.m_sel_src.addAction(s.name, lambda sid=s.id: self.select_by_source(sid))

    def select_by_source(self, sid):
        rows = [i for i, r in enumerate(self.ws.refs) if origin_source(self.store, r)[0] == sid]
        self.thumbs.select_rows(rows, rows[0] if rows else None)

    def select_by_type(self, t):
        rows = [i for i, r in enumerate(self.ws.refs) if self.thumb_model.page_type(r) == t]
        if not rows:
            self.statusBar().showMessage(f"没有{pagetype.LABELS[t]}页", 3000)
        self.thumbs.select_rows(rows, rows[0] if rows else None)

    # ================================================================ 导入
    def import_dialog(self, at=None):
        last = self.settings.value("last_dir", os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(self, "导入 PDF / 图片", last, FILE_FILTER)
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.import_files(paths, at)

    def import_dialog_at(self):
        n = len(self.ws)
        v = ask_page_number(self, "导入并插入", f"插入到第 N 页之后（0 表示最前，共 {n} 页）：",
                            n, value=n, minimum=0)
        if v is not None:
            self.import_dialog(v)

    def _open_with_password(self, path, sid=None):
        """打开文件；加密时弹出密码框，密码错误提示重试，取消返回 None。"""
        pw = None
        while True:
            try:
                return self.store.open_file(path, pw, sid)
            except PasswordRequired:
                prompt = f"「{os.path.basename(path)}」已加密，请输入打开密码："
                if pw is not None:
                    prompt = "密码错误，请重试。\n" + prompt
                pw, ok = QInputDialog.getText(self, "需要密码", prompt, QLineEdit.Password)
                if not ok:
                    return None

    def import_files(self, paths, at=None):
        ok_exts = IMAGE_EXTS | {".pdf"}
        files = [p for p in paths if os.path.isfile(p) and os.path.splitext(p)[1].lower() in ok_exts]
        skipped = [os.path.basename(p) for p in paths if p not in files]
        errors, repaired, new_refs, sources = [], [], [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for path in files:
                QApplication.restoreOverrideCursor()
                try:
                    src = self._open_with_password(path)
                except ImportError_ as e:
                    errors.append(str(e))
                    continue
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{os.path.basename(path)}：{e}")
                    continue
                finally:
                    QApplication.setOverrideCursor(Qt.WaitCursor)
                if src is None:
                    errors.append(f"{os.path.basename(path)}：已取消（未输入密码）")
                    continue
                if src.repaired:
                    repaired.append(os.path.basename(path))
                sources.append(src)
                new_refs += [self.ws.new_ref(src.id, p) for p in range(src.page_count)]
        finally:
            QApplication.restoreOverrideCursor()
        if new_refs:
            label = f"导入 {len(sources)} 个文件" if len(sources) > 1 else f"导入 {sources[0].name}"
            self.ws.insert(new_refs, at, label)
            self.thumbs.select_uids([r.uid for r in new_refs])
            self.viewer.goto(self.ws.index_of(new_refs[0].uid))
            for s in sources:
                self.service.classify(s.id, range(s.page_count))
            self.bookmarks.populate(self.store)
            self.statusBar().showMessage(f"已导入 {len(new_refs)} 页", 4000)
        msgs = []
        if errors:
            msgs.append("以下文件未能导入：\n" + "\n".join(errors))
        if repaired:
            msgs.append("以下文件结构有损坏，已自动修复：\n" + "\n".join(repaired))
        if skipped:
            msgs.append("不支持的文件已忽略：\n" + "\n".join(skipped))
        if msgs:
            QMessageBox.warning(self, "导入提示", "\n\n".join(msgs))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.import_files(paths)

    # ================================================================ 页面操作
    def _on_pages_dropped(self, uids, row):
        self._sel_uids = uids
        self.ws.move(uids, row)

    def copy_pages(self):
        uids = set(self.target_uids())
        self._clipboard = [r for r in self.ws.refs if r.uid in uids]
        self._update_actions()
        self.statusBar().showMessage(f"已复制 {len(self._clipboard)} 页，可粘贴到任意位置", 3000)

    def paste_pages(self):
        if not self._clipboard:
            return
        rows = self.thumbs.selected_rows()
        at = (rows[-1] + 1) if rows else (self.viewer.current + 1 if len(self.ws) else 0)
        new = self.ws.paste(self._clipboard, at)
        self.thumbs.select_uids([r.uid for r in new])
        self._sel_uids = [r.uid for r in new]

    def duplicate_pages(self):
        new = self.ws.duplicate(self.target_uids())
        self.thumbs.select_uids([r.uid for r in new])
        self._sel_uids = [r.uid for r in new]

    def delete_pages(self):
        uids = self.target_uids()
        if not uids:
            return
        rows = sorted(self.ws.index_of(u) for u in uids)
        self._sel_uids = []
        self.ws.delete(uids)
        if len(self.ws):
            self.viewer.goto(min(rows[0], len(self.ws) - 1))
        self.statusBar().showMessage(f"已删除 {len(uids)} 页（可撤销）", 3000)

    def rotate_pages(self, angle):
        self.ws.rotate(self.target_uids(), angle)

    def swap_pages(self):
        rows = self.thumbs.selected_rows()
        n = len(self.ws)
        if len(rows) == 2:
            a, b = rows
        else:
            text, ok = QInputDialog.getText(self, "交换两页", f"输入要交换的两个页码（共 {n} 页），如 3, 8：")
            if not ok:
                return
            try:
                idx = parse_range(text, n)
            except ValueError as e:
                QMessageBox.warning(self, "页码有误", str(e))
                return
            if len(idx) != 2:
                QMessageBox.warning(self, "页码有误", "请输入恰好两个页码")
                return
            a, b = idx
        self.ws.swap(a, b)

    def move_pages_to(self):
        uids = self.target_uids()
        n = len(self.ws)
        v = ask_page_number(self, "移动到页码", f"把选中的 {len(uids)} 页移动到第几页（1-{n}）：", n)
        if v is not None:
            self._sel_uids = uids
            self.ws.move_to_position(uids, v)

    def replace_with_external(self):
        rows = self.thumbs.selected_rows() or [self.viewer.current]
        if len(rows) != 1:
            QMessageBox.information(self, "替换页面", "请只选中一个要被替换的页面")
            return
        last = self.settings.value("last_dir", "")
        path, _ = QFileDialog.getOpenFileName(self, "选择用于替换的文件", last, FILE_FILTER)
        if not path:
            return
        try:
            src = self._open_with_password(path)
        except ImportError_ as e:
            QMessageBox.warning(self, "无法打开", str(e))
            return
        if src is None:
            return
        pno = 1
        if src.page_count > 1:
            pno = ask_page_number(self, "选择页面", f"使用「{src.name}」的第几页（共 {src.page_count} 页）：",
                                  src.page_count)
            if pno is None:
                return
        new = self.ws.new_ref(src.id, pno - 1)
        self.ws.replace_at(rows[0], new)
        self._sel_uids = [new.uid]
        self.service.classify(src.id, [pno - 1])
        self.bookmarks.populate(self.store)

    def insert_blank(self):
        n = len(self.ws)
        if n:
            ref = self.ws.refs[self.viewer.current]
            w, h = self.service.renderer.page_size(ref.src, ref.pno, ref.rot)
            rows = self.thumbs.selected_rows()
            at = (rows[-1] + 1) if rows else self.viewer.current + 1
        else:
            w, h, at = 595.0, 842.0, 0
        pno = self.store.new_blank_page(w, h)
        new = self.ws.new_ref(GEN_SOURCE_ID, pno, 0, "blank")
        self.ws.insert([new], at, "插入空白页")
        self._sel_uids = [new.uid]
        self.thumbs.select_uids([new.uid])

    def _scope_uids(self):
        uids = self.thumbs.selected_uids()
        return uids if len(uids) >= 2 else None

    def reverse_pages(self):
        self.ws.reverse(self._scope_uids())

    def interleave_pages(self):
        uids = self._scope_uids()
        n = len(uids) if uids else len(self.ws)
        r = QMessageBox.question(
            self, "奇偶页重排",
            f"将{'选中的' if uids else '全部'} {n} 页视为：前半部分为正面，后半部分为背面，交错合并。\n\n"
            "背面是否为倒序扫描（最后一页在前）？",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if r != QMessageBox.Cancel:
            self.ws.interleave(uids, reverse_back=(r == QMessageBox.Yes))

    def override_type(self, t):
        for uid in self.target_uids():
            ref = self.ws.by_uid(uid)
            if ref is None or ref.kind == "region":
                continue
            src = self.store.get(ref.src)
            if t is None:
                src.type_overrides.pop(ref.pno, None)
            else:
                src.type_overrides[ref.pno] = t
        self.thumb_model.refresh()
        self.thumbs.select_uids(self._sel_uids)
        self._update_status()

    def _thumb_menu(self, pos):
        idx = self.thumbs.indexAt(pos)
        if idx.isValid() and idx.row() not in self.thumbs.selected_rows():
            self.thumbs.select_rows([idx.row()], idx.row())
        m = QMenu(self)
        for a in (self.a_copy, self.a_paste, self.a_dup, self.a_delete, None, self.a_rot_l,
                  self.a_rot_r, self.a_rot_180, None, self.a_move_to, self.a_swap, self.a_replace,
                  self.a_blank, None):
            m.addSeparator() if a is None else m.addAction(a)
        m.addMenu(self.m_set_type)
        m.addSeparator()
        m.addAction("导出选中页…", self.export_dialog)
        m.exec(self.thumbs.viewport().mapToGlobal(pos))

    def _jump_to_source_page(self, sid, pno):
        for i, r in enumerate(self.ws.refs):
            if r.src == sid and r.pno == pno:
                self.viewer.goto(i)
                return
        self.statusBar().showMessage("该书签对应的页面已不在工作区中", 3000)

    # ================================================================ 区域选取
    def _on_region_drawn(self, uid, points, is_rect):
        reg = self.regions.add(uid, points, is_rect)
        i = self.ws.index_of(uid)
        self.region_panel.set_page(uid, i + 1)
        self.region_panel.select_region(reg.id)
        self.viewer.select_region(uid, reg.id, scroll=False)

    def _on_viewer_region_selected(self, uid, rid):
        if rid < 0:
            return
        if uid != self.region_panel.uid:
            self.region_panel.set_page(uid, self.ws.index_of(uid) + 1)
        self.region_panel.select_region(rid)

    def _build_region_docs(self, uid, rids, opts):
        ref = self.ws.by_uid(uid)
        if ref is None:
            return None, []
        name, opno = origin_of(self.store, ref)
        opts.source_label = f"{name} 第{opno}页"
        docs = []
        for rid in rids:
            reg = self.regions.find(uid, rid)
            if reg is None:
                continue
            shape = RegionShape(list(reg.points), reg.is_rect)
            docs.append((reg, build_region_page(self.store, ref.src, ref.pno, ref.rot, shape, opts)))
        return ref, docs

    def _generate_regions(self, uid, rids, opts):
        if uid < 0 or not rids:
            QMessageBox.information(self, "生成区域页", "当前页没有选区。请先用矩形 / 多边形 / 手绘工具画出选区。")
            return
        try:
            ref, docs = self._build_region_docs(uid, rids, opts)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "生成失败", str(e))
            return
        if not docs:
            return
        origin = origin_source(self.store, ref)
        new_refs = []
        for _reg, doc in docs:
            pno = self.store.append_generated(doc)[0]
            doc.close()
            self.store.gen_origins[pno] = origin
            self.store.gen.page_types[pno] = pagetype.REGION
            new_refs.append(self.ws.new_ref(GEN_SOURCE_ID, pno, 0, "region"))
        at = self.ws.index_of(uid) + 1
        self._sel_uids = [r.uid for r in new_refs]
        self.ws.insert(new_refs, at, "生成区域页")
        self.thumbs.select_uids(self._sel_uids)
        self.statusBar().showMessage(f"已生成 {len(new_refs)} 个区域页，插入在源页面之后", 4000)

    def _export_regions(self, uid, rids, opts):
        if uid < 0 or not rids:
            QMessageBox.information(self, "导出选区", "当前页没有选区。")
            return
        try:
            ref, docs = self._build_region_docs(uid, rids, opts)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "生成失败", str(e))
            return
        if not docs:
            return
        name, opno = origin_of(self.store, ref)
        default = f"{name}_P{opno}_{docs[0][0].name}" if len(docs) == 1 else f"{name}_P{opno}_选区"
        folder = self._default_dir()
        path, _ = QFileDialog.getSaveFileName(self, "导出选区为 PDF", os.path.join(folder, default + ".pdf"),
                                              "PDF 文件 (*.pdf)")
        if not path:
            for _, d in docs:
                d.close()
            return
        import pymupdf as fitz
        with ENGINE_LOCK:
            out = fitz.open()
            for _, d in docs:
                out.insert_pdf(d)
                d.close()
        try:
            save_document(out, path, SaveOptions())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "导出失败", str(e))
            return
        finally:
            out.close()
        show_export_done(self, [path], [], os.path.dirname(path))

    def _apply_region_template(self, uid, rids):
        targets = [u for u in self.thumbs.selected_uids() if u != uid]
        if uid < 0 or not self.regions.get(uid):
            QMessageBox.information(self, "选区模板", "当前页没有选区。")
            return
        if not targets:
            QMessageBox.information(self, "选区模板", "请先在左侧缩略图中选中要应用选区的目标页面（可多选）。")
            return
        n = self.regions.apply_template(uid, targets, set(rids))
        self.statusBar().showMessage(f"已将选区复制到 {len(targets)} 个页面（共 {n} 个选区）", 4000)

    # ================================================================ 文字编辑
    def _setup_text(self):
        self.fonts = FontManager(os.path.join(project.app_data_dir(), "fontcache.json"))
        self.fonts.substitute = self.settings.value("font_substitute", "") or ""
        self.fonts.default_cjk = self.settings.value("font_default", "SimSun") or "SimSun"
        self.texts = TextService(self.store, self.ws, self.fonts)
        self._fonts_ready = False
        self._garbled_ok: set = set()
        self._hit_refs: dict = {}
        self._last_search = None
        v = self.viewer
        v.text_blocks = self.texts.blocks
        v.page_type_of = self.thumb_model.page_type
        v.textBlockSelected.connect(self._on_text_selected)
        v.textEditRequested.connect(self._on_text_edit_requested)
        v.textAddRequested.connect(self._on_text_add_requested)
        v.textCommitted.connect(self._on_text_committed)
        v.textBlockMoved.connect(self._on_text_moved)
        v.textDeleteRequested.connect(self._on_text_delete)
        v.outlineClicked.connect(self._on_outline_clicked)

    def _ensure_fonts(self):
        if not self._fonts_ready:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.fonts.ensure_loaded()
                self.text_panel.set_families(self.fonts.families())
            finally:
                QApplication.restoreOverrideCursor()
            self._fonts_ready = True

    def _ensure_editable(self, uid) -> bool:
        """权限密码检查（7.4）与乱码页提醒。"""
        ref = self.ws.by_uid(uid)
        if ref is None:
            return False
        try:
            self.texts.check_editable(ref)
        except EditLocked as e:
            name = self.store.get(e.sid).name
            prompt = f"「{name}」设置了编辑权限密码，请输入权限密码后才可编辑："
            while True:
                pw, ok = QInputDialog.getText(self, "需要权限密码", prompt, QLineEdit.Password)
                if not ok:
                    return False
                if self.texts.unlock(e.sid, pw):
                    break
                prompt = "权限密码错误，请重试：\n" + prompt
        if self.texts.page_type(ref) == pagetype.GARBLED and (ref.src, ref.pno) not in self._garbled_ok:
            r = QMessageBox.question(
                self, "乱码文字页",
                "该页文字缺少字符映射，复制、搜索得到的可能是乱码，直接编辑可能出错。\n"
                "建议使用「OCR 重建文字层」（阶段三提供）。\n\n仍要直接编辑吗？")
            if r != QMessageBox.Yes:
                return False
            self._garbled_ok.add((ref.src, ref.pno))
        return True

    def _block(self, uid, bid):
        ref = self.ws.by_uid(uid)
        if ref is None:
            return None
        blocks = self.texts.blocks(ref)
        return blocks[bid] if 0 <= bid < len(blocks) else None

    def _font_display(self, pdf_font: str) -> str:
        e = self.fonts.find(pdf_font) if pdf_font else None
        base = pdf_font.split("+", 1)[-1]
        return f"{base}（系统有同名字体：{e.display}）" if e else f"{base}（系统无同名字体）"

    def _editor_family(self, style, pdf_font: str = "") -> str:
        name = style.font or pdf_font
        e = self.fonts.find(name) if name else None
        return e.family if e else (self.fonts.default_cjk or "SimSun")

    def _on_text_selected(self, uid, bid):
        if bid < 0:
            self.text_panel.load_block(None)
            return
        self._ensure_fonts()
        b = self._block(uid, bid)
        ref = self.ws.by_uid(uid)
        if b is not None and ref is not None and self.texts.is_scan_block(ref, b):
            self.text_panel.load_block(b, "扫描图像（OCR 识别行）")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.text_panel.load_scan_style(self._scan_style(ref, b))
            finally:
                QApplication.restoreOverrideCursor()
            return
        self.text_panel.scan_info.setText("")
        self.text_panel.load_block(b, self._font_display(b.font) if b else "")

    def _on_text_edit_requested(self, uid, bid, cursor):
        if not self._ensure_editable(uid):
            return
        b = self._block(uid, bid)
        if b is None:
            return
        st = self.text_panel.style()
        self.viewer.open_editor(uid, bid, b.text, self._editor_family(st, b.font), st.size, st.color, cursor)

    def _on_text_add_requested(self, uid, pt):
        if not self._ensure_editable(uid):
            return
        self._ensure_fonts()
        st = self.text_panel.style()
        self.viewer.open_editor(uid, -1, "", self._editor_family(st), st.size, st.color, origin=pt)

    def _run_text_op(self, fn, *args, **kw):
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                res = fn(*args, **kw)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "文字编辑失败", f"{type(e).__name__}: {e}")
            return None
        self.viewer.sel_block = None
        if res is not None and hasattr(res, "substituted"):
            self.text_panel.show_result(res.font_name, res.substituted)
            if res.substituted:
                self.statusBar().showMessage(f"已使用替代字体：{res.font_name}", 6000)
        return res

    def _on_text_committed(self, uid, bid, text, origin):
        self._ensure_fonts()
        st = self.text_panel.style()
        ref = self.ws.by_uid(uid)
        if bid >= 0 and ref is not None and self._block(uid, bid) is not None \
                and self.texts.is_scan_block(ref, self._block(uid, bid)):
            b = self._block(uid, bid)
            if text == b.text:
                return
            res = self._run_text_op(self.texts.edit, uid, bid, text, st,
                                    scan_opts=self.text_panel.scan_options())
            self._scan_result(res)
            return
        if bid >= 0:
            b = self._block(uid, bid)
            if b is None:
                return
            orig = b.style()
            unchanged = (text == b.text and st.font is None and abs(st.size - orig.size) < 0.05
                         and st.color == orig.color and st.bold == orig.bold
                         and st.char_spacing == 0 and st.line_spacing == 1.0)
            if unchanged:
                return
            if not text.strip():
                self._run_text_op(self.texts.delete, uid, bid)
                return
            self._run_text_op(self.texts.edit, uid, bid, text, st)
        else:
            if not text.strip():
                return
            if st.font is None:
                st.font = self.fonts.default_cjk
            self._run_text_op(self.texts.add, uid, origin, text, st)

    def _on_text_moved(self, uid, bid, dx, dy):
        if not self._ensure_editable(uid):
            return
        self._ensure_fonts()
        b = self._block(uid, bid)
        if b is not None:
            st = b.style()
            st.reflow = False
            scan_opts = None
            ref = self.ws.by_uid(uid)
            if ref is not None and self.texts.is_scan_block(ref, b):
                scan_opts = self.text_panel.scan_options()
                scan_opts.force = True
                scan_opts.proofread_only = False
            self._run_text_op(self.texts.edit, uid, bid, b.text, st, (dx, dy), "移动文字", scan_opts=scan_opts)

    def _on_text_delete(self, uid, bid):
        if self._ensure_editable(uid):
            self._run_text_op(self.texts.delete, uid, bid)
            self.statusBar().showMessage("已删除文字块（从内容流中移除，可撤销）", 4000)

    def _apply_text_style(self):
        sel = self.viewer.sel_block
        if not sel:
            QMessageBox.information(self, "文字样式", "请先用「编辑文字」工具单击选中一个文字块。")
            return
        uid, bid = sel
        if not self._ensure_editable(uid):
            return
        self._ensure_fonts()
        b = self._block(uid, bid)
        if b is not None:
            scan_opts = None
            ref = self.ws.by_uid(uid)
            if ref is not None and self.texts.is_scan_block(ref, b):
                scan_opts = self.text_panel.scan_options()
                scan_opts.force = True
                scan_opts.proofread_only = False
            res = self._run_text_op(self.texts.edit, uid, bid, b.text, self.text_panel.style(),
                                    label="修改文字样式", scan_opts=scan_opts)
            if scan_opts:
                self._scan_result(res)

    def _delete_selected_text(self):
        if self.viewer.sel_block:
            self._on_text_delete(*self.viewer.sel_block)

    def _scan_result(self, res):
        if res is None:
            return
        how = {"fill": "背景色填充", "inpaint": "图像修复"}
        methods = "、".join(sorted({how.get(m, m) for m in res.erase_methods})) or "—"
        msg = f"已擦除原字（{methods}）并重写"
        if res.font_name:
            msg += f"，字体：{res.font_name}"
        self.text_panel.result.setText(msg + "\n（近看可分辨修改痕迹，可撤销或「恢复原始页面」）")
        self.text_panel.result.setStyleSheet("color:#188038")
        self.statusBar().showMessage(msg, 6000)

    def _on_outline_clicked(self, uid):
        ref = self.ws.by_uid(uid)
        if ref is not None and self.texts.is_scan(ref) and not self.texts.has_ocr(ref):
            r = QMessageBox.question(self, "扫描页", "该扫描页还没有识别文字，现在进行 OCR 识别吗？（约 3~5 秒）\n"
                                     "识别后即可单击选中文字行进行修改。")
            if r == QMessageBox.Yes and self._ensure_editable(uid):
                self._ocr_now(ref)
            return
        if ref is not None and self.texts.is_scan(ref):
            return
        t = self.texts.page_type(ref) if ref else None
        if t == pagetype.OUTLINE:
            msg = ("此处为图形，非文字，无法直接编辑。\n\n"
                   "这类页面（如 CAD 用 SHX 字体导出、设计软件“文字转轮廓”）中的文字实际是线条。\n"
                   "可在右键菜单「更正页面类型」中把它改为扫描页，然后 OCR 识别、擦除原字并原位重写。")
        else:
            msg = "这是扫描图片页，没有可编辑的文字对象。\n请先使用「扫描 → OCR 文字识别」。"
        QMessageBox.information(self, "无法直接编辑", msg)

    def font_settings(self):
        self._ensure_fonts()
        dlg = QDialog(self)
        dlg.setWindowTitle("字体设置")
        form = QFormLayout(dlg)
        fams = self.fonts.families()
        sub, dft = QComboBox(), QComboBox()
        sub.addItem("（不设置）", "")
        for e in fams:
            sub.addItem(e.display, e.family)
            dft.addItem(e.display, e.family)
        sub.setCurrentIndex(max(0, sub.findData(self.fonts.substitute)))
        dft.setCurrentIndex(max(0, dft.findData(self.fonts.default_cjk)))
        note = QLabel("修改文字时字体选择顺序（技术要求 7.3）：\n"
                      "① 原嵌入字体（能覆盖新输入的全部字符时）\n② 系统同名字体\n"
                      "③ 下面配置的替代字体\n④ 默认中文字体\n使用 ③④ 时界面会提示“已使用替代字体”。")
        note.setStyleSheet("color:#5f6368")
        form.addRow(note)
        form.addRow("替代字体", sub)
        form.addRow("默认中文字体", dft)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            self.fonts.substitute = sub.currentData()
            self.fonts.default_cjk = dft.currentData()
            self.settings.setValue("font_substitute", self.fonts.substitute)
            self.settings.setValue("font_default", self.fonts.default_cjk)

    # ================================================================ 搜索 / 替换
    def show_search(self, replace=False):
        self.right_dock.show()
        self._show_tab(self.search_panel)
        self.search_panel.focus_input(replace)

    def _search_items(self, scope):
        refs = self.ws.refs
        if scope == "current":
            return [(self.viewer.current, refs[self.viewer.current])] if refs else []
        if scope == "selected":
            return [(i, refs[i]) for i in self.thumbs.selected_rows()]
        return list(enumerate(refs))

    def _search(self, text, case, whole, regex, scope):
        if not text.strip():
            return
        if regex:
            import re
            try:
                re.compile(text)
            except re.error as e:
                QMessageBox.warning(self, "正则表达式有误", str(e))
                return
        items = self._search_items(scope)

        def job(progress):
            done = [0]

            def cancelled():
                done[0] += 1
                return not progress(done[0], len(items), "")
            return self.texts.find(items, text, case, whole, regex, cancelled=cancelled)

        hits = run_task(self, "正在搜索", job)
        self._hits = hits or []
        self._hit_refs = {h.uid: self.ws.by_uid(h.uid) for h in self._hits}
        self._last_search = (text, case, whole, regex, scope)
        hl = {}
        for h in self._hits:
            hl.setdefault(h.uid, []).append(h.rect)
        self.viewer.highlights = hl
        self.viewer.active_highlight = None
        self.search_panel.show_hits(self._hits)
        self.viewer.viewport().update()

    def _on_hit(self, row):
        if 0 <= row < len(self._hits):
            h = self._hits[row]
            i = self.ws.index_of(h.uid)
            if i >= 0:
                self.viewer.active_highlight = (h.uid, h.rect)
                self.viewer.goto(i, h.rect)

    def _replace_scan_opts(self):
        from ..core.scanedit import ScanEditOptions
        p = self.text_panel
        return ScanEditOptions(mode=p.scan_mode.currentData(), texture=p.scan_texture.isChecked(),
                               proofread_only=bool(p.scan_how.currentData()))

    def _replace(self, mode, replacement):
        if not self._hits or not self._last_search:
            QMessageBox.information(self, "替换", "请先搜索。")
            return
        rows = [self.search_panel.list.currentRow()] if mode == "current" else self.search_panel.checked_rows()
        hits = [self._hits[r] for r in rows if 0 <= r < len(self._hits)]
        if not hits:
            QMessageBox.information(self, "替换", "没有选中要替换的项。")
            return
        stale = [h for h in hits if self.ws.by_uid(h.uid) != self._hit_refs.get(h.uid)]
        if stale:
            QMessageBox.information(self, "替换", "页面在搜索后已被修改，请重新搜索后再替换。")
            return
        for uid in {h.uid for h in hits}:
            if not self._ensure_editable(uid):
                return
        text, case, whole, regex, scope = self._last_search
        pages = len({h.uid for h in hits})
        if mode == "checked":
            r = QMessageBox.question(self, "全部替换",
                                     f"将把 {len(hits)} 处“{text}”替换为“{replacement}”（涉及 {pages} 页）。\n"
                                     "受影响的行会整行按原字号、颜色重写。可撤销。继续吗？")
            if r != QMessageBox.Yes:
                return
        self._ensure_fonts()
        try:
            total, subs, changes = run_task(
                self, "正在替换",
                lambda progress: self.texts.replace(hits, text, replacement, case, whole, regex,
                                                    progress=progress, commit=False,
                                                    scan_opts=self._replace_scan_opts()))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "替换失败", f"{type(e).__name__}: {e}")
            return
        if changes:
            self.ws.replace_many(changes, f"替换 {total} 处")
        msg = f"已替换 {total} 处（{len(changes)} 页），可撤销"
        if subs:
            msg += "；已使用替代字体：" + "、".join(subs)
        self.statusBar().showMessage(msg, 8000)
        self._search(text, case, whole, regex, scope)

    # ================================================================ 扫描件
    def _setup_scan(self):
        self.scans = ScanService(self.store, self.ws, self.texts)
        self.ocr = OcrWorker(self.scans)
        self.ocr.pageDone.connect(self._on_ocr_done)
        self.ocr.pageFailed.connect(lambda s, p, e: self._ocr_errors.append(f"第 {p + 1} 页：{e}"))
        self.ocr.progress.connect(self._on_ocr_progress)
        self.ocr.idle.connect(self._on_ocr_idle)
        self._ocr_errors: list[str] = []
        self._style_cache: dict = {}
        self.service.typeReady.connect(self._auto_ocr)

    def _scan_keys(self, scope: str, skip_done: bool) -> list:
        refs = self.ws.refs
        if scope == "current":
            items = [refs[self.viewer.current]] if refs else []
        elif scope == "selected":
            rows = self.thumbs.selected_rows()
            items = [refs[i] for i in rows]
        elif scope == "scan":
            items = [r for r in refs if self.texts.is_scan(r)]
        else:
            items = list(refs)
        keys, seen = [], set()
        for r in items:
            k = (r.src, r.pno)
            if k in seen or (skip_done and self.texts.has_ocr(r)):
                continue
            seen.add(k)
            keys.append(k)
        return keys

    def ocr_dialog(self):
        if not len(self.ws):
            return
        n_scan = sum(1 for r in self.ws.refs if self.texts.is_scan(r))
        dlg = OcrDialog(self, len(self.thumbs.selected_rows()), n_scan, len(self.ws))
        if not dlg.exec():
            return
        keys = self._scan_keys(dlg.scope(), dlg.skip.isChecked())
        if not keys:
            QMessageBox.information(self, "OCR", "所选范围内没有需要识别的页面。")
            return
        self._ocr_errors.clear()
        n = self.ocr.enqueue(keys)
        self.statusBar().showMessage(f"已加入 OCR 队列：{n} 页（后台识别，可继续操作）", 5000)

    def _auto_ocr(self, sid, pno, t):
        if t == pagetype.SCAN and self.a_auto_ocr.isChecked() and not self.store.has_ocr(sid, pno):
            if any(r.src == sid and r.pno == pno for r in self.ws.refs):
                self.ocr.enqueue([(sid, pno)])

    def _on_ocr_done(self, src, pno, new):
        self.scans.commit_ocr(src, pno, new)

    def _on_ocr_progress(self, done, total):
        busy = total > 0 and done < total
        self.ocr_label.setVisible(busy)
        self.ocr_cancel.setVisible(busy)
        if busy:
            self.ocr_label.setText(f"OCR 识别中：{done} / {total} 页")

    def _on_ocr_idle(self):
        self.ocr_label.setVisible(False)
        self.ocr_cancel.setVisible(False)
        if self._ocr_errors:
            QMessageBox.warning(self, "OCR", "以下页面识别失败：\n" + "\n".join(self._ocr_errors[:20]))
            self._ocr_errors.clear()
        else:
            self.statusBar().showMessage("OCR 识别完成：扫描页现在可以搜索、复制，也可以用「编辑文字」修改", 6000)

    def cancel_ocr(self):
        self.ocr.cancel()
        self.statusBar().showMessage("已取消 OCR（已完成的页面保留识别结果）", 4000)

    def _ocr_now(self, ref) -> bool:
        """前台立即识别一页（编辑扫描页文字前）。"""
        try:
            new = run_task(self, "正在识别文字", lambda progress: self.scans.ocr_key(ref.src, ref.pno),
                           cancellable=False)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "OCR 失败", f"{type(e).__name__}: {e}")
            return False
        if new is not None:
            self.scans.commit_ocr(ref.src, ref.pno, new)
        return True

    def preprocess_dialog(self):
        if not len(self.ws):
            return
        ref = self.ws.refs[self.viewer.current]
        n_scan = sum(1 for r in self.ws.refs if self.texts.is_scan(r))
        dlg = PreprocessDialog(self, lambda o: self.scans.preprocess_preview(ref, o),
                               len(self.thumbs.selected_rows()), n_scan)
        if not dlg.exec():
            return
        scope = dlg.scope.currentData()
        if scope == "current":
            refs = [ref]
        elif scope == "selected":
            refs = [self.ws.refs[i] for i in self.thumbs.selected_rows()]
        else:
            refs = [r for r in self.ws.refs if self.texts.is_scan(r)]
        for r in refs:
            if not self._ensure_editable(r.uid):
                return
        opts = dlg.options()
        try:
            changes, skipped, angles = run_task(
                self, "正在预处理", lambda progress: self.scans.preprocess_pages(refs, opts, progress))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "预处理失败", f"{type(e).__name__}: {e}")
            return
        if changes:
            self.ws.replace_many(changes, f"图像预处理（{len(changes)} 页）")
        msg = f"已处理 {len(changes)} 页"
        if angles and opts.deskew:
            msg += f"，纠偏角度 {min(angles):+.1f}° ~ {max(angles):+.1f}°"
        if skipped:
            msg += f"；第 {', '.join(map(str, skipped[:10]))} 页没有扫描底图，已跳过"
        self.statusBar().showMessage(msg, 8000)
        if changes and dlg.reocr.isChecked():
            self.ocr.enqueue([(r.src, r.pno) for r in changes.values()])

    def _erase_regions(self, uid, rids):
        if uid < 0 or not rids:
            QMessageBox.information(self, "擦除选区", "请先在扫描页上画出选区（矩形 / 多边形 / 手绘）。")
            return
        ref = self.ws.by_uid(uid)
        if ref is None or not self._ensure_editable(uid):
            return
        import pymupdf as fitz
        rects = [fitz.Rect(*self.regions.find(uid, rid).bbox()) for rid in rids if self.regions.find(uid, rid)]
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                for r in rects:
                    self.scans.erase(uid, r)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "擦除失败", f"{type(e).__name__}: {e}")
            return
        for rid in rids:
            self.regions.remove(uid, rid)
        self.statusBar().showMessage(f"已擦除 {len(rects)} 个选区（可撤销）", 4000)

    def restore_original(self):
        n = self.scans.restore_original(self.target_uids())
        self.statusBar().showMessage(f"已恢复 {n} 页为原始页面（可撤销）" if n else "所选页面没有修改过", 4000)

    def _scan_style(self, ref, block):
        key = (ref.src, ref.pno, block.id)
        if key not in self._style_cache:
            import pymupdf as fitz
            from ..core.scan import load_page_image
            from ..core.scanedit import estimate_style
            with ENGINE_LOCK:
                pi = load_page_image(self.store.page(ref.src, ref.pno))
            st = estimate_style(pi, fitz.Rect(block.bbox), block.text, self.fonts) if pi else None
            self._style_cache[key] = st
        return self._style_cache[key]

    # ================================================================ 批注
    def _setup_annots(self):
        self.annots = AnnotService(self.store, self.ws, self.texts)
        self._stamp_cache: dict = {}
        v = self.viewer
        v.annot_list = self.annots.annots
        v.annotMarkup.connect(self._on_annot_markup)
        v.annotNoteRequested.connect(self._on_annot_note)
        v.annotFreeTextRequested.connect(self._on_annot_freetext)
        v.annotShape.connect(self._on_annot_shape)
        v.annotInk.connect(self._on_annot_ink)
        v.annotStampRequested.connect(self._on_annot_stamp)
        v.annotSelected.connect(self._on_annot_selected)
        v.annotMoved.connect(self._on_annot_moved)
        v.annotDeleteRequested.connect(self._on_annot_delete)
        v.annotEditRequested.connect(self._on_annot_edit)

    def _annot_op(self, uid, fn, *args, **kw):
        if not self._ensure_editable(uid):
            return None
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                return fn(uid, *args, **kw)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "批注操作失败", f"{type(e).__name__}: {e}")
            return None

    def _find_annot(self, uid, aid):
        ref = self.ws.by_uid(uid)
        if ref is None:
            return None
        return next((a for a in self.annots.annots(ref) if a.id == aid), None)

    def _on_annot_markup(self, uid, kind, quads, text):
        self._annot_op(uid, self.annots.add_markup, kind, quads, self.annot_panel.style(), text)

    def _on_annot_note(self, uid, pt):
        text, ok = QInputDialog.getMultiLineText(self, "便签", "便签内容：")
        if ok:
            self._annot_op(uid, self.annots.add_note, pt, text, self.annot_panel.style())

    def _on_annot_freetext(self, uid, rect):
        text, ok = QInputDialog.getMultiLineText(self, "文本框", "文本框内容：")
        if ok and text.strip():
            self._annot_op(uid, self.annots.add_freetext, rect, text, self.annot_panel.style())

    def _on_annot_shape(self, uid, kind, pts):
        self._annot_op(uid, self.annots.add_shape, kind, pts, self.annot_panel.style())

    def _on_annot_ink(self, uid, strokes, pressure):
        st = self.annot_panel.style()
        if pressure >= 0:                       # 数位板：按平均压感调整线宽
            st.width = max(0.3, st.width * (0.4 + 1.2 * pressure))
        self._annot_op(uid, self.annots.add_ink, strokes, st)

    def _stamp_image(self) -> tuple[bytes, str] | None:
        p = self.annot_panel
        key = p.stamp.currentData()
        if key == "__image__":
            if not p.stamp_image or not os.path.exists(p.stamp_image):
                QMessageBox.information(self, "图章", "请先在「图章」中选择一张图片。")
                return None
            with open(p.stamp_image, "rb") as f:
                return f.read(), os.path.splitext(os.path.basename(p.stamp_image))[0]
        if key not in self._stamp_cache:
            self._ensure_fonts()
            e = self.fonts.find("SimHei") or self.fonts.find(self.fonts.default_cjk)
            self._stamp_cache[key] = A.make_stamp_png(key, A.PRESET_STAMPS[key], e.path if e else None)
        return self._stamp_cache[key], key

    def _on_annot_stamp(self, uid, pt):
        img = self._stamp_image()
        if img is None:
            return
        data, name = img
        try:
            w, h = A.image_size(data)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "图章", f"无法读取图片：{e}")
            return
        rect = A.stamp_rect_at(pt, w, h, self.annot_panel.stamp_width.value())
        self._annot_op(uid, self.annots.add_stamp, rect, data, self.annot_panel.style(), name)

    def _on_annot_selected(self, uid, aid):
        info = self._find_annot(uid, aid) if aid else None
        i = self.ws.index_of(uid)
        self.annot_panel.show_selected(info, i + 1 if info else None)

    def _on_annot_moved(self, uid, aid, dx, dy):
        info = self._find_annot(uid, aid)
        if info is not None and info.type in ("Highlight", "Underline", "StrikeOut", "Squiggly"):
            self.statusBar().showMessage("文字标记批注随文字位置固定，不能移动", 4000)
            return
        self._annot_op(uid, self.annots.move, aid, dx, dy)

    def _on_annot_delete(self, uid, aid):
        if self._annot_op(uid, self.annots.delete, aid):
            self.viewer.select_annot(uid, None)
            self.annot_panel.show_selected(None)
            self.statusBar().showMessage("已删除批注（可撤销）", 3000)

    def _on_annot_edit(self, uid, aid):
        info = self._find_annot(uid, aid)
        if info is None:
            return
        text, ok = QInputDialog.getMultiLineText(self, "修改批注", f"{info.label}的内容：", info.content)
        if ok:
            self._annot_op(uid, self.annots.update, aid, content=text)
            self._on_annot_selected(uid, aid)

    def _apply_annot_style(self):
        sel = self.viewer.sel_annot
        if not sel:
            QMessageBox.information(self, "批注", "请先用「选择批注」工具单击一个批注。")
            return
        uid, aid = sel
        info = self._find_annot(uid, aid)
        if info is None:
            return
        st = self.annot_panel.style()
        kw = dict(color=st.color, opacity=st.opacity, width=st.width,
                  content=self.annot_panel.content.toPlainText())
        if info.type in ("Square", "Circle", "Polygon", "FreeText"):
            kw["fill"] = st.fill
        if info.type == "FreeText":
            kw["fontsize"] = st.fontsize
        self._annot_op(uid, self.annots.update, aid, **kw)
        self._on_annot_selected(uid, aid)

    def _delete_selected_annot(self):
        if self.viewer.sel_annot:
            self._on_annot_delete(*self.viewer.sel_annot)

    def _annot_rows(self):
        rows = self.annots.all_annots()
        only_cur, author, typ = self.annot_panel.filters()
        cur = self.viewer.current
        out = [(i, r, a) for i, r, a in rows
               if (not only_cur or i == cur) and (author is None or (a.author or "（无）") == author)
               and (typ is None or a.label == typ)]
        authors = sorted({a.author or "（无）" for _i, _r, a in rows})
        types = sorted({a.label for _i, _r, a in rows})
        return out, authors, types

    def _refresh_annot_list(self, *_):
        if not self.right_dock.isVisible() or self._current_panel() is not self.annot_panel:
            return
        rows, authors, types = self._annot_rows()
        self.annot_panel.set_rows(rows, authors, types)

    def _jump_annot(self, uid, aid):
        i = self.ws.index_of(uid)
        info = self._find_annot(uid, aid)
        if i < 0 or info is None:
            return
        self.set_tool("a_annot")
        self.viewer.goto(i, info.rect)
        self.viewer.select_annot(uid, aid)
        self._on_annot_selected(uid, aid)

    def _export_annot_summary(self):
        rows, _a, _t = self._annot_rows()
        if not rows:
            QMessageBox.information(self, "批注摘要", "没有批注。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出批注摘要", os.path.join(self._default_dir(), "批注摘要.txt"),
                                              "文本文件 (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(self.annots.summary(rows))
        show_export_done(self, [path], [], os.path.dirname(path))

    def _redact_regions(self, uid, rids):
        if uid < 0 or not rids:
            QMessageBox.information(self, "涂黑", "请先在页面上画出要涂黑的选区（矩形 / 多边形 / 手绘）。")
            return
        r = QMessageBox.question(self, "涂黑", f"将彻底删除 {len(rids)} 个选区内的文字、图片像素与图形，并以黑块覆盖。\n"
                                 "在本程序中可撤销，但导出后无法恢复。继续吗？")
        if r != QMessageBox.Yes:
            return
        import pymupdf as fitz
        rects = [fitz.Rect(*self.regions.find(uid, rid).bbox()) for rid in rids if self.regions.find(uid, rid)]
        if self._annot_op(uid, self.annots.redact, rects):
            for rid in rids:
                self.regions.remove(uid, rid)
            self.statusBar().showMessage("已涂黑。导出时建议勾选「清理隐私信息」", 6000)

    # ================================================================ 导出
    def _default_dir(self):
        for s in self.store.sources.values():
            if s.path:
                return os.path.dirname(s.path)
        return self.settings.value("last_dir", os.path.expanduser("~"))

    def export_dialog(self):
        n = len(self.ws)
        if not n:
            return
        sel_rows = self.thumbs.selected_rows()
        first = next((s for s in self.store.sources.values() if s.path), None)
        default_name = (first.name + "_合并") if first else "导出"
        dlg = ExportDialog(self, bool(sel_rows), n, len(sel_rows), self._default_dir(), default_name,
                           self.annots.has_redactions(self.ws.refs))
        if not dlg.exec():
            return
        rows = sel_rows if dlg.use_selection() else list(range(n))
        job = ExportJob(
            mode=dlg.mode(), refs=[self.ws.refs[i] for i in rows], positions=[i + 1 for i in rows],
            target=dlg.target, template=dlg.template.text(), conflict=dlg.conflict.currentData(),
            chunk_size=dlg.chunk.value(), image_format=dlg.img_fmt.currentText(),
            image_dpi=dlg.img_dpi.value(), save=dlg.save_options())
        try:
            res = run_task(self, "正在导出", lambda progress: run_export(self.store, job, progress))
        except ExportCancelled:
            self.statusBar().showMessage("导出已取消", 3000)
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"{type(e).__name__}: {e}")
            return
        if res.files or res.skipped:
            show_export_done(self, res.files or [res.folder], res.skipped, res.folder)

    # ================================================================ 项目 / 自动保存
    def _confirm_discard(self) -> bool:
        if not self.ws.dirty or not len(self.ws):
            return True
        r = QMessageBox.question(self, APP_NAME, "工作区有未保存的更改，是否保存为项目文件？",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    def _reset_document(self):
        self.service.set_tile_jobs([])
        self.service.cancel_images(set())
        with self.service._cv:
            self.service._classify.clear()
        with ENGINE_LOCK:
            self.store.close_all()
            self.store.__init__()
            self.service.renderer._dls.clear()
            self.service.renderer._geo.clear()
        self.thumb_model._cache.clear()
        self.viewer.drop_caches()
        self.viewer.highlights = {}
        self.viewer.close_editor(False)
        self.viewer.sel_block = None
        self.texts.clear_cache()
        self.annots.clear_cache()
        self.viewer.sel_annot = None
        self.ocr.cancel()
        self._style_cache.clear()
        self._hits = []
        self._garbled_ok.clear()
        self.regions.by_uid.clear()
        self._clipboard = []
        self._sel_uids = []
        self._cur_uid = None
        self.project_path = None
        self.ws.reset()
        self.bookmarks.populate(self.store)

    def new_workspace(self):
        if self._confirm_discard():
            self._reset_document()

    def save_project(self, save_as=False) -> bool:
        path = self.project_path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, "保存项目", os.path.join(self._default_dir(), "工作区"
                                                  + project.PROJECT_EXT), f"项目文件 (*{project.PROJECT_EXT})")
            if not path:
                return False
        try:
            project.save_project(path, self.store, self.ws, self.regions)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(e))
            return False
        self.project_path = path
        self.ws.dirty = False
        self._update_title()
        self.statusBar().showMessage(f"项目已保存：{path}", 4000)
        return True

    def open_project(self, path=None):
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "打开项目", self._default_dir(),
                                                  f"项目文件 (*{project.PROJECT_EXT})")
            if not path:
                return
        self._load_project(path)
        self.project_path = path
        self._update_title()

    def _load_project(self, path):
        self._reset_document()
        try:
            problems = project.load_project(path, self.store, self.ws, self.regions, self._open_with_password)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "打开项目失败", str(e))
            return False
        for s in self.store.sources.values():
            self.service.classify(s.id, range(s.page_count))
        self.bookmarks.populate(self.store)
        if problems:
            QMessageBox.warning(self, "项目恢复提示", "\n".join(problems))
        return True

    def _autosave(self):
        if not len(self.ws) or not self.ws.dirty:
            return
        try:
            dirty = self.ws.dirty
            project.save_project(project.autosave_path(), self.store, self.ws, self.regions)
            self.ws.dirty = dirty
        except Exception as e:  # noqa: BLE001
            print(f"[autosave] 失败：{e}")

    def _check_recovery(self):
        flag, auto = project.running_flag_path(), project.autosave_path()
        crashed = os.path.exists(flag)
        with open(flag, "w") as f:
            f.write(str(os.getpid()))
        if crashed and os.path.exists(auto):
            r = QMessageBox.question(self, "恢复工作区", "程序上次未正常退出，是否恢复自动保存的工作区？")
            if r == QMessageBox.Yes:
                if self._load_project(auto):
                    self.ws.dirty = True
                    self._update_title()

    def _about(self):
        QMessageBox.about(self, "关于", f"<b>{APP_NAME}</b><br>阶段一：页面管理、合并拆分导出、区域选取导出。<br>"
                                      "全部处理在本地完成，不上传任何文件。<br><br>"
                                      "基于 PySide6（LGPL）与 PyMuPDF（AGPL，仅限内部使用）。")

    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.service.stop()
        self.ocr.stop()
        for p in (project.running_flag_path(), project.autosave_path()):
            try:
                os.remove(p)
            except OSError:
                pass
        e.accept()
