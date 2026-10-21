"""矢量文字编辑引擎（技术要求第 7 节）。

- 文字块：按 MuPDF 的块（段落）/ 行聚合，坐标为未旋转的页面坐标；
- 删除：用涂除（redaction）把字符从内容流中真正移除，不是白框遮盖；
- 写入：新文字写在同尺寸临时页上，按 7.3 规则选字体并只嵌入字形子集，
  再以 Form XObject 叠加到目标页，文字可搜索、可复制；
- 查找替换：按行匹配，支持区分大小写、全字匹配、正则表达式。

本模块只操作传入的 fitz.Page，版本管理（写时复制、撤销）由应用层负责。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

import pymupdf as fitz

from .engine import ENGINE_LOCK
from .fonts import FontManager, ResolvedFont, norm, resolve_font

FAKE_BOLD = 0.035          # 模拟加粗的偏移（相对字号）
REDACT_SHRINK = 0.25       # 涂除框上下各收缩的比例，避免误删紧邻的行


# ================================================================ 数据结构
@dataclass
class TextStyle:
    font: str | None = None      # None = 保持原字体
    size: float = 12.0
    color: tuple = (0.0, 0.0, 0.0)
    bold: bool = False
    char_spacing: float = 0.0    # 额外字间距（pt）
    line_spacing: float = 1.0    # 行距倍数（相对原行距；新文字框相对 1.2 倍字号）
    reflow: bool = True          # 段落内自动换行，不超出原宽度


@dataclass
class TChar:
    c: str
    bbox: tuple


@dataclass
class TLine:
    bbox: tuple
    origin: tuple            # 第一个字符的基线起点
    dir: tuple               # 书写方向 (cos, sin)
    wmode: int               # 1 = 竖排
    chars: list = field(default_factory=list)
    font: str = ""
    size: float = 12.0
    color: int = 0
    flags: int = 0
    invisible: bool = False  # 扫描页 OCR 透明文字层（字在图片里）；False 为可见文字

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)

    @property
    def vertical(self) -> bool:
        return self.wmode == 1

    def style(self) -> TextStyle:
        return TextStyle(None, round(self.size, 2), int_to_rgb(self.color), bool(self.flags & 16))


@dataclass
class TBlock:
    id: int
    bbox: tuple
    lines: list

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    def style(self) -> TextStyle:
        return self.lines[0].style()

    @property
    def font(self) -> str:
        return self.lines[0].font


def int_to_rgb(c: int) -> tuple:
    return ((c >> 16 & 255) / 255.0, (c >> 8 & 255) / 255.0, (c & 255) / 255.0)


# ================================================================ 提取
_INVISIBLE = {"​", "‌", "‍", "﻿"}


def extract_blocks(page: fitz.Page) -> list[TBlock]:
    """把页面文字聚合为文字块（段落）与行。"""
    with ENGINE_LOCK:
        raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
    blocks = []
    for b in raw.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines = []
        for l in b["lines"]:
            chars, first = [], None
            for s in l["spans"]:
                for c in s["chars"]:
                    if c["c"] in _INVISIBLE:
                        continue
                    chars.append(TChar(c["c"], tuple(c["bbox"])))
                    if first is None and not c["c"].isspace():
                        first = (s, c)
            if first is None:
                continue
            s, c = first
            # 去掉行首行尾空白，但保留行内空格
            while chars and chars[0].c.isspace():
                chars.pop(0)
            while chars and chars[-1].c.isspace():
                chars.pop()
            bb = fitz.Rect(chars[0].bbox)
            for ch in chars[1:]:
                bb |= fitz.Rect(ch.bbox)
            lines.append(TLine(tuple(bb), tuple(c["origin"]), tuple(l["dir"]), l.get("wmode", 0), chars,
                               s["font"], s["size"], s["color"], s["flags"]))
        if lines:
            bb = fitz.Rect(lines[0].bbox)
            for ln in lines[1:]:
                bb |= fitz.Rect(ln.bbox)
            blocks.append(TBlock(len(blocks), tuple(bb), lines))
    return blocks


def find_font_buffer(page: fitz.Page, span_font: str) -> tuple[str, bytes | None]:
    """找到文字所用的嵌入字体：返回 (完整字体名, 字体数据)。未嵌入时数据为 None。"""
    key = norm(span_font)
    with ENGINE_LOCK:
        for xref, ext, _typ, basefont, _name, _enc in page.get_fonts():
            if basefont and norm(basefont).startswith(key) and ext not in ("n/a", ""):
                try:
                    _n, _e, _t, buf = page.parent.extract_font(xref)
                    return basefont, (buf or None)
                except Exception:  # noqa: BLE001
                    return basefont, None
    return span_font, None


# ================================================================ 删除
def _shrunk(r: fitz.Rect, vertical: bool) -> fitz.Rect:
    """涂除框：垂直于书写方向收缩（避免误删紧邻的行），沿书写方向略外扩。

    外扩是必要的：部分子集字体（如黑体）的字形框宽度为 0，只落在字的起点上，
    收缩后的框会漏掉行首的字。
    """
    if vertical:
        d = r.width * REDACT_SHRINK
        return fitz.Rect(r.x0 + d, r.y0 - 0.6, r.x1 - d, r.y1 + 0.6)
    d = r.height * REDACT_SHRINK
    return fitz.Rect(r.x0 - 0.6, r.y0 + d, r.x1 + 0.6, r.y1 - d)


def redact_lines(page: fitz.Page, lines: list[TLine]):
    """从内容流中真正删除这些行的字符（不影响图形、图片与相邻文字）。"""
    with ENGINE_LOCK:
        n = 0
        for ln in lines:
            horizontal = abs(ln.dir[1]) < 1e-3 and ln.dir[0] > 0 and not ln.vertical
            if horizontal:
                page.add_redact_annot(_shrunk(fitz.Rect(ln.bbox), False), fill=False)
                n += 1
            else:
                for ch in ln.chars:
                    if ch.c.isspace():
                        continue
                    r = fitz.Rect(ch.bbox)
                    # 字形框可能是零宽的（只有起点），用不收缩、略外扩的框
                    page.add_redact_annot(r + (-0.4, -0.4, 0.4, 0.4), fill=False)
                    n += 1
        if n:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)


# ================================================================ 排版
@dataclass
class LineOut:
    text: str
    origin: tuple
    dir: tuple
    vertical: bool = False


def text_width(font: fitz.Font, text: str, size: float, spacing: float) -> float:
    if not text:
        return 0.0
    return font.text_length(text, fontsize=size) + spacing * max(0, len(text) - 1)


_LATIN = re.compile(r"[A-Za-z0-9_\-.,;:!?'\"()%/@&]+")


def wrap_text(text: str, font: fitz.Font, size: float, spacing: float, limit: float) -> list[str]:
    """按宽度自动换行：中文逐字断行，英文单词尽量不拆开。"""
    if limit <= 0 or text_width(font, text, size, spacing) <= limit:
        return [text]
    tokens = []
    i = 0
    while i < len(text):
        m = _LATIN.match(text, i)
        if m:
            tokens.append(m.group())
            i = m.end()
        else:
            tokens.append(text[i])
            i += 1
    lines, cur = [], ""
    for tok in tokens:
        cand = cur + tok
        if cur and text_width(font, cand, size, spacing) > limit:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
            # 单个超长单词：逐字拆
            while text_width(font, cur, size, spacing) > limit and len(cur) > 1:
                k = len(cur) - 1
                while k > 1 and text_width(font, cur[:k], size, spacing) > limit:
                    k -= 1
                lines.append(cur[:k])
                cur = cur[k:]
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def layout(text: str, style: TextStyle, font: fitz.Font, block: TBlock | None = None,
           origin: tuple | None = None, offset: tuple = (0.0, 0.0)) -> list[LineOut]:
    """计算每一行的位置。编辑已有块时沿用原位置、方向与行距；新文字框从 origin 开始。"""
    user_lines = text.split("\n")
    size = style.size
    if block is not None:
        first = block.lines[0]
        d = first.dir
        vertical = first.vertical
        o = first.origin
        if vertical:
            # 竖排以首列中心线与顶边定位
            o = ((first.bbox[0] + first.bbox[2]) / 2, first.bbox[1])
        if len(block.lines) > 1:
            gaps = []
            for a, b in zip(block.lines, block.lines[1:]):
                dx, dy = b.origin[0] - a.origin[0], b.origin[1] - a.origin[1]
                if vertical:
                    dx = (b.bbox[0] + b.bbox[2]) / 2 - (a.bbox[0] + a.bbox[2]) / 2
                gaps.append(abs(-d[1] * dx + d[0] * dy) if not vertical else abs(dx))
            gaps.sort()
            pitch = gaps[len(gaps) // 2] or size * 1.2
        else:
            pitch = (first.bbox[3] - first.bbox[1]) * 1.15 if not vertical else size * 1.2
        # 原文字块在书写方向上的宽度（用于自动换行）
        r = fitz.Rect(block.bbox)
        limit = (r.height if vertical else
                 max(abs((r.x1 - r.x0) * d[0]) + abs((r.y1 - r.y0) * d[1]), size))
        multi = len(block.lines) > 1
    else:
        d, vertical, o = (1.0, 0.0), False, origin
        pitch, limit, multi = size * 1.2, 0.0, False
    # 字号改变时行距按比例缩放，避免改大字号后行与行重叠
    if block is not None and block.lines[0].size > 0:
        pitch *= style.size / block.lines[0].size
    pitch *= style.line_spacing
    lines: list[str] = []
    for ul in user_lines:
        if style.reflow and multi and not vertical:
            lines += wrap_text(ul, font, size, style.char_spacing, limit)
        else:
            lines.append(ul)
    out = []
    ox, oy = o[0] + offset[0], o[1] + offset[1]
    for k, t in enumerate(lines):
        if vertical:
            # 竖排：字头朝上逐字向下，列从右向左
            out.append(LineOut(t, (ox - k * pitch, oy), (0.0, 1.0), True))
            continue
        use_orig = (block is not None and style.line_spacing == 1.0 and k < len(block.lines)
                    and abs(style.size - block.lines[0].size) < 0.05
                    and abs(block.lines[k].dir[0] - d[0]) < 1e-3 and abs(block.lines[k].dir[1] - d[1]) < 1e-3)
        if use_orig and k > 0:
            lo = block.lines[k].origin
            # 原各行左端可能不齐（首行缩进），换行后统一与块左侧对齐
            if len(lines) != len(block.lines):
                lo = (ox - offset[0] + (lo[0] - o[0]) * abs(d[1]), lo[1])
            x, y = lo[0] + offset[0], lo[1] + offset[1]
        else:
            # 法线方向（y 轴向下的坐标系中，方向 (cos, sin) 的下一行在 (-sin, cos)）
            x, y = ox - d[1] * pitch * k, oy + d[0] * pitch * k
        out.append(LineOut(t, (x, y), d, False))
    return out


# ================================================================ 写入
def write_overlay(page: fitz.Page, lines: list[LineOut], rf: ResolvedFont, style: TextStyle):
    """把排好的行写入页面：临时页写字 → 字体子集化 → 以 XObject 叠加。"""
    lines = [ln for ln in lines if ln.text.strip()]
    if not lines:
        return
    with ENGINE_LOCK:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        W, H = page.rect.width, page.rect.height
        tmp = fitz.open()
        tp = tmp.new_page(width=W, height=H)
        size, sp = style.size, style.char_spacing
        offsets = [(0.0, 0.0)]
        if rf.fake_bold:
            offsets.append((size * FAKE_BOLD, 0.0))
        for ln in lines:
            for bx, _by in offsets:
                tw = fitz.TextWriter(tp.rect)
                x0, y0 = ln.origin
                if ln.vertical:
                    # origin 为列中心线与顶边；字头朝上，逐字向下
                    y = y0
                    for ch in ln.text:
                        if not ch.isspace():
                            w = rf.font.text_length(ch, fontsize=size)
                            tw.append((x0 + bx - w / 2, y + size * 0.88), ch, font=rf.font, fontsize=size)
                        y += size + sp
                    tw.write_text(tp, color=style.color)
                    continue
                if sp:
                    x = x0
                    for ch in ln.text:
                        tw.append((x + bx, y0), ch, font=rf.font, fontsize=size)
                        x += rf.font.text_length(ch, fontsize=size) + sp
                else:
                    tw.append((x0 + bx, y0), ln.text, font=rf.font, fontsize=size)
                angle = math.degrees(math.atan2(ln.dir[1], ln.dir[0]))
                # morph 的旋转按 PDF 坐标（y 轴向上）计算，与提取方向（y 轴向下）相反
                morph = (fitz.Point(x0, y0), fitz.Matrix(-angle)) if abs(angle) > 0.01 else None
                tw.write_text(tp, color=style.color, morph=morph)
        tmp.subset_fonts()
        tmp = fitz.open("pdf", tmp.tobytes(garbage=3, deflate=True))
        page.show_pdf_page(page.rect, tmp, 0)
        tmp.close()
        if rot:
            page.set_rotation(rot)


def _prepare(page: fitz.Page):
    """临时取消页面旋转，使编辑坐标 == 未旋转页面坐标。返回原旋转角。"""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    return rot


def _restore(page: fitz.Page, rot: int):
    if rot:
        page.set_rotation(rot)


def resolve_for(fm: FontManager, page: fitz.Page, text: str, style: TextStyle,
                orig_font: str = "") -> ResolvedFont:
    buf = None
    name = orig_font
    if style.font is None and orig_font:
        name, buf = find_font_buffer(page, orig_font)
    return resolve_font(fm, text, style.font, name, buf, style.bold)


# ================================================================ 对外操作
@dataclass
class EditResult:
    font_name: str
    substituted: bool


def edit_block(page: fitz.Page, block: TBlock, new_text: str, style: TextStyle, fm: FontManager,
               offset: tuple = (0.0, 0.0)) -> EditResult:
    """修改（或移动）一个文字块：删除原文字后按新内容、样式重写。"""
    rot = _prepare(page)
    try:
        rf = resolve_for(fm, page, new_text, style, block.font)
        redact_lines(page, block.lines)
        write_overlay(page, layout(new_text, style, rf.font, block, offset=offset), rf, style)
    finally:
        _restore(page, rot)
    return EditResult(rf.name, rf.substituted)


def delete_block(page: fitz.Page, block: TBlock):
    rot = _prepare(page)
    try:
        redact_lines(page, block.lines)
    finally:
        _restore(page, rot)


def add_text(page: fitz.Page, origin: tuple, text: str, style: TextStyle, fm: FontManager) -> EditResult:
    """在任意位置新增文字（origin 为第一行基线起点，未旋转坐标）。"""
    rot = _prepare(page)
    try:
        rf = resolve_for(fm, page, text, style)
        write_overlay(page, layout(text, style, rf.font, None, origin=origin), rf, style)
    finally:
        _restore(page, rot)
    return EditResult(rf.name, rf.substituted)


# ================================================================ 查找替换
@dataclass
class Match:
    block: int
    line: int
    start: int
    end: int
    rect: fitz.Rect
    text: str
    context: str


def compile_pattern(needle: str, case: bool = False, whole: bool = False, regex: bool = False):
    pat = needle if regex else re.escape(needle)
    if whole:
        pat = rf"(?<!\w)(?:{pat})(?!\w)"
    return re.compile(pat, 0 if case else re.IGNORECASE)


def find_in_blocks(blocks: list[TBlock], pattern: re.Pattern) -> list[Match]:
    out = []
    for bi, b in enumerate(blocks):
        for li, ln in enumerate(b.lines):
            text = ln.text
            for m in pattern.finditer(text):
                if m.end() == m.start():
                    continue
                r = fitz.Rect(ln.chars[m.start()].bbox)
                for ch in ln.chars[m.start() + 1:m.end()]:
                    r |= fitz.Rect(ch.bbox)
                out.append(Match(bi, li, m.start(), m.end(), r, m.group(), text.strip()[:80]))
    return out


def replace_matches(page: fitz.Page, blocks: list[TBlock], matches: list[Match], pattern: re.Pattern,
                    replacement: str, regex: bool, fm: FontManager) -> tuple[int, list[str]]:
    """替换指定的命中项。每个受影响的行整行重写（沿用该行的字号、颜色、方向）。

    返回 (替换数量, 使用了替代字体的字体名列表)。
    """
    by_line: dict = {}
    for m in matches:
        by_line.setdefault((m.block, m.line), []).append(m)
    subs = []
    count = 0
    rot = _prepare(page)
    try:
        edits = []
        for (bi, li), ms in by_line.items():
            ln = blocks[bi].lines[li]
            text = ln.text
            wanted = {(m.start, m.end) for m in ms}
            pieces, last = [], 0
            for m in pattern.finditer(text):
                if (m.start(), m.end()) not in wanted:
                    continue
                pieces.append(text[last:m.start()])
                pieces.append(m.expand(replacement) if regex else replacement)
                last = m.end()
                count += 1
            pieces.append(text[last:])
            edits.append((ln, "".join(pieces)))
        redact_lines(page, [ln for ln, _ in edits])
        for ln, new in edits:
            style = ln.style()
            rf = resolve_for(fm, page, new, style, ln.font)
            if rf.substituted:
                subs.append(rf.name)
            lo = LineOut(new, ln.origin, ln.dir, ln.vertical)
            write_overlay(page, [lo], rf, replace(style, reflow=False))
    finally:
        _restore(page, rot)
    return count, sorted(set(subs))
