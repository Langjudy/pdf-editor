"""扫描件服务：OCR 文字层、图像预处理、区域擦除、恢复原图（技术要求第 8 节）。

- OCR 只增加不可见文字层、不改变页面外观：通过 Workspace.upgrade_page 提交，
  不占用撤销历史，撤销其他操作时也不会丢失识别结果；
- 预处理、区域擦除、恢复原图改变了页面外观：作为普通编辑提交，可撤销。

耗时的识别 / 图像处理方法都可在后台线程调用，提交工作区（commit_*）必须在界面线程执行。
"""
from __future__ import annotations

from dataclasses import replace

import pymupdf as fitz

from ..core import ocr as ocrmod
from ..core.engine import ENGINE_LOCK, GEN_SOURCE_ID, DocumentStore
from ..core.scan import PreOptions, load_page_image, preprocess, save_page_image
from ..core.scanedit import erase_area
from .exporter import origin_source
from .textservice import TextService
from .workspace import PageRef, Workspace


class ScanService:
    def __init__(self, store: DocumentStore, ws: Workspace, texts: TextService):
        self.store, self.ws, self.texts = store, ws, texts
        self.engine = ocrmod.OcrEngine()

    def _copy(self, src: str, pno: int) -> tuple[int, fitz.Page]:
        """复制 (src, pno) 为生成文档中的新版本。"""
        with ENGINE_LOCK:
            tmp = fitz.open()
            tmp.insert_pdf(self.store.get(src).doc, from_page=pno, to_page=pno)
            new = self.store.append_generated(tmp)[0]
            tmp.close()
            ref = PageRef(0, src, pno)
            self.store.gen_origins[new] = origin_source(self.store, ref)
            self.store.gen.page_types[new] = self.store.get(src).page_type(pno)
            return new, self.store.gen.doc[new]

    # ---------- OCR ----------
    def ocr_key(self, src: str, pno: int, cancelled=None) -> int | None:
        """识别 (src, pno) 页，返回带文字层的新版本页号（可在后台线程调用）。"""
        with ENGINE_LOCK:
            page = self.store.page(src, pno)
            img, px2pg = ocrmod.render_page(page)
        lines = ocrmod.ocr_image(img, px2pg, self.engine)        # 耗时，不持锁
        if cancelled and cancelled():
            return None
        new, page = self._copy(src, pno)
        with ENGINE_LOCK:
            ocrmod.remove_text_layer(page)       # 重新识别：先去掉旧文字层
            ocrmod.write_text_layer(page, lines)
            self.store.gen_ocr.add(new)
        return new

    def commit_ocr(self, src: str, pno: int, new_pno: int) -> int:
        self.texts._blocks.pop((GEN_SOURCE_ID, new_pno), None)
        return self.ws.upgrade_page(src, pno, GEN_SOURCE_ID, new_pno)

    # ---------- 预处理 ----------
    def preprocess_preview(self, ref: PageRef, opts: PreOptions):
        """返回 (原图, 处理后, 信息)；页面没有扫描底图时返回 None。"""
        with ENGINE_LOCK:
            pi = load_page_image(self.store.page(ref.src, ref.pno))
        if pi is None:
            return None
        out, info = preprocess(pi.arr, opts)
        return pi.arr, out, info

    def preprocess_pages(self, refs: list[PageRef], opts: PreOptions, progress=None):
        """处理多页，返回 ({工作区序号: 新 PageRef}, 跳过的页码列表, 角度列表)。在后台线程调用。"""
        changes, skipped, angles = {}, [], []
        for k, ref in enumerate(refs):
            if progress and not progress(k, len(refs), f"第 {k + 1} / {len(refs)} 页"):
                break
            i = self.ws.index_of(ref.uid)
            new, page = self._copy(ref.src, ref.pno)
            with ENGINE_LOCK:
                pi = load_page_image(page)
            if pi is None:
                skipped.append(i + 1)
                continue
            out, info = preprocess(pi.arr, opts)
            with ENGINE_LOCK:
                save_page_image(page, pi, out)
                # 图像已变化（纠偏会移动文字位置），旧文字层作废
                ocrmod.remove_text_layer(page)
            angles.append(info["angle"])
            changes[i] = replace(ref, src=GEN_SOURCE_ID, pno=new)
        return changes, skipped, angles

    # ---------- 区域擦除 ----------
    def erase(self, uid: int, rect: fitz.Rect) -> str:
        i = self.ws.index_of(uid)
        ref = self.ws.refs[i]
        self.texts.check_editable(ref)
        new, page = self._copy(ref.src, ref.pno)
        if self.store.has_ocr(ref.src, ref.pno):
            self.store.gen_ocr.add(new)
        with ENGINE_LOCK:
            method = erase_area(page, rect)
        self.ws.replace_at(i, replace(ref, src=GEN_SOURCE_ID, pno=new), "擦除区域")
        return method

    # ---------- 恢复原图 ----------
    def restore_original(self, uids) -> int:
        """把页面恢复为导入时的原始页面（撤销所有扫描修改、文字编辑与 OCR 文字层）。可撤销。"""
        changes = {}
        for uid in uids:
            i = self.ws.index_of(uid)
            ref = self.ws.refs[i]
            if ref.src != GEN_SOURCE_ID or ref.kind != "page":
                continue
            sid, pno = origin_source(self.store, ref)
            if sid != GEN_SOURCE_ID:
                changes[i] = replace(ref, src=sid, pno=pno)
        if changes:
            self.ws.replace_many(changes, "恢复原始页面")
        return len(changes)
