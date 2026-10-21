"""主视图的文字编辑交互（混入 PageView）。

- 「编辑文字」工具：文字块以虚线框标示；单击选中，拖动移动，Delete 删除，双击 / 回车进入编辑；
- 「添加文字」工具：在页面任意位置单击新建文字框；
- 编辑框直接覆盖在页面原文字位置上，Esc 取消，Ctrl+Enter（单行时 Enter）或点击别处提交。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPen, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QToolTip

from ..core import pagetype

TEXT_TOOLS = ("text", "addtext")
OUTLINE_TIP = "此处为图形，非文字，无法直接编辑\n（可在阶段三使用「按扫描页方式编辑」）"


class InlineEditor(QPlainTextEdit):
    committed = Signal()
    cancelled = Signal()

    def __init__(self, parent, single_line: bool):
        super().__init__(parent)
        self.single_line = single_line
        self._done = False
        self.setFrameStyle(0)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QPlainTextEdit { background: rgba(255,255,240,240); border: 1.5px solid #d93025;"
                           " padding: 0px; }")
        self.document().setDocumentMargin(1)
        self.setToolTip("Esc 取消；" + ("Enter 提交" if single_line else "Ctrl+Enter 提交，Enter 换行")
                        + "；点击别处也会提交")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.finish(False)
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and (
                self.single_line or e.modifiers() & Qt.ControlModifier):
            self.finish(True)
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.finish(True)

    def finish(self, ok: bool):
        if self._done:
            return
        self._done = True
        (self.committed if ok else self.cancelled).emit()


class TextToolMixin:
    """需要宿主提供：refs()、src_to_vp()、vp_to_src()、_rects、_to_content()、_page_at()、scale。"""

    def _text_init(self):
        self.text_blocks = None        # callable(ref) -> list[TBlock]
        self.page_type_of = None       # callable(ref) -> str | None
        self.sel_block: tuple[int, int] | None = None
        self._tmove: dict | None = None
        self._editor: InlineEditor | None = None
        self._edmeta: dict | None = None
        self._last_tip_page = -1

    # ---------- 查询 ----------
    def _blocks_of(self, i: int):
        if self.text_blocks is None:
            return []
        try:
            return self.text_blocks(self.refs()[i])
        except Exception:  # noqa: BLE001
            return []

    def _block_vp_rect(self, i: int, block) -> QRectF:
        x0, y0, x1, y1 = block.bbox
        pts = self.src_to_vp(i, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def _block_at(self, vp: QPointF):
        i = self._page_at(self._to_content(vp))
        if i < 0:
            return -1, None
        for b in self._blocks_of(i):
            if self._block_vp_rect(i, b).adjusted(-3, -3, 3, 3).contains(vp):
                return i, b
        return i, None

    def selected_block(self):
        if not self.sel_block:
            return None
        uid, bid = self.sel_block
        i = self.ws.index_of(uid)
        blocks = self._blocks_of(i) if i >= 0 else []
        return blocks[bid] if bid < len(blocks) else None

    def _cursor_index(self, i: int, block, vp: QPointF) -> int:
        """点击位置对应到块文字中的光标序号。"""
        x, y = self.vp_to_src(i, vp)
        best, best_d, pos = len(block.text), 1e18, 0
        for ln in block.lines:
            for k, ch in enumerate(ln.chars):
                cx = (ch.bbox[0] + ch.bbox[2]) / 2
                cy = (ch.bbox[1] + ch.bbox[3]) / 2
                d = (cx - x) ** 2 + (cy - y) ** 2 * 4
                if d < best_d:
                    after = (x > cx) if abs(ln.dir[0]) >= abs(ln.dir[1]) else ((y > cy) == (ln.dir[1] > 0))
                    best, best_d = pos + k + (1 if after else 0), d
            pos += len(ln.chars) + 1
        return best

    # ---------- 选择 ----------
    def set_selected_block(self, uid: int, bid: int):
        self.sel_block = (uid, bid) if bid >= 0 else None
        self.textBlockSelected.emit(uid, bid)
        self.viewport().update()

    # ---------- 绘制 ----------
    def _paint_text_layer(self, p, i: int, ref):
        if self.tool not in TEXT_TOOLS:
            return
        blocks = self._blocks_of(i)
        for b in blocks:
            r = self._block_vp_rect(i, b)
            sel = self.sel_block == (ref.uid, b.id)
            if sel:
                p.setPen(QPen(QColor("#1a73e8"), 1.6))
                p.setBrush(QColor(26, 115, 232, 25))
            else:
                p.setPen(QPen(QColor(26, 115, 232, 150), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            p.drawRect(r.adjusted(-2, -2, 2, 2))
            if sel and self._tmove and self._tmove.get("moved"):
                d = self._tmove["cur"] - self._tmove["start"]
                p.setPen(QPen(QColor("#d93025"), 1.4, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r.adjusted(-2, -2, 2, 2).translated(d))

    # ---------- 鼠标键盘（返回 True 表示已处理） ----------
    def _text_press(self, e) -> bool:
        if self.tool not in TEXT_TOOLS or e.button() != Qt.LeftButton:
            return False
        vp = e.position()
        i, b = self._block_at(vp)
        if i < 0:
            self.set_selected_block(-1, -1)
            return True
        ref = self.refs()[i]
        if i != self.current:
            self.current = i
            self.currentChanged.emit(i)
        if self.tool == "addtext":
            x, y = self.vp_to_src(i, vp)
            self.textAddRequested.emit(ref.uid, (x, y))
            return True
        if b is None:
            self.set_selected_block(-1, -1)
            t = self.page_type_of(ref) if self.page_type_of else None
            if t in (pagetype.OUTLINE, pagetype.SCAN):
                self.outlineClicked.emit(ref.uid)
            return True
        if self.sel_block != (ref.uid, b.id):
            self.set_selected_block(ref.uid, b.id)
        self._tmove = {"page": i, "uid": ref.uid, "bid": b.id, "start": vp, "cur": vp, "moved": False}
        return True

    def _text_move(self, e) -> bool:
        if self.tool not in TEXT_TOOLS:
            return False
        vp = e.position()
        if self._tmove:
            self._tmove["cur"] = vp
            if (vp - self._tmove["start"]).manhattanLength() > 4:
                self._tmove["moved"] = True
            self.viewport().update()
            return True
        i, b = self._block_at(vp)
        if b is not None:
            self.viewport().setCursor(Qt.SizeAllCursor if self.sel_block == (self.refs()[i].uid, b.id)
                                      else Qt.IBeamCursor)
        else:
            self.viewport().setCursor(Qt.IBeamCursor if self.tool == "addtext" else Qt.ArrowCursor)
        # 曲线页悬停提示
        if i >= 0 and self.page_type_of:
            t = self.page_type_of(self.refs()[i])
            if t == pagetype.OUTLINE and b is None:
                if self._last_tip_page != i:
                    QToolTip.showText(self.viewport().mapToGlobal(vp.toPoint()), OUTLINE_TIP, self.viewport())
                    self._last_tip_page = i
            else:
                self._last_tip_page = -1
        return False

    def _text_release(self, e) -> bool:
        if not self._tmove:
            return False
        mv, self._tmove = self._tmove, None
        if mv["moved"]:
            i = mv["page"]
            a = self.vp_to_src(i, mv["start"])
            b = self.vp_to_src(i, mv["cur"])
            self.textBlockMoved.emit(mv["uid"], mv["bid"], b[0] - a[0], b[1] - a[1])
        self.viewport().update()
        return True

    def _text_double(self, e) -> bool:
        if self.tool != "text":
            return False
        i, b = self._block_at(e.position())
        if b is not None:
            uid = self.refs()[i].uid
            self.set_selected_block(uid, b.id)
            self.textEditRequested.emit(uid, b.id, self._cursor_index(i, b, e.position()))
        return True

    def _text_key(self, e) -> bool:
        if self.tool not in TEXT_TOOLS or self._editor is not None:
            return False
        k = e.key()
        if self.sel_block and k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.textDeleteRequested.emit(*self.sel_block)
            return True
        if self.sel_block and k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_F2):
            b = self.selected_block()
            if b is not None:
                self.textEditRequested.emit(self.sel_block[0], self.sel_block[1], len(b.text))
            return True
        if k == Qt.Key_Escape and self.sel_block:
            self.set_selected_block(-1, -1)
            return True
        return False

    # ---------- 就地编辑框 ----------
    def open_editor(self, uid: int, bid: int, text: str, family: str, size_pt: float, color,
                    cursor: int = -1, origin: tuple | None = None):
        """bid < 0 表示新增文字，origin 为单击处（原始坐标，作为文字框左上角）。"""
        self.close_editor(False)
        i = self.ws.index_of(uid)
        if i < 0:
            return
        single = bid >= 0 and "\n" not in text
        ed = InlineEditor(self.viewport(), single)
        f = QFont(family)
        f.setPixelSize(max(8, round(size_pt * self.scale)))
        ed.setFont(f)
        r, g, b = (int(c * 255) for c in color)
        ed.setStyleSheet(ed.styleSheet() + f" QPlainTextEdit {{ color: rgb({r},{g},{b}); }}")
        ed.setPlainText(text)
        cur = ed.textCursor()
        cur.setPosition(len(text) if cursor < 0 else max(0, min(cursor, len(text))))
        ed.setTextCursor(cur)
        if bid < 0:
            cur.movePosition(QTextCursor.End)
        self._editor = ed
        self._edmeta = {"uid": uid, "bid": bid, "origin": origin, "size": size_pt}
        ed.committed.connect(lambda: self._finish_editor(True))
        ed.cancelled.connect(lambda: self._finish_editor(False))
        ed.textChanged.connect(self._place_editor)
        self._place_editor()
        ed.show()
        ed.setFocus()

    def _place_editor(self):
        ed, meta = self._editor, self._edmeta
        if ed is None:
            return
        i = self.ws.index_of(meta["uid"])
        if i < 0 or self._rects[i] is None:
            return
        px = max(8, round(meta["size"] * self.scale))
        if ed.font().pixelSize() != px:      # 缩放后同步字号
            f = ed.font()
            f.setPixelSize(px)
            ed.setFont(f)
        fm = ed.fontMetrics()
        lines = ed.toPlainText().split("\n")
        tw = max(fm.horizontalAdvance(l) for l in lines) + 16
        th = fm.lineSpacing() * len(lines) + 8
        if meta["bid"] >= 0:
            blocks = self._blocks_of(i)
            if meta["bid"] >= len(blocks):
                return
            r = self._block_vp_rect(i, blocks[meta["bid"]])
            rect = QRectF(r.x() - 3, r.y() - 3, max(r.width() + 6, tw), max(r.height() + 6, th))
        else:
            pt = self.src_to_vp(i, [meta["origin"]])[0]
            rect = QRectF(pt.x(), pt.y(), max(160, tw), th)
        ed.setGeometry(rect.toAlignedRect())

    def _finish_editor(self, ok: bool):
        ed, meta = self._editor, self._edmeta
        if ed is None:
            return
        self._editor, self._edmeta = None, None
        text = ed.toPlainText()
        ed.hide()
        ed.deleteLater()
        self.setFocus()
        if ok:
            origin = None
            if meta["bid"] < 0 and meta["origin"]:
                # 单击处作为文字框左上角，换算成第一行基线
                x, y = meta["origin"]
                origin = (x, y + meta["size"] * 0.88)
            self.textCommitted.emit(meta["uid"], meta["bid"], text, origin)
        self.viewport().update()

    def close_editor(self, commit: bool = True):
        if self._editor is not None:
            self._editor.finish(commit)
