"""核心引擎：源文件管理。

所有对 PyMuPDF 的访问都必须持有 ENGINE_LOCK（MuPDF 不支持多线程并发访问）。
导入的原文件只读打开，所有修改都发生在工作区或新生成的文档上。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import pymupdf as fitz

ENGINE_LOCK = threading.RLock()

GEN_SOURCE_ID = "__gen__"   # 内部生成页（区域页、空白页）所在的内存文档

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

# 来源色条用的颜色表（按导入顺序循环使用）
SOURCE_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
    "#f032e6", "#9a6324", "#469990", "#800000", "#808000", "#000075",
]


class ImportError_(Exception):
    """导入失败（带文件名和原因）。"""


class PasswordRequired(Exception):
    pass


@dataclass
class Source:
    id: str
    path: str | None          # 原文件路径；内部生成文档为 None
    name: str                 # 显示名（不含扩展名）
    color: str
    doc: fitz.Document
    password: str | None = None
    repaired: bool = False
    edit_locked: bool = False      # 带权限密码且不允许修改（7.4）
    page_types: list = field(default_factory=list)     # 自动识别结果
    type_overrides: dict = field(default_factory=dict)  # 用户手动更正 {pno: type}

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def page_type(self, pno: int) -> str | None:
        if pno in self.type_overrides:
            return self.type_overrides[pno]
        if pno < len(self.page_types):
            return self.page_types[pno]
        return None


class DocumentStore:
    """持有所有已导入的源文档。源文档只读，不会被保存回原路径。"""

    def __init__(self):
        self.sources: dict[str, Source] = {}
        self.gen_origins: dict[int, tuple[str, int]] = {}   # 生成页号 → (来源 id, 来源页号)
        self.gen_ocr: set[int] = set()                       # 已有 OCR 文字层的生成页
        self.gen_redacted: set[int] = set()                  # 做过涂黑的生成页（导出时清理隐私信息）
        self._counter = 0
        gen = fitz.open()
        self.sources[GEN_SOURCE_ID] = Source(GEN_SOURCE_ID, None, "生成页", "#888888", gen)

    # ---------- 导入 ----------
    def open_file(self, path: str, password: str | None = None, sid: str | None = None) -> Source:
        """打开 PDF 或图片。需要密码时抛 PasswordRequired；失败抛 ImportError_。"""
        name = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1].lower()
        with ENGINE_LOCK:
            try:
                if ext in IMAGE_EXTS:
                    doc = self._image_to_pdf(path)
                else:
                    doc = fitz.open(path, filetype="pdf")
            except Exception as e:  # noqa: BLE001
                raise ImportError_(f"{os.path.basename(path)}：无法解析（{e}）") from e
            if doc.needs_pass:
                if password is None:
                    doc.close()
                    raise PasswordRequired(path)
                if not doc.authenticate(password):
                    doc.close()
                    raise PasswordRequired(path)
            if doc.page_count == 0:
                doc.close()
                raise ImportError_(f"{os.path.basename(path)}：文件中没有页面")
            repaired = bool(getattr(doc, "is_repaired", False))
            # 先记录原始权限，再解密为内存副本
            # （只设权限密码的文件打开时无需密码，is_encrypted 为 False，故直接看权限位）
            edit_locked = not (doc.permissions & fitz.PDF_PERM_MODIFY)
            encrypted = bool((doc.metadata or {}).get("encryption"))
            # 加密文件转成解密后的内存副本，避免后续导出时仍受加密限制
            if encrypted or doc.is_encrypted or doc.needs_pass or password:
                data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
                doc.close()
                doc = fitz.open("pdf", data)
            if sid is None:
                self._counter += 1
                sid = f"s{self._counter}"
            elif sid[1:].isdigit():
                self._counter = max(self._counter, int(sid[1:]))
            num = int(sid[1:]) if sid[1:].isdigit() else 1
            color = SOURCE_COLORS[(num - 1) % len(SOURCE_COLORS)]
            src = Source(sid, os.path.abspath(path), name, color, doc,
                         password=password, repaired=repaired, edit_locked=edit_locked,
                         page_types=[None] * doc.page_count)
            self.sources[sid] = src
            return src

    @staticmethod
    def _image_to_pdf(path: str) -> fitz.Document:
        img = fitz.open(path)
        out = fitz.open()
        # TIFF 可能多帧，每帧一页
        for i in range(img.page_count):
            pdfbytes = img.convert_to_pdf(i, i + 1)
            out.insert_pdf(fitz.open("pdf", pdfbytes))
        img.close()
        return fitz.open("pdf", out.tobytes())

    def has_ocr(self, sid: str, pno: int) -> bool:
        return sid == GEN_SOURCE_ID and pno in self.gen_ocr

    def load_generated(self, data: bytes, origins: dict, ocr=(), redacted=()):
        """从项目文件恢复生成页文档。"""
        with ENGINE_LOCK:
            gen = self.gen
            gen.doc.close()
            gen.doc = fitz.open("pdf", data) if data else fitz.open()
            gen.page_types = [None] * gen.doc.page_count
            self.gen_origins = {int(k): tuple(v) for k, v in origins.items()}
            self.gen_ocr = {int(x) for x in ocr}
            self.gen_redacted = {int(x) for x in redacted}

    @property
    def gen(self) -> Source:
        return self.sources[GEN_SOURCE_ID]

    def get(self, sid: str) -> Source:
        return self.sources[sid]

    def page(self, sid: str, pno: int) -> fitz.Page:
        return self.sources[sid].doc[pno]

    def close_all(self):
        with ENGINE_LOCK:
            for s in self.sources.values():
                try:
                    s.doc.close()
                except Exception:  # noqa: BLE001
                    pass
            self.sources.clear()

    # ---------- 生成页 ----------
    def append_generated(self, tmp: fitz.Document) -> list[int]:
        """把临时文档的所有页追加到生成页文档，返回新页号列表。"""
        with ENGINE_LOCK:
            gen = self.gen
            start = gen.doc.page_count
            gen.doc.insert_pdf(tmp)
            gen.page_types.extend([None] * tmp.page_count)
            return list(range(start, gen.doc.page_count))

    def new_blank_page(self, width: float, height: float) -> int:
        with ENGINE_LOCK:
            tmp = fitz.open()
            tmp.new_page(width=width, height=height)
            pno = self.append_generated(tmp)[0]
            self.gen.page_types[pno] = "blank"
            return pno
