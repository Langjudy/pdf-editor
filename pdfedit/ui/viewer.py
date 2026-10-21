"""主视图：单页 / 连续滚动、缩放、分块局部高清渲染、搜索高亮、区域选取。

渲染策略（F-VIEW-07、NF-PERF-03）：
- 每页先显示一张低分辨率预览；
- 当前缩放下预览不够清晰时，只对可见区域按 512 像素分块请求高清渲染，
  大幅面 CAD 图纸放大到 6400% 也只渲染屏幕上看得见的部分。
"""
from __future__ import annotations

import math
from collections import OrderedDict

import pymupdf as fitz
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QImage, QPainter, QPainterPath,
                           QPen, QPolygonF)
from PySide6.QtWidgets import QAbstractScrollArea

from ..app.regions import Region, RegionStore
from ..app.workspace import Workspace
from ..core.engine import DocumentStore
from .render_service import RenderService
from .annotlayer import ANNOT_TOOLS, AnnotToolMixin
from .textlayer import TEXT_TOOLS, TextToolMixin

PT_TO_PX = 96.0 / 72.0
MM_PER_PT = 25.4 / 72.0
MIN_ZOOM, MAX_ZOOM = 0.10, 64.0
TILE = 512
PREVIEW_LONG_SIDE = 1600       # 预览图长边（设备像素）
MAX_TILES = 260
MAX_PREVIEWS = 40

TOOLS = ("hand", "rect", "poly", "free") + TEXT_TOOLS + ANNOT_TOOLS


class PageView(QAbstractScrollArea, TextToolMixin, AnnotToolMixin):
    currentChanged = Signal(int)
    zoomChanged = Signal(float)
    regionDrawn = Signal(int, list, bool)      # uid, 原始坐标顶点, 是否矩形
    regionSelected = Signal(int, int)          # uid, region id（-1 表示取消选中）
    statusMessage = Signal(str)
    # 文字编辑
    textBlockSelected = Signal(int, int)           # uid, 块序号（-1 取消选中）
    textEditRequested = Signal(int, int, int)      # uid, 块序号, 光标位置
    textAddRequested = Signal(int, object)         # uid, 单击处（原始坐标）
    textCommitted = Signal(int, int, str, object)  # uid, 块序号（-1 新增）, 文字, 基线起点
    textBlockMoved = Signal(int, int, float, float)
    textDeleteRequested = Signal(int, int)
    outlineClicked = Signal(int)
    # 批注
    annotMarkup = Signal(int, str, list, str)          # uid, 类型, 四边形, 选中文字
    annotNoteRequested = Signal(int, object)           # uid, 单击点
    annotStampRequested = Signal(int, object)
    annotFreeTextRequested = Signal(int, object)       # uid, 矩形 (x0, y0, x1, y1)
    annotShape = Signal(int, str, list)                # uid, 图形, 顶点
    annotInk = Signal(int, list, float)                # uid, 笔画, 平均压感（无压感为 -1）
    annotSelected = Signal(int, str)
    annotMoved = Signal(int, str, float, float)
    annotDeleteRequested = Signal(int, str)
    annotEditRequested = Signal(int, str)

    GAP = 16

    def __init__(self, ws: Workspace, store: DocumentStore, service: RenderService,
                 regions: RegionStore, parent=None):
        super().__init__(parent)
        self.ws, self.store, self.service, self.regions = ws, store, service, regions
        self.renderer = service.renderer
        self.mode = "continuous"
        self.zoom = 1.0
        self.fit: str | None = "width"
        self.tool = "hand"
        self.current = 0
        self._rects: list[QRectF | None] = []
        self._content_w = 0.0
        self._content_h = 0.0
        self._tiles: OrderedDict = OrderedDict()
        self._previews: OrderedDict = OrderedDict()
        self.highlights: dict[int, list] = {}      # uid -> [fitz.Rect 原始坐标]
        self.active_highlight: tuple | None = None  # (uid, fitz.Rect)
        self.selected_region: tuple[int, int] | None = None
        self._pan_start = None
        self._space = False
        self._draw: dict | None = None      # 正在绘制的选区
        self._edit: dict | None = None      # 正在调整的选区
        self._last_preview: OrderedDict = OrderedDict()   # uid -> 最近显示的预览（页面换版本时避免闪白）
        self._text_init()
        self._annot_init()

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(False)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        service.tileReady.connect(self._on_tile)
        service.imageReady.connect(self._on_image)
        regions.listeners.append(lambda uid: self.viewport().update())
        self._relayout_timer = QTimer(self, singleShot=True, interval=0)
        self._relayout_timer.timeout.connect(self.relayout)

    # ================================================================ 布局
    @property
    def scale(self) -> float:
        """逻辑像素 / pt。"""
        return self.zoom * PT_TO_PX

    def refs(self):
        return self.ws.refs

    def _page_size_px(self, i: int) -> tuple[float, float]:
        r = self.refs()[i]
        w, h = self.renderer.page_size(r.src, r.pno, r.rot)
        return w * self.scale, h * self.scale

    def relayout(self, keep_anchor: bool = True):
        refs = self.refs()
        n = len(refs)
        if n == 0:
            self.current = 0
            self._rects = []
            self._content_w = self._content_h = 0
            self._update_scrollbars()
            self.viewport().update()
            return
        self.current = max(0, min(self.current, n - 1))
        if self.fit:
            self._apply_fit()
        vw = self.viewport().width()
        sizes = [self._page_size_px(i) if (self.mode == "continuous" or i == self.current) else None
                 for i in range(n)]
        max_w = max((s[0] for s in sizes if s), default=0)
        self._content_w = max(max_w + 2 * self.GAP, vw)
        rects: list[QRectF | None] = []
        y = self.GAP
        for s in sizes:
            if s is None:
                rects.append(None)
                continue
            x = (self._content_w - s[0]) / 2
            rects.append(QRectF(x, y, s[0], s[1]))
            y += s[1] + self.GAP
        self._rects = rects
        self._content_h = y
        self._update_scrollbars()
        self.viewport().update()

    def _update_scrollbars(self):
        vw, vh = self.viewport().width(), self.viewport().height()
        hs, vs = self.horizontalScrollBar(), self.verticalScrollBar()
        hs.setRange(0, max(0, int(self._content_w - vw)))
        hs.setPageStep(vw)
        hs.setSingleStep(40)
        vs.setRange(0, max(0, int(self._content_h - vh)))
        vs.setPageStep(vh)
        vs.setSingleStep(40)

    def _apply_fit(self):
        if not self.refs():
            return
        r = self.refs()[self.current]
        w, h = self.renderer.page_size(r.src, r.pno, r.rot)
        vw = max(50, self.viewport().width() - 2 * self.GAP - 4)
        vh = max(50, self.viewport().height() - 2 * self.GAP)
        z = vw / (w * PT_TO_PX)
        if self.fit == "page":
            z = min(z, vh / (h * PT_TO_PX))
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        if abs(z - self.zoom) > 1e-6:
            self.zoom = z
            self.zoomChanged.emit(z)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        anchor = self._anchor_at(QPointF(0, 0)) if self.fit else None
        self.relayout()
        if anchor and self.fit:
            self._restore_anchor(anchor, QPointF(0, 0))

    # ================================================================ 视图控制
    def set_mode(self, mode: str):
        if mode != self.mode:
            cur = self.current
            self.mode = mode
            self.relayout()
            self.goto(cur)

    def set_fit(self, fit: str | None):
        self.fit = fit
        cur = self.current
        self.relayout()
        self.goto(cur)

    def set_zoom(self, z: float, anchor: QPointF | None = None):
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        if anchor is None:
            anchor = QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        a = self._anchor_at(anchor)
        self.fit = None
        self.zoom = z
        self.relayout()
        if a:
            self._restore_anchor(a, anchor)
        self.zoomChanged.emit(z)

    def zoom_by(self, factor: float, anchor: QPointF | None = None):
        self.set_zoom(self.zoom * factor, anchor)

    def _anchor_at(self, vp: QPointF):
        """记录视口某点对应的（页序号，页内相对位置）。"""
        if not self._rects:
            return None
        c = self._to_content(vp)
        i = self._page_at(c, nearest=True)
        if i < 0:
            return None
        r = self._rects[i]
        return i, (c.x() - r.x()) / r.width(), (c.y() - r.y()) / r.height()

    def _restore_anchor(self, a, vp: QPointF):
        i, fx, fy = a
        if i >= len(self._rects) or self._rects[i] is None:
            return
        r = self._rects[i]
        self.horizontalScrollBar().setValue(int(r.x() + fx * r.width() - vp.x()))
        self.verticalScrollBar().setValue(int(r.y() + fy * r.height() - vp.y()))

    def goto(self, index: int, rect: fitz.Rect | None = None):
        """跳转到页面；rect（原始坐标）不为空时滚动到该区域。"""
        n = len(self.refs())
        if n == 0:
            return
        index = max(0, min(index, n - 1))
        changed = index != self.current
        self.current = index
        if self.mode == "single":
            self.relayout()
        elif self.fit:
            # 适合宽度/页面以当前页为准；只有缩放比例因此改变时才需要重新布局
            old = self.zoom
            self._apply_fit()
            if abs(self.zoom - old) > 1e-6:
                self.relayout()
        r = self._rects[index]
        if r is None:
            return
        if rect is not None:
            ref = self.refs()[index]
            m = self.renderer.to_display(ref.src, ref.pno, ref.rot, self.scale)
            q = rect * m
            cx = r.x() + (q.x0 + q.x1) / 2
            cy = r.y() + (q.y0 + q.y1) / 2
            self.horizontalScrollBar().setValue(int(cx - self.viewport().width() / 2))
            self.verticalScrollBar().setValue(int(cy - self.viewport().height() / 3))
        else:
            self.verticalScrollBar().setValue(int(r.y() - self.GAP))
            # 混合尺寸（如 A1 图纸与 A4 页）时水平方向对准目标页：放得下就居中，放不下就对齐左边
            vw = self.viewport().width()
            x = r.center().x() - vw / 2 if r.width() + 2 * self.GAP <= vw else r.x() - self.GAP
            self.horizontalScrollBar().setValue(int(x))
        if changed:
            self.currentChanged.emit(index)
        self.viewport().update()

    def next_page(self, step=1):
        self.goto(self.current + step)

    def _on_scroll(self, _):
        if self.mode == "continuous" and self._rects:
            vh = self.viewport().height()
            c = self._to_content(QPointF(0, vh * 0.4))
            i = self._page_at(c, nearest=True, by_y=True)
            if i >= 0 and i != self.current:
                self.current = i
                self.currentChanged.emit(i)
        self.viewport().update()

    # ================================================================ 坐标换算
    def _to_content(self, vp: QPointF) -> QPointF:
        return QPointF(vp.x() + self.horizontalScrollBar().value(),
                       vp.y() + self.verticalScrollBar().value())

    def _to_viewport(self, c: QPointF) -> QPointF:
        return QPointF(c.x() - self.horizontalScrollBar().value(),
                       c.y() - self.verticalScrollBar().value())

    def _page_at(self, c: QPointF, nearest=False, by_y=False) -> int:
        best, bestd = -1, 1e18
        for i, r in enumerate(self._rects):
            if r is None:
                continue
            if by_y:
                if r.top() - self.GAP <= c.y() <= r.bottom():
                    return i
            elif r.contains(c):
                return i
            if nearest:
                d = abs(c.y() - r.center().y())
                if d < bestd:
                    best, bestd = i, d
        return best if nearest else -1

    def _matrix(self, i: int) -> fitz.Matrix:
        """原始坐标 → 内容坐标（逻辑像素）。"""
        ref = self.refs()[i]
        r = self._rects[i]
        m = self.renderer.to_display(ref.src, ref.pno, ref.rot, self.scale)
        return m * fitz.Matrix(1, 0, 0, 1, r.x(), r.y())

    def src_to_vp(self, i: int, pts) -> list[QPointF]:
        m = self._matrix(i)
        out = []
        for x, y in pts:
            p = fitz.Point(x, y) * m
            out.append(self._to_viewport(QPointF(p.x, p.y)))
        return out

    def vp_to_src(self, i: int, vp: QPointF) -> tuple[float, float]:
        c = self._to_content(vp)
        p = fitz.Point(c.x(), c.y()) * ~self._matrix(i)
        return p.x, p.y

    def _clamp_to_page(self, i: int, vp: QPointF) -> QPointF:
        r = self._rects[i]
        c = self._to_content(vp)
        c = QPointF(min(max(c.x(), r.left()), r.right()), min(max(c.y(), r.top()), r.bottom()))
        return self._to_viewport(c)

    # ================================================================ 渲染
    def _on_tile(self, key, img: QImage, x: int, y: int):
        self._tiles[key] = (img, x, y)
        while len(self._tiles) > MAX_TILES:
            self._tiles.popitem(last=False)
        self.viewport().update()

    def _on_image(self, key, img: QImage):
        if key and key[0] == "p":
            self._previews[key] = img
            while len(self._previews) > MAX_PREVIEWS:
                self._previews.popitem(last=False)
            self.viewport().update()

    def drop_caches(self):
        self._tiles.clear()
        self._previews.clear()
        self._last_preview.clear()

    def paintEvent(self, _):
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor("#5f6368"))
        refs = self.refs()
        if not refs or len(self._rects) != len(refs):
            p.setPen(QColor("#e8eaed"))
            p.setFont(QFont(self.font().family(), 13))
            p.drawText(self.viewport().rect(), Qt.AlignCenter,
                       "拖拽 PDF / 图片到此处，或使用「文件 → 导入」")
            self.service.set_tile_jobs([])
            return
        dpr = self.devicePixelRatioF()
        hs, vs = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        vis = QRectF(hs, vs, self.viewport().width(), self.viewport().height())
        jobs = []
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for i, r in enumerate(self._rects):
            if r is None or not r.intersects(vis):
                continue
            ref = refs[i]
            vr = r.translated(-hs, -vs)
            p.fillRect(vr.translated(3, 3), QColor(0, 0, 0, 60))
            p.fillRect(vr, Qt.white)
            ds = self.scale * dpr
            w_pt, h_pt = self.renderer.page_size(ref.src, ref.pno, ref.rot)
            pscale = PREVIEW_LONG_SIDE / max(w_pt, h_pt)
            pkey = ("p", ref.src, ref.pno, ref.rot)
            prev = self._previews.get(pkey)
            if prev is not None:
                self._previews.move_to_end(pkey)
                p.drawImage(vr, prev)
                self._last_preview[ref.uid] = prev
                self._last_preview.move_to_end(ref.uid)
                while len(self._last_preview) > 12:
                    self._last_preview.popitem(last=False)
            else:
                self.service.request_image(pkey, ref.src, ref.pno, ref.rot, pscale, priority=0)
                old = self._last_preview.get(ref.uid)
                if old is not None and abs(old.width() / max(old.height(), 1) - vr.width() / vr.height()) < 0.02:
                    p.drawImage(vr, old)
            if ds > pscale * 1.05:
                jobs += self._paint_tiles(p, ref, r, vr, vis, ds, dpr)
            self._paint_overlays(p, i, ref)
            self._paint_text_layer(p, i, ref)
            self._paint_annot_layer(p, i, ref)
        self.service.set_tile_jobs(jobs)
        if self._editor is not None:
            self._place_editor()
        self._paint_drawing(p)
        self._paint_annot_drawing(p)

    def _paint_tiles(self, p: QPainter, ref, r: QRectF, vr: QRectF, vis: QRectF, ds: float, dpr: float):
        jobs = []
        inter = r.intersected(vis)
        W = math.ceil(r.width() * dpr)
        H = math.ceil(r.height() * dpr)
        lx0 = (inter.left() - r.left()) * dpr
        ly0 = (inter.top() - r.top()) * dpr
        lx1 = (inter.right() - r.left()) * dpr
        ly1 = (inter.bottom() - r.top()) * dpr
        skey = round(ds, 5)
        for ty in range(int(ly0 // TILE), int(max(ly0, ly1 - 1) // TILE) + 1):
            for tx in range(int(lx0 // TILE), int(max(lx0, lx1 - 1) // TILE) + 1):
                key = ("t", ref.src, ref.pno, ref.rot, skey, tx, ty)
                hit = self._tiles.get(key)
                if hit is not None:
                    self._tiles.move_to_end(key)
                    img, x, y = hit
                    p.drawImage(QRectF(vr.x() + x / dpr, vr.y() + y / dpr,
                                       img.width() / dpr, img.height() / dpr), img)
                else:
                    rect = (tx * TILE, ty * TILE, min((tx + 1) * TILE, W), min((ty + 1) * TILE, H))
                    if rect[2] > rect[0] and rect[3] > rect[1]:
                        jobs.append((key, ref.src, ref.pno, ref.rot, ds, rect))
        return jobs

    def _paint_overlays(self, p: QPainter, i: int, ref):
        # 搜索高亮
        hl = self.highlights.get(ref.uid)
        if hl:
            p.setPen(Qt.NoPen)
            for rect in hl:
                active = self.active_highlight and self.active_highlight[0] == ref.uid \
                    and self.active_highlight[1] == rect
                p.setBrush(QColor(255, 120, 0, 110) if active else QColor(255, 230, 0, 100))
                pts = self.src_to_vp(i, [(rect.x0, rect.y0), (rect.x1, rect.y1)])
                p.drawRect(QRectF(pts[0], pts[1]).normalized())
        # 选区
        for reg in self.regions.get(ref.uid):
            sel = self.selected_region == (ref.uid, reg.id)
            poly = QPolygonF(self.src_to_vp(i, reg.points))
            color = QColor("#d93025") if sel else QColor("#1a73e8")
            pen = QPen(color, 2 if sel else 1.5, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 35))
            p.drawPolygon(poly)
            br = poly.boundingRect()
            p.setFont(QFont(self.font().family(), 9))
            label = f"{reg.name}  {self._size_text(reg.bbox())}"
            tw = p.fontMetrics().horizontalAdvance(label) + 8
            p.fillRect(QRectF(br.left(), br.top() - 18, tw, 17), color)
            p.setPen(Qt.white)
            p.drawText(QRectF(br.left() + 4, br.top() - 18, tw, 17), Qt.AlignVCenter, label)
            if sel and reg.is_rect:
                p.setPen(QPen(color, 1))
                p.setBrush(Qt.white)
                for h in self._handles(br).values():
                    p.drawRect(h)

    @staticmethod
    def _size_text(bbox) -> str:
        x0, y0, x1, y1 = bbox
        return f"{(x1 - x0) * MM_PER_PT:.1f}×{(y1 - y0) * MM_PER_PT:.1f} mm"

    def _paint_drawing(self, p: QPainter):
        d = self._draw
        if not d:
            return
        p.setPen(QPen(QColor("#d93025"), 1.5, Qt.DashLine))
        p.setBrush(QColor(217, 48, 37, 30))
        if d["tool"] == "rect":
            rect = QRectF(d["start"], d["end"]).normalized()
            p.drawRect(rect)
            a = self.vp_to_src(d["page"], d["start"])
            b = self.vp_to_src(d["page"], d["end"])
            txt = self._size_text((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
            p.setPen(Qt.white)
            p.fillRect(QRectF(rect.right() + 6, rect.bottom() + 4, 130, 18), QColor(0, 0, 0, 160))
            p.drawText(QRectF(rect.right() + 10, rect.bottom() + 4, 126, 18), Qt.AlignVCenter, txt)
        else:
            pts = list(d["points"])
            if d["tool"] == "poly" and d.get("hover") is not None:
                pts.append(d["hover"])
            if len(pts) >= 2:
                path = QPainterPath(pts[0])
                for q in pts[1:]:
                    path.lineTo(q)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
            p.setBrush(QColor("#d93025"))
            for q in d["points"][:1]:
                p.drawEllipse(q, 4, 4)

    # ================================================================ 选区编辑辅助
    @staticmethod
    def _handles(br: QRectF) -> dict:
        s = 7.0
        pts = {
            "tl": br.topLeft(), "tr": br.topRight(), "bl": br.bottomLeft(), "br": br.bottomRight(),
            "t": QPointF(br.center().x(), br.top()), "b": QPointF(br.center().x(), br.bottom()),
            "l": QPointF(br.left(), br.center().y()), "r": QPointF(br.right(), br.center().y()),
        }
        return {k: QRectF(v.x() - s / 2, v.y() - s / 2, s, s) for k, v in pts.items()}

    def _region_hit(self, vp: QPointF):
        """返回 (页序号, Region, 命中部位)；部位为手柄名、'move' 或 None。"""
        hs, vs = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        vis = QRectF(hs, vs, self.viewport().width(), self.viewport().height())
        # 先检查已选中选区的手柄
        if self.selected_region:
            uid, rid = self.selected_region
            i = self.ws.index_of(uid)
            reg = self.regions.find(uid, rid)
            if i >= 0 and reg and self._rects[i] is not None:
                poly = QPolygonF(self.src_to_vp(i, reg.points))
                if reg.is_rect:
                    for name, h in self._handles(poly.boundingRect()).items():
                        if h.adjusted(-3, -3, 3, 3).contains(vp):
                            return i, reg, name
        for i, r in enumerate(self._rects):
            if r is None or not r.intersects(vis):
                continue
            uid = self.refs()[i].uid
            for reg in reversed(self.regions.get(uid)):
                poly = QPolygonF(self.src_to_vp(i, reg.points))
                if poly.containsPoint(vp, Qt.OddEvenFill):
                    return i, reg, "move"
        return None

    def select_region(self, uid: int, rid: int, scroll: bool = True):
        self.selected_region = (uid, rid) if rid >= 0 else None
        if scroll and rid >= 0:
            i = self.ws.index_of(uid)
            reg = self.regions.find(uid, rid)
            if i >= 0 and reg:
                self.goto(i, fitz.Rect(*reg.bbox()))
        self.viewport().update()

    # ================================================================ 鼠标键盘
    def set_tool(self, tool: str):
        self.close_editor(True)
        if tool not in TEXT_TOOLS and self.sel_block:
            self.set_selected_block(-1, -1)
        self.tool = tool
        self._draw = None
        self._adraw = None
        if tool != "a_annot":
            self.sel_annot = None
        self._update_cursor()
        self.viewport().update()

    def _update_cursor(self, vp: QPointF | None = None):
        if self._space or self._pan_start:
            self.viewport().setCursor(Qt.ClosedHandCursor if self._pan_start else Qt.OpenHandCursor)
            return
        if vp is not None and self.tool in ("hand", "rect"):
            hit = self._region_hit(vp)
            if hit:
                name = hit[2]
                cur = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                       "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                       "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor}.get(name, Qt.SizeAllCursor)
                self.viewport().setCursor(cur)
                return
        if self.tool in TEXT_TOOLS:
            self.viewport().setCursor(Qt.IBeamCursor if self.tool == "addtext" else Qt.ArrowCursor)
            return
        if self.tool in ANNOT_TOOLS:
            self.viewport().setCursor(Qt.IBeamCursor if self.tool in ("a_hl", "a_ul", "a_so", "a_sq") else
                                      Qt.ArrowCursor if self.tool == "a_annot" else Qt.CrossCursor)
            return
        self.viewport().setCursor(Qt.OpenHandCursor if self.tool == "hand" else Qt.CrossCursor)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            steps = e.angleDelta().y() / 120.0
            self.zoom_by(1.15 ** steps, e.position())
            e.accept()
            return
        if self.mode == "single":
            vs = self.verticalScrollBar()
            dy = e.angleDelta().y()
            if dy < 0 and vs.value() >= vs.maximum() and self.current < len(self.refs()) - 1:
                self.goto(self.current + 1)
                return
            if dy > 0 and vs.value() <= vs.minimum() and self.current > 0:
                self.goto(self.current - 1)
                self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
                return
        super().wheelEvent(e)

    def mousePressEvent(self, e):
        self.setFocus()
        vp = e.position()
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and self._space):
            self._start_pan(vp)
            return
        if e.button() != Qt.LeftButton:
            return
        if self._text_press(e) or self._annot_press(e):
            return
        c = self._to_content(vp)
        page = self._page_at(c)
        if self.tool == "poly" and self._draw:
            d = self._draw
            q = self._clamp_to_page(d["page"], vp)
            if len(d["points"]) >= 3 and (q - d["points"][0]).manhattanLength() < 10:
                self._finish_polygon()
            else:
                d["points"].append(q)
            self.viewport().update()
            return
        if self.tool in ("hand", "rect"):
            hit = self._region_hit(vp)
            if hit:
                i, reg, part = hit
                uid = self.refs()[i].uid
                self.selected_region = (uid, reg.id)
                self.regionSelected.emit(uid, reg.id)
                self._edit = {"page": i, "uid": uid, "rid": reg.id, "part": part,
                              "orig": list(reg.points), "start": vp}
                self.viewport().update()
                return
            if self.selected_region:
                self.selected_region = None
                self.regionSelected.emit(-1, -1)
        if self.tool == "hand" or page < 0:
            self._start_pan(vp)
            if page >= 0 and page != self.current:
                self.current = page
                self.currentChanged.emit(page)
            return
        q = self._clamp_to_page(page, vp)
        if self.tool == "rect":
            self._draw = {"tool": "rect", "page": page, "start": q, "end": q}
        elif self.tool == "free":
            self._draw = {"tool": "free", "page": page, "points": [q]}
        elif self.tool == "poly":
            self._draw = {"tool": "poly", "page": page, "points": [q], "hover": q}
        self.viewport().update()

    def _start_pan(self, vp):
        self._pan_start = (vp, self.horizontalScrollBar().value(), self.verticalScrollBar().value())
        self._update_cursor()

    def mouseMoveEvent(self, e):
        vp = e.position()
        if self._pan_start:
            start, h0, v0 = self._pan_start
            self.horizontalScrollBar().setValue(int(h0 - (vp.x() - start.x())))
            self.verticalScrollBar().setValue(int(v0 - (vp.y() - start.y())))
            return
        if self._edit:
            self._apply_edit(vp)
            return
        if self._text_move(e) or self._annot_move(e):
            return
        d = self._draw
        if d:
            q = self._clamp_to_page(d["page"], vp)
            if d["tool"] == "rect":
                d["end"] = q
            elif d["tool"] == "free":
                if (q - d["points"][-1]).manhattanLength() >= 3:
                    d["points"].append(q)
            else:
                d["hover"] = q
            self.viewport().update()
            return
        self._update_cursor(vp)
        i = self._page_at(self._to_content(vp))
        if i >= 0:
            x, y = self.vp_to_src(i, vp)
            self.statusMessage.emit(f"X {x * MM_PER_PT:.1f} mm, Y {y * MM_PER_PT:.1f} mm")

    def mouseReleaseEvent(self, e):
        if self._pan_start and e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._pan_start = None
            self._update_cursor(e.position())
            return
        if self._edit:
            self._edit = None
            return
        if self._text_release(e) or self._annot_release(e):
            return
        d = self._draw
        if not d or e.button() != Qt.LeftButton:
            return
        if d["tool"] == "rect":
            rect = QRectF(d["start"], d["end"]).normalized()
            self._draw = None
            if rect.width() >= 4 and rect.height() >= 4:
                a = self.vp_to_src(d["page"], rect.topLeft())
                b = self.vp_to_src(d["page"], rect.bottomRight())
                self.regionDrawn.emit(self.refs()[d["page"]].uid, Region.rect_points(*a, *b), True)
            self.viewport().update()
        elif d["tool"] == "free":
            self._draw = None
            if len(d["points"]) >= 3:
                pts = [self.vp_to_src(d["page"], q) for q in d["points"]]
                self.regionDrawn.emit(self.refs()[d["page"]].uid, pts, False)
            self.viewport().update()

    def mouseDoubleClickEvent(self, e):
        if self._text_double(e) or self._annot_double(e):
            return
        if self.tool == "poly" and self._draw:
            self._finish_polygon()
            return
        super().mouseDoubleClickEvent(e)

    def _finish_polygon(self):
        d = self._draw
        self._draw = None
        pts = d["points"]
        # 去掉双击带来的重复点
        clean = [pts[0]]
        for q in pts[1:]:
            if (q - clean[-1]).manhattanLength() > 2:
                clean.append(q)
        if len(clean) >= 3:
            src = [self.vp_to_src(d["page"], q) for q in clean]
            self.regionDrawn.emit(self.refs()[d["page"]].uid, src, False)
        self.viewport().update()

    def _apply_edit(self, vp: QPointF):
        ed = self._edit
        i = ed["page"]
        reg = self.regions.find(ed["uid"], ed["rid"])
        if reg is None:
            self._edit = None
            return
        vp = self._clamp_to_page(i, vp)
        if ed["part"] == "move":
            a = self.vp_to_src(i, ed["start"])
            b = self.vp_to_src(i, vp)
            dx, dy = b[0] - a[0], b[1] - a[1]
            self.regions.set_points(ed["uid"], ed["rid"], [(x + dx, y + dy) for x, y in ed["orig"]])
            return
        # 调整矩形：在视口坐标中移动对应边，再换算回原始坐标
        poly = QPolygonF(self.src_to_vp(i, ed["orig"]))
        br = poly.boundingRect()
        l, t, r, b = br.left(), br.top(), br.right(), br.bottom()
        part = ed["part"]
        if "l" in part:
            l = vp.x()
        if "r" in part:
            r = vp.x()
        if "t" in part:
            t = vp.y()
        if "b" in part:
            b = vp.y()
        a = self.vp_to_src(i, QPointF(min(l, r), min(t, b)))
        c = self.vp_to_src(i, QPointF(max(l, r), max(t, b)))
        self.regions.set_points(ed["uid"], ed["rid"], Region.rect_points(*a, *c))

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Space and not e.isAutoRepeat():
            self._space = True
            self._update_cursor()
            return
        if self._text_key(e) or self._annot_key(e):
            return
        if self._draw and k == Qt.Key_Escape:
            self._draw = None
            self.viewport().update()
            return
        if self._draw and self._draw["tool"] == "poly":
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_polygon()
                return
            if k == Qt.Key_Backspace and len(self._draw["points"]) > 1:
                self._draw["points"].pop()
                self.viewport().update()
                return
        if k == Qt.Key_PageDown or (self.mode == "single" and k == Qt.Key_Right):
            self.next_page(1)
        elif k == Qt.Key_PageUp or (self.mode == "single" and k == Qt.Key_Left):
            self.next_page(-1)
        elif k == Qt.Key_Home and e.modifiers() & Qt.ControlModifier:
            self.goto(0)
        elif k == Qt.Key_End and e.modifiers() & Qt.ControlModifier:
            self.goto(len(self.refs()) - 1)
        else:
            super().keyPressEvent(e)

    def viewportEvent(self, e):
        if self._annot_tablet(e):
            return False
        return super().viewportEvent(e)

    def event(self, e):
        # 选中了文字块 / 批注、或正在绘制时，Delete、Esc、回车等由视图自己处理，
        # 不触发窗口级快捷键（否则 Delete 会删除整页）
        if e.type() == QEvent.ShortcutOverride and e.key() in (
                Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            busy = (self.sel_block is not None or self.sel_annot is not None or self._draw is not None
                    or self._adraw is not None or self.selected_region is not None)
            if busy:
                e.accept()
                return True
        return super().event(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = False
            self._update_cursor(QPointF(self.viewport().mapFromGlobal(QCursor.pos())))
            return
        super().keyReleaseEvent(e)
