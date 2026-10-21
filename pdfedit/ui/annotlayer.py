"""主视图的批注交互（混入 PageView，技术要求第 9 节）。

工具：
- 文字标记 hl / ul / so / sq：按住拖过文字，松开生成高亮 / 下划线 / 删除线 / 波浪线；
- note 便签、stamp 图章：单击放置；
- freetext 文本框、rect 矩形、ellipse 椭圆、line 直线、arrow 箭头：拖拽；
- polygon 多边形、cloud 云线：逐点单击，双击或回车闭合；
- ink 画笔：按住绘制（数位板压感用于线宽）；
- annot 选择批注：单击选中、拖动移动、Delete 删除、双击编辑内容。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QPolygonF

from ..core import annots as A

MARKUP_TOOLS = ("hl", "ul", "so", "sq")
DRAG_TOOLS = ("freetext", "rect", "ellipse", "line", "arrow")
POLY_TOOLS = ("polygon", "cloud")
ANNOT_KINDS = MARKUP_TOOLS + DRAG_TOOLS + POLY_TOOLS + ("note", "stamp", "ink", "annot")
# 视图中的工具名带 a_ 前缀，避免与区域选取工具（rect 等）重名
ANNOT_TOOLS = tuple("a_" + k for k in ANNOT_KINDS)


class AnnotToolMixin:
    def _annot_init(self):
        self.annot_list = None            # callable(ref) -> list[AnnotInfo]
        self.sel_annot: tuple[int, str] | None = None
        self._adraw: dict | None = None
        self._amove: dict | None = None
        self._chars_cache: dict = {}
        self._pressure: list = []

    def _atool(self) -> str | None:
        """当前批注工具（去掉 a_ 前缀）；不是批注工具时为 None。"""
        t = self.tool
        return t[2:] if t.startswith("a_") else None

    # ---------- 查询 ----------
    def _page_chars(self, i: int):
        ref = self.refs()[i]
        key = (ref.src, ref.pno)
        if key not in self._chars_cache:
            if len(self._chars_cache) > 50:
                self._chars_cache.clear()
            self._chars_cache[key] = A.chars_in_order(self._blocks_of(i))
        return self._chars_cache[key]

    def _annots_of(self, i: int):
        if self.annot_list is None:
            return []
        try:
            return self.annot_list(self.refs()[i])
        except Exception:  # noqa: BLE001
            return []

    def _rect_vp(self, i: int, r) -> QRectF:
        pts = self.src_to_vp(i, [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)])
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def _annot_at(self, i: int, vp: QPointF):
        best = None
        for a in self._annots_of(i):
            r = self._rect_vp(i, a.rect).adjusted(-3, -3, 3, 3)
            if r.contains(vp):
                area = r.width() * r.height()
                if best is None or area < best[0]:
                    best = (area, a)
        return best[1] if best else None

    def select_annot(self, uid: int, aid: str | None):
        self.sel_annot = (uid, aid) if aid else None
        self.viewport().update()

    # ---------- 数位板压感 ----------
    def _annot_tablet(self, e) -> bool:
        """记录画笔压感；返回 True 表示是数位板事件（不消费，交给随后合成的鼠标事件）。"""
        t = e.type()
        if t in (QEvent.TabletPress, QEvent.TabletMove) and self._adraw and self._adraw.get("tool") == "ink":
            self._pressure.append(float(e.pressure()))
        if t in (QEvent.TabletPress, QEvent.TabletMove, QEvent.TabletRelease):
            e.ignore()
            return True
        return False

    # ---------- 鼠标键盘 ----------
    def _annot_press(self, e) -> bool:
        if self._atool() is None or e.button() != Qt.LeftButton:
            return False
        vp = e.position()
        i = self._page_at(self._to_content(vp))
        if self._adraw and self._adraw["tool"] in POLY_TOOLS:
            d = self._adraw
            q = self._clamp_to_page(d["page"], vp)
            if len(d["points"]) >= 3 and (q - d["points"][0]).manhattanLength() < 10:
                self._finish_annot_poly()
            else:
                d["points"].append(q)
            self.viewport().update()
            return True
        if i < 0:
            return True
        ref = self.refs()[i]
        if i != self.current:
            self.current = i
            self.currentChanged.emit(i)
        x, y = self.vp_to_src(i, vp)
        tool = self._atool()
        if tool in MARKUP_TOOLS:
            chars = self._page_chars(i)
            if not chars:
                self.statusMessage.emit("该页没有可选择的文字（扫描页请先 OCR）")
                return True
            k = A.nearest_char(chars, x, y)
            self._adraw = {"tool": tool, "page": i, "chars": chars, "i0": k, "i1": k}
        elif tool == "note":
            self.annotNoteRequested.emit(ref.uid, (x, y))
        elif tool == "stamp":
            self.annotStampRequested.emit(ref.uid, (x, y))
        elif tool in DRAG_TOOLS:
            q = self._clamp_to_page(i, vp)
            self._adraw = {"tool": tool, "page": i, "start": q, "end": q}
        elif tool in POLY_TOOLS:
            q = self._clamp_to_page(i, vp)
            self._adraw = {"tool": tool, "page": i, "points": [q], "hover": q}
        elif tool == "ink":
            self._pressure = []
            self._adraw = {"tool": "ink", "page": i, "points": [self._clamp_to_page(i, vp)]}
        elif tool == "annot":
            a = self._annot_at(i, vp)
            if a is None:
                self.select_annot(ref.uid, None)
                self.annotSelected.emit(-1, "")
                return True
            self.select_annot(ref.uid, a.id)
            self.annotSelected.emit(ref.uid, a.id)
            self._amove = {"page": i, "uid": ref.uid, "aid": a.id, "start": vp, "cur": vp,
                           "rect": a.rect, "moved": False}
        self.viewport().update()
        return True

    def _annot_move(self, e) -> bool:
        if self._atool() is None:
            return False
        vp = e.position()
        if self._amove:
            self._amove["cur"] = vp
            if (vp - self._amove["start"]).manhattanLength() > 4:
                self._amove["moved"] = True
            self.viewport().update()
            return True
        d = self._adraw
        if not d:
            i = self._page_at(self._to_content(vp))
            if self._atool() == "annot" and i >= 0:
                self.viewport().setCursor(Qt.SizeAllCursor if self._annot_at(i, vp) else Qt.ArrowCursor)
            elif self._atool() in MARKUP_TOOLS:
                self.viewport().setCursor(Qt.IBeamCursor)
            else:
                self.viewport().setCursor(Qt.CrossCursor)
            return False
        q = self._clamp_to_page(d["page"], vp)
        if d["tool"] in MARKUP_TOOLS:
            x, y = self.vp_to_src(d["page"], q)
            d["i1"] = A.nearest_char(d["chars"], x, y)
        elif d["tool"] in DRAG_TOOLS:
            d["end"] = q
        elif d["tool"] in POLY_TOOLS:
            d["hover"] = q
        elif d["tool"] == "ink":
            if (q - d["points"][-1]).manhattanLength() >= 2:
                d["points"].append(q)
        self.viewport().update()
        return True

    def _annot_release(self, e) -> bool:
        if self._amove:
            mv, self._amove = self._amove, None
            if mv["moved"]:
                a = self.vp_to_src(mv["page"], mv["start"])
                b = self.vp_to_src(mv["page"], mv["cur"])
                self.annotMoved.emit(mv["uid"], mv["aid"], b[0] - a[0], b[1] - a[1])
            self.viewport().update()
            return True
        d = self._adraw
        if not d or d["tool"] in POLY_TOOLS or e.button() != Qt.LeftButton:
            return bool(d)
        self._adraw = None
        i = d["page"]
        uid = self.refs()[i].uid
        t = d["tool"]
        if t in MARKUP_TOOLS:
            quads = A.selection_quads(d["chars"], d["i0"], d["i1"])
            if quads:
                self.annotMarkup.emit(uid, t, quads, A.selection_text(d["chars"], d["i0"], d["i1"]))
        elif t in DRAG_TOOLS:
            a = self.vp_to_src(i, d["start"])
            b = self.vp_to_src(i, d["end"])
            small = (d["end"] - d["start"]).manhattanLength() < 8
            if t == "freetext":
                if small:
                    b = (a[0] + 200, a[1] + 40)
                x0, x1 = sorted((a[0], b[0]))
                y0, y1 = sorted((a[1], b[1]))
                self.annotFreeTextRequested.emit(uid, (x0, y0, x1, y1))
            elif not small:
                self.annotShape.emit(uid, t, [a, b])
        elif t == "ink" and len(d["points"]) >= 2:
            pts = [self.vp_to_src(i, q) for q in d["points"]]
            pr = sum(self._pressure) / len(self._pressure) if self._pressure else -1.0
            self.annotInk.emit(uid, [pts], pr)
        self.viewport().update()
        return True

    def _annot_double(self, e) -> bool:
        if self._adraw and self._adraw["tool"] in POLY_TOOLS:
            self._finish_annot_poly()
            return True
        if self._atool() == "annot":
            vp = e.position()
            i = self._page_at(self._to_content(vp))
            if i >= 0:
                a = self._annot_at(i, vp)
                if a is not None:
                    self.annotEditRequested.emit(self.refs()[i].uid, a.id)
            return True
        return False

    def _finish_annot_poly(self):
        d, self._adraw = self._adraw, None
        pts = [d["points"][0]]
        for q in d["points"][1:]:
            if (q - pts[-1]).manhattanLength() > 2:
                pts.append(q)
        if len(pts) >= 3:
            i = d["page"]
            self.annotShape.emit(self.refs()[i].uid, d["tool"], [self.vp_to_src(i, q) for q in pts])
        self.viewport().update()

    def _annot_key(self, e) -> bool:
        if self._atool() is None:
            return False
        k = e.key()
        if self._adraw and k == Qt.Key_Escape:
            self._adraw = None
            self.viewport().update()
            return True
        if self._adraw and self._adraw["tool"] in POLY_TOOLS:
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_annot_poly()
                return True
            if k == Qt.Key_Backspace and len(self._adraw["points"]) > 1:
                self._adraw["points"].pop()
                self.viewport().update()
                return True
        if self._atool() == "annot" and self.sel_annot and k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.annotDeleteRequested.emit(*self.sel_annot)
            return True
        return False

    # ---------- 绘制 ----------
    def _paint_annot_layer(self, p, i: int, ref):
        if self._atool() is None:
            return
        show_all = self._atool() == "annot"
        for a in self._annots_of(i):
            sel = self.sel_annot == (ref.uid, a.id)
            if not (sel or show_all):
                continue
            r = self._rect_vp(i, a.rect).adjusted(-2, -2, 2, 2)
            if sel:
                p.setPen(QPen(QColor("#1a73e8"), 1.6))
                p.setBrush(QColor(26, 115, 232, 20))
            else:
                p.setPen(QPen(QColor(26, 115, 232, 130), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            p.drawRect(r)
            if sel and self._amove and self._amove.get("moved"):
                p.setPen(QPen(QColor("#d93025"), 1.4, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r.translated(self._amove["cur"] - self._amove["start"]))

    def _paint_annot_drawing(self, p):
        d = self._adraw
        if not d:
            return
        t = d["tool"]
        if t in MARKUP_TOOLS:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 200, 0, 90))
            for q in A.selection_quads(d["chars"], d["i0"], d["i1"]):
                p.drawRect(self._rect_vp(d["page"], q.rect))
            return
        p.setPen(QPen(QColor("#d93025"), 1.5, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        if t in DRAG_TOOLS:
            r = QRectF(d["start"], d["end"]).normalized()
            if t == "ellipse":
                p.drawEllipse(r)
            elif t in ("line", "arrow"):
                p.drawLine(d["start"], d["end"])
            else:
                p.drawRect(r)
        elif t in POLY_TOOLS:
            pts = list(d["points"]) + ([d["hover"]] if d.get("hover") is not None else [])
            p.drawPolyline(QPolygonF(pts))
        elif t == "ink" and len(d["points"]) >= 2:
            p.setPen(QPen(QColor("#d93025"), 2))
            path = QPainterPath(d["points"][0])
            for q in d["points"][1:]:
                path.lineTo(q)
            p.drawPath(path)
