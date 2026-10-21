"""OCR 识别与透明文字层（技术要求 8.2）。

- 识别引擎：RapidOCR（PaddleOCR 模型的 ONNX 封装），纯 CPU；中文简体、英文、数字混排，
  模型本身也能识别繁体；
- 透明文字层：逐字放在识别出的字符框位置（render mode 3，不可见），页面外观不变，
  可搜索、可复制；
- 重新识别 / 校对时只删除透明文字，不影响页面上原有的可见文字。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

import numpy as np
import pymupdf as fitz

from .engine import ENGINE_LOCK
from .render import display_matrix, source_to_display

OCR_DPI = 300
MAX_SIDE = 7000            # 大幅面页面限制像素，避免内存暴涨
MIN_SCORE = 0.5


@dataclass
class OcrLine:
    text: str
    quad: list                      # 4 个顶点（原始页面坐标）
    score: float
    chars: list = field(default_factory=list)   # [(字符, fitz.Quad)]，与 text 一一对应

    @property
    def rect(self) -> fitz.Rect:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    @property
    def angle(self) -> float:
        (x0, y0), (x1, y1) = self.quad[0], self.quad[1]
        return math.degrees(math.atan2(y1 - y0, x1 - x0))


class OcrEngine:
    """延迟加载的识别引擎（首次使用约 1 秒）。ONNX 推理会释放 GIL，可在后台线程运行。"""

    def __init__(self):
        self._eng = None
        self._lock = threading.Lock()

    def _get(self):
        if self._eng is None:
            from rapidocr_onnxruntime import RapidOCR
            self._eng = RapidOCR()
        return self._eng

    def recognize(self, img: np.ndarray) -> list:
        with self._lock:
            res, _ = self._get()(img, return_word_box=True)
        return res or []


def render_page(page: fitz.Page, dpi: int = OCR_DPI) -> tuple[np.ndarray, fitz.Matrix]:
    """把页面渲染成 RGB 图像，返回 (图像, 像素→原始页面坐标 的矩阵)。"""
    with ENGINE_LOCK:
        long_side = max(page.rect.width, page.rect.height) * dpi / 72.0
        scale = dpi / 72.0 * min(1.0, MAX_SIDE / long_side)
        pix = page.get_pixmap(matrix=display_matrix(page, 0, scale), alpha=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).copy()
        to_px = source_to_display(page, 0, scale)
    return img, ~to_px


def ocr_page(page: fitz.Page, engine: OcrEngine, dpi: int = OCR_DPI,
             min_score: float = MIN_SCORE) -> list[OcrLine]:
    img, px2pg = render_page(page, dpi)
    return ocr_image(img, px2pg, engine, min_score)


def ocr_image(img: np.ndarray, px2pg: fitz.Matrix, engine: OcrEngine,
              min_score: float = MIN_SCORE) -> list[OcrLine]:
    def pt(p):
        q = fitz.Point(p[0], p[1]) * px2pg
        return (q.x, q.y)

    lines = []
    for r in engine.recognize(img):
        box, text, score = r[0], r[1], float(r[2])
        if score < min_score or not text.strip():
            continue
        chars = []
        if len(r) >= 5 and len(r[3]) == len(r[4]):
            for cb, ch in zip(r[3], r[4]):
                chars.append((ch, _quad([pt(p) for p in cb])))
            text = "".join(c for c, _ in chars)
        lines.append(OcrLine(text, [pt(p) for p in box], score, chars))
    # 按阅读顺序排序：先上后下，同一行从左到右
    lines.sort(key=lambda l: (round(l.rect.y0 / max(l.rect.height, 1)), l.rect.x0))
    return lines


def _quad(pts) -> fitz.Quad:
    """RapidOCR 的四点为顺时针（左上、右上、右下、左下），fitz.Quad 需要（左上、右上、左下、右下）。"""
    p = [fitz.Point(*q) for q in pts]
    return fitz.Quad(p[0], p[1], p[3], p[2])


# ================================================================ 透明文字层
_font_cache = {}


def _layer_font() -> fitz.Font:
    f = _font_cache.get("cjk")
    if f is None:
        f = fitz.Font("cjk")      # 内置中文字体（Droid Sans Fallback），覆盖中英文
        _font_cache["cjk"] = f
    return f


def _char_size(q: fitz.Quad) -> float:
    # OCR 框比字形高约 25%（检测框有外扩），字号取框高的 0.8
    return max(1.0, abs(q.ll - q.ul) * 0.8)


def write_text_layer(page: fitz.Page, lines: list[OcrLine]):
    """在页面上写入不可见文字层（逐字定位）。"""
    if not lines:
        return
    with ENGINE_LOCK:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        W, H = page.rect.width, page.rect.height
        tmp = fitz.open()
        tp = tmp.new_page(width=W, height=H)
        font = _layer_font()
        for ln in lines:
            tw = fitz.TextWriter(tp.rect)
            ang = ln.angle
            if ln.chars:
                for ch, q in ln.chars:
                    if ch.isspace():
                        continue
                    size = _char_size(q)
                    # 每个字放在字符框左下角（基线略高于框底）
                    base = q.ll + (q.ul - q.ll) * 0.12
                    tw.append(base, ch, font=font, fontsize=size)
                    # 宽度按字符框横向缩放：通过 morph 实现太贵，这里依靠逐字定位已足够准确
            else:
                q = _quad(ln.quad)
                size = _char_size(q)
                tw.append(q.ll + (q.ul - q.ll) * 0.12, ln.text, font=font, fontsize=size)
            morph = None
            # 逐字放置时位置已随倾斜排布，不再整体旋转；整行放置时才按行角度旋转
            if not ln.chars and abs(ang) > 0.5:
                o = fitz.Point(*ln.quad[3])
                morph = (o, fitz.Matrix(-ang))
            tw.write_text(tp, render_mode=3, morph=morph)
        tmp.subset_fonts()
        tmp = fitz.open("pdf", tmp.tobytes(garbage=3, deflate=True))
        page.show_pdf_page(page.rect, tmp, 0)
        tmp.close()
        if rot:
            page.set_rotation(rot)


def invisible_char_rects(page: fitz.Page, within: fitz.Rect | None = None) -> list[fitz.Rect]:
    """页面上不可见（OCR 层）字符的位置（原始坐标）。"""
    out = []
    with ENGINE_LOCK:
        for span in page.get_texttrace():
            if span.get("type") != 3:
                continue
            for c in span["chars"]:
                r = fitz.Rect(c[3])
                if r.width <= 0.01:          # 零宽字形框：以起点为左边、按字号补出宽度
                    o = c[2]
                    r = fitz.Rect(o[0], r.y0, o[0] + max(r.height * 0.5, 1.0), r.y1)
                if r.is_empty:
                    continue
                if within is None or r.intersects(within):
                    out.append(r)
    return out


def remove_text_layer(page: fitz.Page, within: fitz.Rect | None = None) -> int:
    """删除不可见文字（可限定区域），可见文字不受影响。返回删除的字符数。"""
    with ENGINE_LOCK:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        try:
            rects = invisible_char_rects(page, within)
            for r in rects:
                d = r.height * 0.25
                page.add_redact_annot(r + (-0.4, d, 0.4, -d), fill=False)
            if rects:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
        finally:
            if rot:
                page.set_rotation(rot)
    return len(rects)


def has_text_layer(page: fitz.Page) -> bool:
    with ENGINE_LOCK:
        return any(s.get("type") == 3 for s in page.get_texttrace())
