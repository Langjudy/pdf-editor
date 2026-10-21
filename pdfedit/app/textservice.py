"""文字编辑服务：写时复制的页面版本管理 + 编辑 / 删除 / 新增 / 查找替换。

每次编辑都把当前页面完整复制为生成文档中的一个新版本，在新版本上修改，
再让工作区中的该页（uid 不变）指向新版本。于是：
- 原文件始终只读；
- 撤销 / 重做沿用工作区快照机制，自然覆盖文字编辑（F-PG-31）；
- 页面版本一旦生成不再修改，文字块缓存永久有效。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace

import pymupdf as fitz

from ..core import pagetype
from ..core.engine import ENGINE_LOCK, GEN_SOURCE_ID, DocumentStore
from ..core.fonts import FontManager
from ..core.scanedit import ScanEditOptions, ScanEditResult, scan_edit_block
from ..core.textedit import (EditResult, Match, TBlock, TextStyle, add_text, compile_pattern,
                             delete_block, edit_block, extract_blocks, find_in_blocks,
                             replace_matches)
from .exporter import origin_source
from .workspace import PageRef, Workspace

MAX_BLOCK_CACHE = 300


class EditLocked(Exception):
    """文档带编辑权限密码，需先输入权限密码。"""

    def __init__(self, sid: str):
        super().__init__("文档受权限密码保护，不允许编辑")
        self.sid = sid


@dataclass
class TextHit:
    index: int        # 工作区序号
    uid: int
    match: Match

    @property
    def rect(self):
        return self.match.rect

    @property
    def context(self):
        return self.match.context


class TextService:
    def __init__(self, store: DocumentStore, ws: Workspace, fonts: FontManager):
        self.store, self.ws, self.fonts = store, ws, fonts
        self._blocks: OrderedDict = OrderedDict()

    # ---------- 文字块 ----------
    @staticmethod
    def page_blocks(page: fitz.Page, scan: bool) -> list[TBlock]:
        """扫描页按“一行识别结果”拆成独立文字块，矢量页按段落。"""
        with ENGINE_LOCK:
            blocks = extract_blocks(page)
        if not scan:
            return blocks
        from ..core.ocr import invisible_char_rects
        inv = invisible_char_rects(page)
        out = []
        for b in blocks:
            for ln in b.lines:
                # 区分 OCR 透明层（字在图片里）与可见文字（例如之前以矢量方式写入的修改）
                hits = 0
                for ch in ln.chars:
                    if ch.c.isspace():
                        continue
                    r = fitz.Rect(ch.bbox)
                    c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                    hits += 1 if any(c in q for q in inv) else -1
                ln.invisible = hits > 0
                out.append(TBlock(len(out), ln.bbox, [ln]))
        return out

    def is_scan_block(self, ref: PageRef, block: TBlock) -> bool:
        """该文字块需要“擦除图像 + 重写”：扫描页上的 OCR 识别行。可见文字按矢量文字处理。"""
        return self.is_scan(ref) and bool(block.lines) and block.lines[0].invisible

    def blocks(self, ref: PageRef) -> list[TBlock]:
        key = (ref.src, ref.pno)
        b = self._blocks.get(key)
        if b is None:
            with ENGINE_LOCK:
                b = self.page_blocks(self.store.page(ref.src, ref.pno), self.is_scan(ref))
            self._blocks[key] = b
            while len(self._blocks) > MAX_BLOCK_CACHE:
                self._blocks.popitem(last=False)
        else:
            self._blocks.move_to_end(key)
        return b

    def clear_cache(self):
        self._blocks.clear()

    def page_type(self, ref: PageRef) -> str | None:
        if ref.kind == "region":
            return pagetype.REGION
        return self.store.get(ref.src).page_type(ref.pno)

    def is_scan(self, ref: PageRef) -> bool:
        """扫描页（含从扫描页裁出的区域页）：文字修改走“擦除 + 原位重写”。"""
        t = self.page_type(ref)
        if t == pagetype.REGION:
            sid, pno = origin_source(self.store, ref)
            t = self.store.get(sid).page_type(pno) if sid in self.store.sources else None
        return t == pagetype.SCAN

    def has_ocr(self, ref: PageRef) -> bool:
        return self.store.has_ocr(ref.src, ref.pno) or (self.is_scan(ref) and bool(self.blocks(ref)))

    # ---------- 权限 ----------
    def check_editable(self, ref: PageRef):
        sid, _ = origin_source(self.store, ref)
        src = self.store.sources.get(sid)
        if src is not None and src.edit_locked:
            raise EditLocked(sid)

    def unlock(self, sid: str, password: str) -> bool:
        """校验权限（所有者）密码。"""
        src = self.store.get(sid)
        if not src.path:
            return False
        with ENGINE_LOCK:
            d = fitz.open(src.path)
            try:
                rc = d.authenticate(password)
            finally:
                d.close()
        if rc & 4:
            src.edit_locked = False
            return True
        return False

    # ---------- 版本 ----------
    def _new_version(self, ref: PageRef) -> tuple[int, fitz.Page]:
        with ENGINE_LOCK:
            tmp = fitz.open()
            tmp.insert_pdf(self.store.get(ref.src).doc, from_page=ref.pno, to_page=ref.pno)
            pno = self.store.append_generated(tmp)[0]
            tmp.close()
            self.store.gen_origins[pno] = origin_source(self.store, ref)
            self.store.gen.page_types[pno] = self.store.get(ref.src).page_type(ref.pno)
            if self.store.has_ocr(ref.src, ref.pno):
                self.store.gen_ocr.add(pno)
            if ref.src == GEN_SOURCE_ID and ref.pno in self.store.gen_redacted:
                self.store.gen_redacted.add(pno)
            return pno, self.store.gen.doc[pno]

    def _ref(self, uid: int) -> tuple[int, PageRef]:
        i = self.ws.index_of(uid)
        if i < 0:
            raise ValueError("页面已不在工作区中")
        return i, self.ws.refs[i]

    @staticmethod
    def _find_block(blocks: list[TBlock], old: TBlock) -> TBlock:
        """在新版本页面中找到与旧块对应的块（复制后结构一致，按序号 + 文本校验）。"""
        if old.id < len(blocks) and blocks[old.id].text == old.text:
            return blocks[old.id]
        for b in blocks:
            if b.text == old.text and fitz.Rect(b.bbox).intersects(fitz.Rect(old.bbox)):
                return b
        raise ValueError("找不到对应的文字块")

    # ---------- 编辑操作 ----------
    def edit(self, uid: int, block_id: int, text: str, style: TextStyle,
             offset: tuple = (0.0, 0.0), label: str = "修改文字",
             scan_opts: ScanEditOptions | None = None) -> EditResult | ScanEditResult:
        """修改文字块。扫描页：擦除原字并原位重写（或仅校对文字层），见 scan_opts。"""
        i, ref = self._ref(uid)
        self.check_editable(ref)
        old = self.blocks(ref)[block_id]
        page_scan = self.is_scan(ref)
        scan = self.is_scan_block(ref, old)
        pno, page = self._new_version(ref)
        with ENGINE_LOCK:
            blk = self._find_block(self.page_blocks(page, page_scan), old)
            if scan:
                res = scan_edit_block(page, blk, text, self.fonts, scan_opts or ScanEditOptions(), offset)
            else:
                res = edit_block(page, blk, text, style, self.fonts, offset)
        self.ws.replace_at(i, replace(ref, src=GEN_SOURCE_ID, pno=pno), label)
        return res

    def delete(self, uid: int, block_id: int):
        i, ref = self._ref(uid)
        self.check_editable(ref)
        old = self.blocks(ref)[block_id]
        page_scan = self.is_scan(ref)
        scan = self.is_scan_block(ref, old)
        pno, page = self._new_version(ref)
        with ENGINE_LOCK:
            blk = self._find_block(self.page_blocks(page, page_scan), old)
            if scan:
                scan_edit_block(page, blk, "", self.fonts)
            else:
                delete_block(page, blk)
        self.ws.replace_at(i, replace(ref, src=GEN_SOURCE_ID, pno=pno), "删除文字")

    def add(self, uid: int, origin: tuple, text: str, style: TextStyle) -> EditResult:
        i, ref = self._ref(uid)
        self.check_editable(ref)
        pno, page = self._new_version(ref)
        with ENGINE_LOCK:
            res = add_text(page, origin, text, style, self.fonts)
        self.ws.replace_at(i, replace(ref, src=GEN_SOURCE_ID, pno=pno), "添加文字")
        return res

    # ---------- 查找替换 ----------
    def find(self, items, needle: str, case=False, whole=False, regex=False,
             cancelled=None, limit: int = 5000) -> list[TextHit]:
        """items: [(工作区序号, PageRef)]。"""
        pat = compile_pattern(needle, case, whole, regex)
        hits: list[TextHit] = []
        for idx, ref in items:
            if cancelled and cancelled():
                break
            for m in find_in_blocks(self.blocks(ref), pat):
                hits.append(TextHit(idx, ref.uid, m))
            if len(hits) >= limit:
                break
        return hits

    def replace(self, hits: list[TextHit], needle: str, replacement: str, case=False, whole=False,
                regex=False, progress=None, commit: bool = True,
                scan_opts: ScanEditOptions | None = None):
        """替换选定的命中项。所有页面的替换合并为一步撤销。

        commit=False 时只生成新版本页面，返回 (数量, 替代字体, 变更)，由调用方在界面线程提交。
        """
        pat = compile_pattern(needle, case, whole, regex)
        by_uid: dict[int, list[Match]] = {}
        for h in hits:
            by_uid.setdefault(h.uid, []).append(h.match)
        for uid in by_uid:
            self.check_editable(self._ref(uid)[1])
        changes: dict[int, PageRef] = {}
        total, subs = 0, set()
        for k, (uid, matches) in enumerate(by_uid.items()):
            if progress and not progress(k, len(by_uid), ""):
                break
            i, ref = self._ref(uid)
            old_blocks = self.blocks(ref)
            scan = self.is_scan(ref)
            pno, page = self._new_version(ref)
            with ENGINE_LOCK:
                if scan:
                    n = self._replace_scan(page, old_blocks, matches, pat, replacement, regex, scan_opts)
                    s = []
                else:
                    n, s = replace_matches(page, old_blocks, matches, pat, replacement, regex, self.fonts)
            total += n
            subs.update(s)
            changes[i] = replace(ref, src=GEN_SOURCE_ID, pno=pno)
        if not commit:
            return total, sorted(subs), changes
        if changes:
            self.ws.replace_many(changes, f"替换 {total} 处")
        return total, sorted(subs)

    def _replace_scan(self, page, old_blocks, matches, pat, replacement, regex, scan_opts) -> int:
        """扫描页替换：每个受影响的识别行擦除原字后原位重写。"""
        blocks = self.page_blocks(page, True)
        by_block: dict[int, set] = {}
        for m in matches:
            by_block.setdefault(m.block, set()).add((m.start, m.end))
        count = 0
        for bi, wanted in by_block.items():
            blk = self._find_block(blocks, old_blocks[bi])
            text = blk.lines[0].text
            pieces, last = [], 0
            for m in pat.finditer(text):
                if (m.start(), m.end()) not in wanted:
                    continue
                pieces.append(text[last:m.start()])
                pieces.append(m.expand(replacement) if regex else replacement)
                last = m.end()
                count += 1
            pieces.append(text[last:])
            if blk.lines[0].invisible:
                scan_edit_block(page, blk, "".join(pieces), self.fonts, scan_opts or ScanEditOptions())
            else:          # 之前以矢量方式写入的可见文字
                st = blk.style()
                st.reflow = False
                edit_block(page, blk, "".join(pieces), st, self.fonts)
        return count
