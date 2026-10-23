"""扫描页覆盖式文字编辑（技术要求 8.3）。

流程：取该行 OCR 结果 → 估算原文字字号、颜色、字体风格（宋/黑/仿宋/楷四类匹配）
→ 擦除原文字（纯色背景填色，复杂背景图像修复并保留表格线）
→ 在原位置写入新文字：矢量文字（清晰、可搜索）或渲染进图片（与原图风格一致）。

覆盖式编辑在字体和墨色上无法做到与原件完全一致，近看可分辨修改痕迹（8.4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
import pymupdf as fitz

from .engine import ENGINE_LOCK
from .fonts import FontManager, ResolvedFont
from .ocr import OcrLine, remove_text_layer, write_text_layer
from .scan import (PageImage, composite_rgba, encode_image, erase_text, line_strokes, load_page_image,
                   render_region, save_page_image)
from .textedit import LineOut, TBlock, TextStyle, write_overlay

# 字体风格候选：显示名 → 系统字体族名
SCAN_FONTS = {   # 风格 → 各平台候选字体族名（取系统中第一个存在的）
    "宋体": ["SimSun", "Songti SC", "STSong", "Noto Serif CJK SC", "Source Han Serif SC", "AR PL UMing CN"],
    "黑体": ["SimHei", "Heiti SC", "STHeiti", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC",
           "WenQuanYi Zen Hei"],
    "仿宋": ["FangSong", "STFangsong", "FangSong_GB2312"],
    "楷体": ["KaiTi", "Kaiti SC", "STKaiti", "AR PL UKai CN"],
}


def scan_family(fm: FontManager, label: str) -> str:
    """某种字体风格在本机可用的字体族名；都没有时返回第一个候选（将回退到内置中文字体）。"""
    for name in SCAN_FONTS[label]:
        if fm.find(name):
            return name
    return SCAN_FONTS[label][0]


@dataclass
class ScanStyle:
    family: str                 # 系统字体族名
    label: str                  # 宋体 / 黑体 / 仿宋 / 楷体
    size: float                 # pt
    color: tuple                # RGB 0~1
    origin: tuple               # 第一个字的基线起点（原始页面坐标）
    scores: dict = field(default_factory=dict)


@dataclass
class ScanEditOptions:
    mode: str = "vector"            # vector：矢量文字 | image：渲染进图片
    texture: bool = False           # 渲染进图片时模拟扫描质感
    proofread_only: bool = False    # 只校对文字层，不改图像
    family: str | None = None       # 覆盖自动识别的字体（系统族名）
    size: float | None = None
    color: tuple | None = None
    force: bool = False             # 文字未变也重写（改样式、移动时）


@dataclass
class ScanEditResult:
    style: ScanStyle | None
    erase_methods: list
    font_name: str = ""


# ================================================================ 文字渲染
def _font(fm: FontManager, family: str) -> tuple[fitz.Font, str]:
    e = fm.find(family)
    if e is None:
        with ENGINE_LOCK:
            return fitz.Font("cjk"), "内置中文字体"
    return fm.load(e), e.display


def render_text_rgba(text: str, font: fitz.Font, size: float, color: tuple, scale: float):
    """把一行文字渲染成透明底 RGBA 图像。返回 (图像, 基线原点在图像中的像素位置)。"""
    w = font.text_length(text, fontsize=size) + size
    h = size * 2.0
    m = size * 0.5
    with ENGINE_LOCK:
        doc = fitz.open()
        pg = doc.new_page(width=w + 2 * m, height=h)
        origin = fitz.Point(m, size * 1.3)
        tw = fitz.TextWriter(pg.rect)
        tw.append(origin, text, font=font, fontsize=size)
        tw.write_text(pg, color=color)
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
        rgba = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 4).copy()
        doc.close()
    return rgba, (origin.x * scale, origin.y * scale)


def _ink_box(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# ================================================================ 样式估算
def estimate_style(pi: PageImage, rect: fitz.Rect, text: str, fm: FontManager) -> ScanStyle | None:
    """估算一行扫描文字的字号、颜色、字体风格与基线位置。"""
    bx0_, by0_, bx1_, by1_ = pi.page_to_px(rect)
    if bx1_ - bx0_ < 4 or by1_ - by0_ < 4:
        return None
    # 外扩裁剪，容纳超出行框的笔画；只取属于这一行的笔画
    pad = int((by1_ - by0_) * 0.5)
    H, W = pi.arr.shape[:2]
    x0, y0 = max(0, bx0_ - pad), max(0, by0_ - pad)
    x1, y1 = min(W, bx1_ + pad), min(H, by1_ + pad)
    crop = pi.arr[y0:y1, x0:x1]
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    ink, _lines = line_strokes(gray, (bx0_ - x0, by0_ - y0, bx1_ - x0, by1_ - y0), pi.dpi)
    box = _ink_box(ink)
    if box is None:
        return None
    bx0, by0, bx1, by1 = box
    ink_h, ink_w = by1 - by0, bx1 - bx0
    # 颜色：墨迹像素的中位色
    # 墨色取笔画中最深的 40% 像素：扫描模糊使笔画边缘偏灰，全体中位数会偏浅
    pixels = crop[ink > 0]
    lum = pixels if crop.ndim == 2 else pixels.astype(np.float32) @ np.array([0.299, 0.587, 0.114])
    core = pixels[lum <= np.percentile(lum, 40)]
    if len(core) == 0:
        core = pixels
    color = tuple(float(v) / 255 for v in (np.median(core, axis=0) if crop.ndim == 3
                                           else [np.median(core)] * 3))
    px_per_pt = pi.dpi / 72.0
    target = ink[by0:by1, bx0:bx1]
    sample = text.strip() or "字"
    best = None
    scores = {}
    for label in SCAN_FONTS:
        family = scan_family(fm, label)
        font, _ = _font(fm, family)
        # 先按 100pt 渲染测量墨迹高度比例，换算出字号
        ref, _o = render_text_rgba(sample, font, 100.0, (0, 0, 0), 1.0)
        rb = _ink_box(ref[:, :, 3] > 100)
        if rb is None:
            continue
        ratio_h = (rb[3] - rb[1]) / 100.0
        ratio_w = (rb[2] - rb[0]) / 100.0
        size_h = ink_h / px_per_pt / max(ratio_h, 1e-3)
        size_w = ink_w / px_per_pt / max(ratio_w, 1e-3)
        # 高度对单行文字最可靠；宽度用于交叉校验（OCR 文字正确时两者应接近）
        size = size_h if abs(size_w - size_h) / size_h > 0.25 else (size_h * 0.6 + size_w * 0.4)
        rgba, (ox, oy) = render_text_rgba(sample, font, size, (0, 0, 0), px_per_pt)
        m = (rgba[:, :, 3] > 100).astype(np.uint8) * 255
        mb = _ink_box(m)
        if mb is None:
            continue
        cand = cv2.resize(m[mb[1]:mb[3], mb[0]:mb[2]], (target.shape[1], target.shape[0]),
                          interpolation=cv2.INTER_AREA)
        # 相似度：先按笔画尺度模糊再求相关（对字间距、扫描模糊不敏感），
        # 再用墨迹密度差惩罚（黑体笔画粗、密度高，宋体 / 仿宋细）
        sig = max(1.0, ink_h / 25.0)
        A = cv2.GaussianBlur(target.astype(np.float32), (0, 0), sig).ravel()
        B = cv2.GaussianBlur(cand.astype(np.float32), (0, 0), sig).ravel()
        corr = float(np.corrcoef(A, B)[0, 1]) if A.std() > 0 and B.std() > 0 else 0.0
        dt, dc = (target > 0).mean(), (cand > 127).mean()
        dens = min(1.0, abs(dt - dc) / max(dt, 1e-3))
        score = corr * (1 - 0.5 * dens)
        scores[label] = round(score, 3)
        # 基线原点：让渲染出的墨迹左上角与原墨迹左上角对齐
        org_px = (x0 + bx0 - (mb[0] - ox), y0 + by0 - (mb[1] - oy))
        if best is None or score > best[0]:
            best = (score, label, family, size, org_px)
    if best is None:
        return None
    _s, label, family, size, org_px = best
    o = pi.px_to_page(*org_px)
    return ScanStyle(family, label, round(size, 1), color, (o.x, o.y), scores)


# ================================================================ 编辑
def _page_prepare(page):
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    return rot


def _write_line(page: fitz.Page, pi: PageImage, arr: np.ndarray, text: str, st: ScanStyle,
                opts: ScanEditOptions, fm: FontManager, origin: tuple):
    """写入一行新文字；返回 (新图像, 新文字的页面矩形, 字体名, 是否需要随后写矢量)。"""
    family = opts.family or st.family
    size = opts.size or st.size
    color = opts.color or st.color
    font, name = _font(fm, family)
    px_per_pt = pi.dpi / 72.0
    width_pt = font.text_length(text, fontsize=size)
    new_rect = fitz.Rect(origin[0], origin[1] - size * 0.88, origin[0] + width_pt, origin[1] + size * 0.12)
    if opts.mode == "image":
        rgba, (ox, oy) = render_text_rgba(text, font, size, color, px_per_pt)
        p = fitz.Point(*origin) * ~pi.matrix
        arr = composite_rgba(arr, rgba, int(round(p.x - ox)), int(round(p.y - oy)), opts.texture)
    return arr, new_rect, name, font


def scan_edit_block(page: fitz.Page, block: TBlock, new_text: str, fm: FontManager,
                    opts: ScanEditOptions | None = None, offset: tuple = (0.0, 0.0)) -> ScanEditResult:
    """修改 / 删除 / 移动扫描页上的一个文字块（文字块来自 OCR 文字层）。"""
    opts = opts or ScanEditOptions()
    rot = _page_prepare(page)
    try:
        pi = load_page_image(page)
        new_lines = new_text.split("\n") if new_text.strip() else []
        old = block.lines
        if opts.proofread_only:
            remove_text_layer(page, fitz.Rect(block.bbox) + (-1, -1, 1, 1))
            write_text_layer(page, _proof_lines(old, new_lines))
            return ScanEditResult(None, [])
        moved = abs(offset[0]) > 0.01 or abs(offset[1]) > 0.01
        same_count = len(new_lines) == len(old)
        todo = []          # (旧行 or None, 新文字, 序号)
        if same_count:
            for k, (ln, t) in enumerate(zip(old, new_lines)):
                if moved or opts.force or t != ln.text:
                    todo.append((ln, t, k))
        else:
            todo = [(ln, None, k) for k, ln in enumerate(old)]
        if pi is None:
            # 没有整页底图（扫描图像切块存放、CAD 曲线文字等）：删矢量 + 渲染擦除补丁
            return _patch_edit(page, old, new_lines, todo, same_count, fm, opts, offset)
        arr = pi.arr
        # 1. 先估算样式（擦除前），再擦除
        styles = {}
        methods = []
        for ln, _t, k in todo:
            st = estimate_style(pi, fitz.Rect(ln.bbox), ln.text, fm)
            if st is None:
                st = ScanStyle("SimSun", "宋体", ln.size, (0, 0, 0), ln.origin)
            styles[k] = st
            r = fitz.Rect(ln.bbox)
            box = pi.page_to_px(r)
            arr, info = erase_text(arr, box, pi.dpi)
            methods.append(info.method)
            remove_text_layer(page, r + (-1, -1, 1, 1))
        # 2. 写入新文字
        writes = []        # (文字, 原点, ScanStyle)
        if same_count:
            for ln, t, k in todo:
                if t.strip():
                    st = styles[k]
                    writes.append((t, (st.origin[0] + offset[0], st.origin[1] + offset[1]), st))
        elif new_lines:
            st0 = styles.get(0) or next(iter(styles.values()))
            pitch = _pitch(old, st0.size)
            for k, t in enumerate(new_lines):
                if t.strip():
                    st = styles.get(k, st0)
                    writes.append((t, (st0.origin[0] + offset[0], st0.origin[1] + offset[1] + k * pitch), st))
        font_name = ""
        layer = []
        vector = []
        for t, origin, st in writes:
            arr, rect, font_name, font = _write_line(page, pi, arr, t, st, opts, fm, origin)
            if opts.mode == "image":
                layer.append(OcrLine(t, [(rect.x0, rect.y0), (rect.x1, rect.y0), (rect.x1, rect.y1),
                                         (rect.x0, rect.y1)], 1.0))
            else:
                vector.append((t, origin, st, font, font_name))
        save_page_image(page, pi, arr)
        for t, origin, st, font, name in vector:
            style = TextStyle(font=None, size=opts.size or st.size, color=opts.color or st.color)
            write_overlay(page, [LineOut(t, origin, (1.0, 0.0))], ResolvedFont(font, name, False, False), style)
        if layer:
            write_text_layer(page, layer)
        first = styles.get(0) or (next(iter(styles.values())) if styles else None)
        return ScanEditResult(first, methods, font_name)
    finally:
        if rot:
            page.set_rotation(rot)


def _remove_vector_glyphs(page: fitz.Page, rect: fitz.Rect):
    """删除完全落在文字行框内的矢量线条（CAD 用线条画出的“曲线文字”）与该处的文字。

    只删“被框完全覆盖”的路径：穿过文字附近的尺寸线、边框等较长的线条不受影响。
    """
    with ENGINE_LOCK:
        page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)


def _insert_patch(page: fitz.Page, pi: PageImage, arr: np.ndarray, box: tuple):
    """把渲染图中 box（像素）这一块作为图片补丁贴回页面原位。"""
    x0, y0, x1, y1 = box
    if x1 - x0 < 1 or y1 - y0 < 1:
        return
    crop = np.ascontiguousarray(arr[y0:y1, x0:x1])
    a = pi.px_to_page(x0, y0)
    b = pi.px_to_page(x1, y1)
    with ENGINE_LOCK:
        page.insert_image(fitz.Rect(a, b), stream=encode_image(crop, prefer_jpeg=_is_photo(crop)))


def _is_photo(crop: np.ndarray) -> bool:
    """颜色丰富（扫描件、照片）用 JPEG，近似纯色的矢量页底色用 PNG。"""
    small = crop[::4, ::4].reshape(-1, crop.shape[2] if crop.ndim == 3 else 1)
    return len(np.unique(small // 8, axis=0)) > 64


def _patch_edit(page: fitz.Page, old, new_lines, todo, same_count, fm: FontManager,
                opts: ScanEditOptions, offset) -> ScanEditResult:
    """没有整页底图时的覆盖式编辑。

    1. 在渲染图上估算原文字样式；
    2. 删除完全落在行框内的矢量线条（曲线文字）与 OCR 文字层；
    3. 重新渲染该区域，若仍有字迹残留（字在切块图片里），用背景擦除算法处理后作为图片补丁贴回；
    4. 在原位写入新文字（矢量；或“渲染进图片”时画进补丁）。
    """
    if not todo:
        return ScanEditResult(None, [])
    area = fitz.Rect(todo[0][0].bbox)
    for ln, _t, _k in todo:
        area |= fitz.Rect(ln.bbox)
    h = max(ln.bbox[3] - ln.bbox[1] for ln, _t, _k in todo)
    area = area + (-2 * h, -2 * h, 2 * h, 2 * h)
    before = render_region(page, area)
    styles = {}
    for ln, _t, k in todo:
        st = estimate_style(before, fitz.Rect(ln.bbox), ln.text, fm)
        styles[k] = st or ScanStyle("SimSun", "宋体", ln.size, (0, 0, 0), ln.origin)
    for ln, _t, _k in todo:
        r = fitz.Rect(ln.bbox) + (-1, -1, 1, 1)
        remove_text_layer(page, r)
        _remove_vector_glyphs(page, r)
    after = render_region(page, area)
    arr = after.arr
    methods = []
    for ln, _t, _k in todo:
        box = after.page_to_px(fitz.Rect(ln.bbox))
        new, info = erase_text(arr, box, after.dpi)
        diff = np.any(new != arr, axis=2) if new.ndim == 3 else new != arr
        if diff.sum() > 20:                          # 仍有字迹（图片中的字）：贴擦除补丁
            ys, xs = np.nonzero(diff)
            pad = 2
            pbox = (max(0, xs.min() - pad), max(0, ys.min() - pad),
                    min(arr.shape[1], xs.max() + 1 + pad), min(arr.shape[0], ys.max() + 1 + pad))
            _insert_patch(page, after, new, pbox)
            methods.append(info.method)
        else:
            methods.append("vector")
        arr = new
    # 写入新文字
    writes = []
    if same_count:
        for ln, t, k in todo:
            if t and t.strip():
                st = styles[k]
                writes.append((t, (st.origin[0] + offset[0], st.origin[1] + offset[1]), st))
    elif new_lines:
        st0 = styles.get(0) or next(iter(styles.values()))
        pitch = _pitch(old, st0.size)
        for k, t in enumerate(new_lines):
            if t.strip():
                writes.append((t, (st0.origin[0] + offset[0], st0.origin[1] + offset[1] + k * pitch),
                               styles.get(k, st0)))
    font_name = ""
    for t, origin, st in writes:
        family = opts.family or st.family
        size = opts.size or st.size
        color = opts.color or st.color
        font, font_name = _font(fm, family)
        if opts.mode == "image":
            w = font.text_length(t, fontsize=size)
            tr = fitz.Rect(origin[0] - 2, origin[1] - size * 1.1, origin[0] + w + 2, origin[1] + size * 0.4)
            reg = render_region(page, tr)
            rgba, (ox, oy) = render_text_rgba(t, font, size, color, reg.dpi / 72.0)
            q = fitz.Point(*origin) * ~reg.matrix
            img = composite_rgba(reg.arr, rgba, int(round(q.x - ox)), int(round(q.y - oy)), opts.texture)
            _insert_patch(page, reg, img, (0, 0, img.shape[1], img.shape[0]))
            write_text_layer(page, [OcrLine(t, [(tr.x0, tr.y0), (tr.x1, tr.y0), (tr.x1, tr.y1), (tr.x0, tr.y1)], 1.0)])
        else:
            style = TextStyle(font=None, size=size, color=color)
            write_overlay(page, [LineOut(t, origin, (1.0, 0.0))], ResolvedFont(font, font_name, False, False), style)
    first = styles.get(0) or next(iter(styles.values()))
    return ScanEditResult(first, methods, font_name)


def _pitch(lines, size: float) -> float:
    if len(lines) > 1:
        gaps = sorted(b.origin[1] - a.origin[1] for a, b in zip(lines, lines[1:]))
        g = gaps[len(gaps) // 2]
        if g > 0:
            return g
    return size * 1.5


def _proof_lines(old, new_lines) -> list[OcrLine]:
    """校对：把新文字放回原行位置（行数变化时多出的行按行距顺延）。"""
    out = []
    pitch = _pitch(old, old[0].size if old else 12)
    for k, t in enumerate(new_lines):
        if not t.strip():
            continue
        ref = old[min(k, len(old) - 1)]
        dy = 0 if k < len(old) else (k - len(old) + 1) * pitch
        r = fitz.Rect(ref.bbox) + (0, dy, 0, dy)
        out.append(OcrLine(t, [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)], 1.0))
    return out


def erase_area(page: fitz.Page, rect: fitz.Rect) -> str:
    """区域擦除工具（F-SCN-22）：擦除框内所有墨迹（手写字、污点等），并删除该区域的 OCR 文字。"""
    rot = _page_prepare(page)
    try:
        pi = load_page_image(page)
        if pi is None:
            # 没有整页底图（图像切块存放等）：渲染该区域擦除后作为补丁贴回
            r = fitz.Rect(rect)
            reg = render_region(page, r + (-r.height, -r.height, r.height, r.height))
            arr, info = erase_text(reg.arr, reg.page_to_px(r), reg.dpi, everything=True)
            _insert_patch(page, reg, arr, (0, 0, arr.shape[1], arr.shape[0]))
            remove_text_layer(page, rect)
            return info.method
        arr, info = erase_text(pi.arr, pi.page_to_px(rect), pi.dpi, everything=True)
        save_page_image(page, pi, arr)
        remove_text_layer(page, rect)
        return info.method
    finally:
        if rot:
            page.set_rotation(rot)
