"""批注服务：在页面新版本上增删改批注（可撤销）、全工作区批注列表、涂黑、摘要导出。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace

from ..core import annots as A
from ..core.engine import ENGINE_LOCK, GEN_SOURCE_ID, DocumentStore
from .textservice import TextService
from .workspace import PageRef, Workspace


class AnnotService:
    def __init__(self, store: DocumentStore, ws: Workspace, texts: TextService):
        self.store, self.ws, self.texts = store, ws, texts
        self._cache: OrderedDict = OrderedDict()

    # ---------- 列表 ----------
    def annots(self, ref: PageRef) -> list[A.AnnotInfo]:
        key = (ref.src, ref.pno)
        v = self._cache.get(key)
        if v is None:
            with ENGINE_LOCK:
                v = A.list_annots(self.store.page(ref.src, ref.pno))
            self._cache[key] = v
            while len(self._cache) > 2000:
                self._cache.popitem(last=False)
        return v

    def all_annots(self) -> list[tuple[int, PageRef, A.AnnotInfo]]:
        out = []
        for i, ref in enumerate(self.ws.refs):
            for a in self.annots(ref):
                out.append((i, ref, a))
        return out

    def clear_cache(self):
        self._cache.clear()

    def summary(self, entries) -> str:
        return A.summary_text([(i + 1, a) for i, _ref, a in entries])

    # ---------- 版本化修改 ----------
    def _apply(self, uid: int, fn, label: str):
        i = self.ws.index_of(uid)
        if i < 0:
            raise ValueError("页面已不在工作区中")
        ref = self.ws.refs[i]
        self.texts.check_editable(ref)
        pno, page = self.texts._new_version(ref)
        with ENGINE_LOCK:
            result = fn(page)
        self.ws.replace_at(i, replace(ref, src=GEN_SOURCE_ID, pno=pno), label)
        return result

    def add_markup(self, uid, kind, quads, style, text=""):
        return self._apply(uid, lambda p: A.add_markup(p, kind, quads, style, text),
                           f"添加{A.MARKUP_KINDS[kind]}")

    def add_note(self, uid, point, text, style):
        return self._apply(uid, lambda p: A.add_note(p, point, text, style), "添加便签")

    def add_freetext(self, uid, rect, text, style):
        return self._apply(uid, lambda p: A.add_freetext(p, rect, text, style), "添加文本框")

    def add_shape(self, uid, kind, points, style):
        return self._apply(uid, lambda p: A.add_shape(p, kind, points, style), f"添加{A.SHAPE_KINDS[kind]}")

    def add_ink(self, uid, strokes, style):
        return self._apply(uid, lambda p: A.add_ink(p, strokes, style), "添加画笔批注")

    def add_stamp(self, uid, rect, image, style, text=""):
        return self._apply(uid, lambda p: A.add_stamp(p, rect, image, style, text), "添加图章")

    def update(self, uid, aid, **kw):
        return self._apply(uid, lambda p: A.update_annot(p, aid, **kw), "修改批注")

    def move(self, uid, aid, dx, dy):
        return self._apply(uid, lambda p: A.move_annot(p, aid, dx, dy), "移动批注")

    def delete(self, uid, aid):
        return self._apply(uid, lambda p: A.delete_annot(p, aid), "删除批注")

    def redact(self, uid, rects):
        def fn(p):
            for r in rects:
                A.redact_area(p, r)
            return True
        res = self._apply(uid, fn, "涂黑")
        self.store.gen_redacted.add(self.ws.by_uid(uid).pno)
        return res

    def has_redactions(self, refs) -> bool:
        return any(r.src == GEN_SOURCE_ID and r.pno in self.store.gen_redacted for r in refs)
