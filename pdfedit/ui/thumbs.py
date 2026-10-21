"""缩略图面板：拖拽排序（多页一起拖、插入指示线）、来源色条、页面类型角标、懒加载渲染。"""
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import (QAbstractListModel, QByteArray, QMimeData, QModelIndex, QPoint,
                            QRect, QSize, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from ..app.exporter import origin_source
from ..app.workspace import Workspace
from ..core import pagetype
from ..core.engine import GEN_SOURCE_ID, DocumentStore
from .render_service import RenderService

MIME = "application/x-pdfedit-uids"
THUMB_SIZES = {"小": 90, "中": 140, "大": 210}
MAX_CACHE = 1500

TYPE_COLORS = {
    pagetype.TEXT: "#188038", pagetype.SCAN: "#e37400", pagetype.OUTLINE: "#9334e6",
    pagetype.GARBLED: "#d93025", pagetype.MIXED: "#1967d2", pagetype.BLANK: "#80868b",
    pagetype.REGION: "#c5221f",
}


class ThumbModel(QAbstractListModel):
    def __init__(self, ws: Workspace, store: DocumentStore, service: RenderService):
        super().__init__()
        self.ws, self.store, self.service = ws, store, service
        self.thumb_w = THUMB_SIZES["中"]
        self.dpr = 1.0
        self._cache: OrderedDict = OrderedDict()
        self.requested: set = set()
        service.imageReady.connect(self._on_image)
        service.typeReady.connect(self._on_type)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.ws)

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()

    def ref(self, row):
        return self.ws.refs[row]

    def page_type(self, ref) -> str | None:
        if ref.kind == "region":
            return pagetype.REGION
        return self.store.get(ref.src).page_type(ref.pno)

    def source_info(self, ref):
        sid, pno = origin_source(self.store, ref)
        if sid == GEN_SOURCE_ID:
            return "#888888", "空白页", pno
        s = self.store.get(sid)
        return s.color, s.name, pno

    def thumb_key(self, ref):
        return ("th", ref.src, ref.pno, ref.rot, self.thumb_w, self.dpr)

    def image(self, ref) -> QImage | None:
        key = self.thumb_key(ref)
        img = self._cache.get(key)
        if img is None:
            w, _ = self.service.renderer.page_size(ref.src, ref.pno, ref.rot)
            self.requested.add(key)
            self.service.request_image(key, ref.src, ref.pno, ref.rot,
                                       self.thumb_w * self.dpr / max(w, 1), priority=1)
        else:
            self._cache.move_to_end(key)
        return img

    def _on_image(self, key, img):
        if not key or key[0] != "th":
            return
        self._cache[key] = img
        while len(self._cache) > MAX_CACHE:
            self._cache.popitem(last=False)
        self._emit_changed(lambda r: r.src == key[1] and r.pno == key[2])

    def _on_type(self, sid, pno, _t):
        self._emit_changed(lambda r: r.src == sid and r.pno == pno)

    def _emit_changed(self, pred):
        for i, r in enumerate(self.ws.refs):
            if pred(r):
                idx = self.index(i)
                self.dataChanged.emit(idx, idx)

    def invalidate_source(self, sid):
        for k in [k for k in self._cache if k[1] == sid]:
            del self._cache[k]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.ws):
            return None
        ref = self.ref(index.row())
        if role == Qt.DisplayRole:
            return str(index.row() + 1)
        if role == Qt.ToolTipRole:
            _, name, pno = self.source_info(ref)
            t = self.page_type(ref)
            tip = f"工作区第 {index.row() + 1} 页\n原文件：{name}\n原页码：第 {pno + 1} 页"
            if t:
                tip += f"\n类型：{pagetype.LABELS.get(t, t)}"
                if t == pagetype.SCAN:
                    tip += "（已识别文字，可搜索）" if self.store.has_ocr(ref.src, ref.pno) else "（未 OCR）"
            if t == pagetype.OUTLINE:
                tip += "\n此处为图形，非文字，无法直接编辑"
            elif t == pagetype.GARBLED:
                tip += "\n文字缺少字符映射，复制搜索可能乱码，可改走 OCR"
            if ref.rot:
                tip += f"\n已旋转 {ref.rot}°"
            return tip
        if role == Qt.UserRole:
            return ref
        return None

    def flags(self, index):
        f = Qt.ItemIsDropEnabled
        if index.isValid():
            f |= Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        return f

    def supportedDropActions(self):
        return Qt.MoveAction | Qt.CopyAction

    def mimeTypes(self):
        return [MIME, "text/uri-list"]

    def mimeData(self, indexes):
        uids = [str(self.ref(i.row()).uid) for i in sorted(indexes, key=lambda i: i.row())]
        md = QMimeData()
        md.setData(MIME, QByteArray(",".join(uids).encode()))
        return md


class ThumbDelegate(QStyledItemDelegate):
    def __init__(self, model: ThumbModel, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index):
        w = self.model.thumb_w
        return QSize(w + 16, int(w * 1.414) + 30)

    def paint(self, p: QPainter, option, index):
        ref = self.model.ref(index.row())
        r = option.rect
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        selected = option.state & QStyle.State_Selected
        if selected:
            p.fillRect(r.adjusted(1, 1, -1, -1), QColor("#d2e3fc"))
            p.setPen(QPen(QColor("#1a73e8"), 2))
            p.drawRect(r.adjusted(2, 2, -2, -2))
        tw = self.model.thumb_w
        box = QRect(r.x() + 8, r.y() + 6, tw, r.height() - 30)
        w, h = self.model.service.renderer.page_size(ref.src, ref.pno, ref.rot)
        k = min(box.width() / w, box.height() / h)
        iw, ih = int(w * k), int(h * k)
        ir = QRect(box.x() + (box.width() - iw) // 2, box.y() + (box.height() - ih) // 2, iw, ih)
        img = self.model.image(ref)
        p.fillRect(ir.translated(2, 2), QColor(0, 0, 0, 40))
        if img is not None:
            p.drawImage(ir, img)
        else:
            p.fillRect(ir, Qt.white)
        p.setPen(QColor("#9aa0a6"))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ir)
        # 来源色条
        color, _, _ = self.model.source_info(ref)
        p.fillRect(QRect(ir.x() - 6, ir.y(), 4, ir.height()), QColor(color))
        # 类型角标
        t = self.model.page_type(ref)
        if t:
            f = QFont(option.font)
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            label = pagetype.LABELS.get(t, t)
            if t == pagetype.SCAN and self.model.store.has_ocr(ref.src, ref.pno):
                label += "·OCR"
            bw = p.fontMetrics().horizontalAdvance(label) + 8
            badge = QRect(ir.right() - bw, ir.y(), bw, 15)
            p.fillRect(badge, QColor(TYPE_COLORS.get(t, "#5f6368")))
            p.setPen(Qt.white)
            p.drawText(badge, Qt.AlignCenter, label)
        if ref.rot:
            p.setPen(QColor("#5f6368"))
            f = QFont(option.font)
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.drawText(QRect(ir.x() + 3, ir.bottom() - 15, 40, 14), Qt.AlignLeft, f"↻{ref.rot}°")
        # 页码
        p.setPen(QColor("#202124"))
        f = QFont(option.font)
        f.setBold(bool(selected))
        p.setFont(f)
        p.drawText(QRect(r.x(), r.bottom() - 22, r.width(), 18), Qt.AlignCenter, str(index.row() + 1))
        p.restore()


class ThumbView(QListView):
    pagesDropped = Signal(list, int)       # uids, 插入位置
    filesDropped = Signal(list, int)       # 文件路径, 插入位置
    contextRequested = Signal(QPoint)

    def __init__(self, model: ThumbModel, parent=None):
        super().__init__(parent)
        self._model = model
        self.setModel(model)
        self.setItemDelegate(ThumbDelegate(model, self))
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.contextRequested.emit)
        self.setStyleSheet("QListView { background: #f1f3f4; }")
        self._drop_row = -1
        self._model.dpr = self.devicePixelRatioF()

    # ---------- 选择辅助 ----------
    def selected_rows(self) -> list[int]:
        return sorted(i.row() for i in self.selectionModel().selectedIndexes())

    def selected_uids(self) -> list[int]:
        return [self._model.ref(r).uid for r in self.selected_rows()]

    def select_rows(self, rows, current: int | None = None):
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        sel = QItemSelection()
        for r in rows:
            idx = self._model.index(r)
            sel.select(idx, idx)
        sm = self.selectionModel()
        sm.select(sel, QItemSelectionModel.ClearAndSelect)
        if current is not None and 0 <= current < self._model.rowCount():
            sm.setCurrentIndex(self._model.index(current), QItemSelectionModel.NoUpdate)
            self.scrollTo(self._model.index(current))

    def select_uids(self, uids):
        s = set(uids)
        rows = [i for i, r in enumerate(self._model.ws.refs) if r.uid in s]
        self.select_rows(rows, rows[0] if rows else None)

    # ---------- 懒加载：重绘后丢弃不可见缩略图请求 ----------
    def paintEvent(self, e):
        self._model.dpr = self.devicePixelRatioF()
        self._model.requested = set()
        super().paintEvent(e)
        self._model.service.cancel_images(self._model.requested)
        if self._drop_row >= 0:
            self._paint_drop_indicator()

    # ---------- 拖拽 ----------
    def _insert_row_at(self, pos: QPoint) -> int:
        n = self._model.rowCount()
        if n == 0:
            return 0
        for dx in (0, -10, 10, -20, 20):
            idx = self.indexAt(pos + QPoint(dx, 0))
            if idx.isValid():
                rect = self.visualRect(idx)
                return idx.row() + (1 if pos.x() > rect.center().x() else 0)
        last = self.visualRect(self._model.index(n - 1))
        if pos.y() > last.bottom() or (pos.y() >= last.top() and pos.x() > last.right()):
            return n
        first = self.visualRect(self._model.index(0))
        if pos.y() < first.top():
            return 0
        # 行尾空白处：插到该行最后一项之后
        for r in range(n - 1, -1, -1):
            vr = self.visualRect(self._model.index(r))
            if vr.top() <= pos.y() <= vr.bottom() and pos.x() > vr.right():
                return r + 1
        return n

    def _paint_drop_indicator(self):
        n = self._model.rowCount()
        if n == 0:
            return
        if self._drop_row < n:
            vr = self.visualRect(self._model.index(self._drop_row))
            x = vr.left() - 2
        else:
            vr = self.visualRect(self._model.index(n - 1))
            x = vr.right() + 2
        p = QPainter(self.viewport())
        p.setPen(QPen(QColor("#d93025"), 3))
        p.drawLine(x, vr.top() + 4, x, vr.bottom() - 4)
        p.end()

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME) or md.hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        if not (md.hasFormat(MIME) or md.hasUrls()):
            e.ignore()
            return
        self._drop_row = self._insert_row_at(e.position().toPoint())
        self.viewport().update()
        e.setDropAction(Qt.MoveAction if md.hasFormat(MIME) else Qt.CopyAction)
        e.accept()
        # 拖到边缘时自动滚动
        y = e.position().y()
        sb = self.verticalScrollBar()
        if y < 30:
            sb.setValue(sb.value() - 20)
        elif y > self.viewport().height() - 30:
            sb.setValue(sb.value() + 20)

    def dragLeaveEvent(self, e):
        self._drop_row = -1
        self.viewport().update()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        row = self._insert_row_at(e.position().toPoint())
        self._drop_row = -1
        self.viewport().update()
        md = e.mimeData()
        if md.hasFormat(MIME):
            uids = [int(x) for x in bytes(md.data(MIME)).decode().split(",") if x]
            # 以 CopyAction 结束，避免视图在拖拽结束后自行删除行
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.pagesDropped.emit(uids, row)
        elif md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.filesDropped.emit(paths, row)
        else:
            e.ignore()
