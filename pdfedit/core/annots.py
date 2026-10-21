"""批注与图章（技术要求第 9 节）、涂黑（7.5）。

批注一律保存为 PDF 标准注释对象，可在 Acrobat、福昕、WPS 等阅读器中正常显示和编辑。
坐标统一为未旋转的页面坐标（操作时临时把页面旋转设为 0）。
每个批注写入唯一的 NM（名称）作为 ID，页面复制为新版本后仍可按 ID 找到同一个批注。
"""
from __future__ import annotations

import contextlib
import getpass
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pymupdf as fitz

from .engine import ENGINE_LOCK

# 批注类型 → 中文名
LABELS = {
    "Highlight": "高亮", "Underline": "下划线", "StrikeOut": "删除线", "Squiggly": "波浪线",
    "Text": "便签", "FreeText": "文本框", "Square": "矩形", "Circle": "椭圆", "Line": "直线",
    "Polygon": "多边形", "PolyLine": "折线", "Ink": "画笔", "Stamp": "图章", "Redact": "涂黑标记",
    "Link": "链接", "Widget": "表单域", "Popup": "弹出窗口", "FileAttachment": "附件",
}
MARKUP_KINDS = {"hl": "高亮", "ul": "下划线", "so": "删除线", "sq": "波浪线"}
SHAPE_KINDS = {"rect": "矩形", "ellipse": "椭圆", "line": "直线", "arrow": "箭头",
               "polygon": "多边形", "cloud": "云线"}
RECT_TYPES = {"Text", "FreeText", "Square", "Circle", "Stamp"}           # 可整体移动矩形的类型
VERTEX_TYPES = {"Line", "Polygon", "PolyLine", "Ink"}                   # 通过顶点移动的类型
SKIP_TYPES = {"Popup", "Link", "Widget"}                                 # 不在批注列表中显示

PRESET_STAMPS = {      # 名称 → 颜色
    "已审核": (0.85, 0.1, 0.1), "已批准": (0.85, 0.1, 0.1), "作废": (0.85, 0.1, 0.1),
    "草稿": (0.1, 0.3, 0.8), "机密": (0.85, 0.1, 0.1), "退回": (0.85, 0.45, 0.0),
}


def default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def pdf_date() -> str:
    return datetime.now().strftime("D:%Y%m%d%H%M%S")


@contextlib.contextmanager
def unrotated(page: fitz.Page):
    """临时取消页面旋转，使批注坐标 == 未旋转页面坐标。"""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        yield page
    finally:
        if rot:
            page.set_rotation(rot)


@dataclass
class Style:
    color: tuple = (1.0, 0.85, 0.0)       # 线条 / 文字颜色
    fill: tuple | None = None             # 填充色（矩形、椭圆、多边形、文本框背景）
    opacity: float = 1.0
    width: float = 1.5                    # 线宽
    fontsize: float = 12.0                # 文本框字号
    author: str = ""


@dataclass
class AnnotInfo:
    id: str
    type: str
    label: str
    rect: fitz.Rect                      # 未旋转页面坐标
    content: str = ""
    author: str = ""
    color: tuple | None = None
    fill: tuple | None = None
    opacity: float = 1.0
    width: float = 0.0
    modified: str = ""
    vertices: list = field(default_factory=list)


# ================================================================ 基础
def _set_meta(doc: fitz.Document, annot: fitz.Annot, author: str, content: str | None = None) -> str:
    aid = "pe-" + uuid.uuid4().hex[:12]
    doc.xref_set_key(annot.xref, "NM", fitz.get_pdf_str(aid))
    info = {"title": author or "", "creationDate": pdf_date(), "modDate": pdf_date()}
    if content is not None:
        info["content"] = content
    annot.set_info(**info)
    return aid


def find(page: fitz.Page, aid: str) -> fitz.Annot | None:
    for a in page.annots():
        if a.info.get("id") == aid:
            return a
    return None


def list_annots(page: fitz.Page) -> list[AnnotInfo]:
    out = []
    with ENGINE_LOCK, unrotated(page):
        for a in page.annots():
            tname = a.type[1]
            if tname in SKIP_TYPES:
                continue
            info = a.info
            colors = a.colors or {}
            label = LABELS.get(tname, tname)
            if tname == "Line" and a.line_ends and a.line_ends[1] not in (0, None):
                label = "箭头"
            if tname == "Polygon" and _is_cloudy(page.parent, a):
                label = "云线"
            out.append(AnnotInfo(
                id=info.get("id") or f"xref-{a.xref}", type=tname, label=label, rect=fitz.Rect(a.rect),
                content=info.get("content", ""), author=info.get("title", ""),
                color=tuple(colors.get("stroke") or ()) or None, fill=tuple(colors.get("fill") or ()) or None,
                opacity=a.opacity if a.opacity is not None and a.opacity >= 0 else 1.0,
                width=(a.border or {}).get("width", 0) or 0, modified=info.get("modDate", ""),
                vertices=[tuple(v) for v in (a.vertices or [])] if tname in VERTEX_TYPES else []))
    return out


def _is_cloudy(doc, annot) -> bool:
    be = doc.xref_get_key(annot.xref, "BE")
    return be[0] == "dict" and "/C" in be[1]


# ================================================================ 文字选区
def chars_in_order(blocks) -> list[tuple]:
    """把文字块展开成阅读顺序的字符列表：[(字符, 字符框, 行编号)]。"""
    out = []
    line_no = 0
    for b in blocks:
        for ln in b.lines:
            for ch in ln.chars:
                out.append((ch.c, fitz.Rect(ch.bbox), line_no))
            line_no += 1
    return out


def nearest_char(chars, x: float, y: float) -> int:
    """离点最近的字符序号；点在字符右半边时返回下一个序号（便于选到行尾）。"""
    best, bd = -1, 1e18
    for i, (_c, r, _l) in enumerate(chars):
        cx = min(max(x, r.x0), r.x1)
        cy = min(max(y, r.y0), r.y1)
        d = (cx - x) ** 2 + (cy - y) ** 2 * 4
        if d < bd:
            best, bd = i, d
    return best


def selection_quads(chars, i0: int, i1: int) -> list[fitz.Quad]:
    """字符序号区间 → 每行一个四边形（文字标记批注所需）。"""
    if i0 < 0 or i1 < 0:
        return []
    a, b = sorted((i0, i1))
    lines: dict[int, fitz.Rect] = {}
    for c, r, ln in chars[a:b + 1]:
        if c.isspace() and ln not in lines:
            continue
        lines[ln] = r if ln not in lines else lines[ln] | r
    return [fitz.Rect(r).quad for _ln, r in sorted(lines.items())]


def selection_text(chars, i0: int, i1: int) -> str:
    a, b = sorted((i0, i1))
    out, last = [], None
    for c, _r, ln in chars[a:b + 1]:
        if last is not None and ln != last:
            out.append("\n")
        out.append(c)
        last = ln
    return "".join(out)


# ================================================================ 创建
def add_markup(page: fitz.Page, kind: str, quads, style: Style, text: str = "") -> str:
    fn = {"hl": page.add_highlight_annot, "ul": page.add_underline_annot,
          "so": page.add_strikeout_annot, "sq": page.add_squiggly_annot}[kind]
    with ENGINE_LOCK, unrotated(page):
        a = fn(quads=quads)
        a.set_colors(stroke=style.color)
        a.set_opacity(style.opacity)
        aid = _set_meta(page.parent, a, style.author, text)
        a.update()
    return aid


def add_note(page: fitz.Page, point, text: str, style: Style, icon: str = "Note") -> str:
    with ENGINE_LOCK, unrotated(page):
        a = page.add_text_annot(point, text, icon=icon)
        a.set_colors(stroke=style.color)
        a.set_opacity(style.opacity)
        aid = _set_meta(page.parent, a, style.author, text)
        a.update()
    return aid


def add_freetext(page: fitz.Page, rect, text: str, style: Style) -> str:
    with ENGINE_LOCK, unrotated(page):
        a = page.add_freetext_annot(
            fitz.Rect(rect), text, fontsize=style.fontsize, text_color=style.color,
            fill_color=style.fill, opacity=style.opacity)
        aid = _set_meta(page.parent, a, style.author, text)
        a.update()
    return aid


def add_shape(page: fitz.Page, kind: str, points, style: Style, text: str = "") -> str:
    """kind: rect / ellipse / line / arrow / polygon / cloud / polyline。points 为原始坐标点列表。"""
    pts = [fitz.Point(p) for p in points]
    with ENGINE_LOCK, unrotated(page):
        if kind in ("rect", "ellipse"):
            r = fitz.Rect(pts[0], pts[1]).normalize()
            a = page.add_rect_annot(r) if kind == "rect" else page.add_circle_annot(r)
        elif kind in ("line", "arrow"):
            a = page.add_line_annot(pts[0], pts[1])
            if kind == "arrow":
                a.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
        elif kind in ("polygon", "cloud"):
            a = page.add_polygon_annot(pts)
        elif kind == "polyline":
            a = page.add_polyline_annot(pts)
        else:
            raise ValueError(f"未知图形：{kind}")
        if kind == "cloud":
            a.set_border(width=style.width, clouds=2)
        else:
            a.set_border(width=style.width)
        a.set_colors(stroke=style.color, fill=style.fill if kind not in ("line", "arrow") else None)
        a.set_opacity(style.opacity)
        aid = _set_meta(page.parent, a, style.author, text)
        a.update()
    return aid


def add_ink(page: fitz.Page, strokes, style: Style) -> str:
    """strokes：[[(x, y), ...], ...]。"""
    with ENGINE_LOCK, unrotated(page):
        a = page.add_ink_annot([[tuple(p) for p in s] for s in strokes if len(s) >= 2])
        a.set_border(width=style.width)
        a.set_colors(stroke=style.color)
        a.set_opacity(style.opacity)
        aid = _set_meta(page.parent, a, style.author)
        a.update()
    return aid


def make_stamp_png(text: str, color=(0.85, 0.1, 0.1), font_file: str | None = None) -> bytes:
    """生成透明底的中文印章图片（圆角边框 + 文字）。"""
    with ENGINE_LOCK:
        font = None
        if font_file:
            try:
                font = fitz.Font(fontfile=font_file)
            except Exception:  # noqa: BLE001
                font = None
        if font is None:
            font = fitz.Font("cjk")
        size = 40
        w = font.text_length(text, fontsize=size) + 44
        h = 76
        d = fitz.open()
        p = d.new_page(width=w, height=h)
        p.draw_rect(fitz.Rect(4, 4, w - 4, h - 4), color=color, width=4, radius=0.15)
        tw = fitz.TextWriter(p.rect, color=color)
        tl = font.text_length(text, fontsize=size)
        tw.append(((w - tl) / 2, 53), text, font=font, fontsize=size)
        tw.write_text(p)
        pix = p.get_pixmap(dpi=300, alpha=True)
        return pix.tobytes("png")


def image_size(png: bytes) -> tuple[int, int]:
    with ENGINE_LOCK:
        pix = fitz.Pixmap(png)
        return pix.width, pix.height


def add_stamp(page: fitz.Page, rect, image: bytes, style: Style, text: str = "") -> str:
    """图章批注：标准 Stamp 注释，外观替换为指定图片（支持透明）。"""
    r = fitz.Rect(rect)
    doc = page.parent
    with ENGINE_LOCK, unrotated(page):
        a = page.add_stamp_annot(r, stamp=0)
        aid = _set_meta(doc, a, style.author, text)
        a.set_opacity(style.opacity)
        a.update()
        a.set_rect(r)                     # update 会按英文标准图章比例调整矩形，改回来
        _set_image_ap(doc, page, a, image)
    return aid


def _image_xobject(doc: fitz.Document, image: bytes) -> int:
    """直接在文档中创建图片对象（RGB + 透明度遮罩 SMask），返回 xref。

    不借助临时页面：增删页面会使已持有的 Page / Annot 对象失效。
    """
    pix = fitz.Pixmap(image)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    w, h = pix.width, pix.height
    samples = pix.samples
    smask = 0
    if pix.alpha:
        arr = np.frombuffer(samples, np.uint8).reshape(-1, pix.n)
        rgb = arr[:, :3].tobytes()
        alpha = arr[:, pix.n - 1].tobytes()
        smask = doc.get_new_xref()
        doc.update_object(smask, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}"
                                 f"/ColorSpace/DeviceGray/BitsPerComponent 8>>")
        doc.update_stream(smask, alpha)
    else:
        rgb = samples
    xref = doc.get_new_xref()
    sm = f"/SMask {smask} 0 R" if smask else ""
    doc.update_object(xref, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}"
                            f"/ColorSpace/DeviceRGB/BitsPerComponent 8{sm}>>")
    doc.update_stream(xref, rgb)
    return xref


def _set_image_ap(doc: fitz.Document, page: fitz.Page, a: fitz.Annot, image: bytes):
    img_xref = _image_xobject(doc, image)
    ap = doc.xref_get_key(a.xref, "AP/N")
    if ap[0] != "xref":
        a.update()
        ap = doc.xref_get_key(a.xref, "AP/N")
    ap_xref = int(ap[1].split()[0])
    W, H = a.rect.width, a.rect.height
    doc.xref_set_key(ap_xref, "BBox", f"[0 0 {W:.3f} {H:.3f}]")
    doc.xref_set_key(ap_xref, "Matrix", "[1 0 0 1 0 0]")
    doc.xref_set_key(ap_xref, "Resources", f"<</XObject<</Img {img_xref} 0 R>>>>")
    doc.update_stream(ap_xref, f"q {W:.3f} 0 0 {H:.3f} 0 0 cm /Img Do Q".encode())


# ================================================================ 修改
def update_annot(page: fitz.Page, aid: str, color=None, fill=..., opacity=None, width=None,
                 content=None, fontsize=None) -> bool:
    doc = page.parent
    with ENGINE_LOCK, unrotated(page):
        a = find(page, aid)
        if a is None:
            return False
        tname = a.type[1]
        if tname == "Stamp":
            # 图章外观是图片，只改透明度与说明，不触发重新生成外观
            if opacity is not None:
                doc.xref_set_key(a.xref, "CA", f"{opacity:g}")
            if content is not None:
                a.set_info(content=content, modDate=pdf_date())
            return True
        if content is not None:
            a.set_info(content=content, modDate=pdf_date())
        else:
            a.set_info(modDate=pdf_date())
        if width is not None and tname not in ("Highlight", "Underline", "StrikeOut", "Squiggly", "Text"):
            clouds = 2 if _is_cloudy(doc, a) else -1
            a.set_border(width=width, clouds=clouds)
        if opacity is not None:
            a.set_opacity(opacity)
        if tname == "FreeText":
            kw = {}
            if color is not None:
                kw["text_color"] = color
            if fill is not ...:
                kw["fill_color"] = fill
            if fontsize:
                kw["fontsize"] = fontsize
            a.update(**kw)
            return True
        if color is not None or fill is not ...:
            cur = a.colors or {}
            stroke = color if color is not None else cur.get("stroke")
            f = cur.get("fill") if fill is ... else fill
            if tname in ("Line", "Ink") or tname in ("Highlight", "Underline", "StrikeOut", "Squiggly", "Text"):
                f = None
            a.set_colors(stroke=stroke, fill=f)
        a.update()
    return True


def move_annot(page: fitz.Page, aid: str, dx: float, dy: float) -> bool:
    doc = page.parent
    with ENGINE_LOCK, unrotated(page):
        a = find(page, aid)
        if a is None:
            return False
        tname = a.type[1]
        if tname in RECT_TYPES:
            r = fitz.Rect(a.rect) + (dx, dy, dx, dy)
            if tname == "Stamp":
                # 平移图章：直接改 Rect，外观（图片）保持不变
                mat = page.transformation_matrix
                pr = r * mat
                pr.normalize()
                doc.xref_set_key(a.xref, "Rect", f"[{pr.x0:.3f} {pr.y0:.3f} {pr.x1:.3f} {pr.y1:.3f}]")
            else:
                a.set_rect(r)
                a.update()
            return True
        if tname in VERTEX_TYPES:
            return _recreate_shifted(page, a, dx, dy)
    return False


def _recreate_shifted(page: fitz.Page, a: fitz.Annot, dx: float, dy: float) -> bool:
    """顶点型批注（直线、多边形、画笔）：按位移重建，保留 ID、颜色、线宽、作者等属性。"""
    doc = page.parent
    tname = a.type[1]
    info = dict(a.info)
    colors = a.colors or {}
    width = (a.border or {}).get("width", 1) or 1
    opacity = a.opacity if a.opacity is not None and a.opacity >= 0 else 1.0
    cloudy = _is_cloudy(doc, a)
    ends = a.line_ends
    verts = a.vertices or []
    page.delete_annot(a)
    if tname == "Ink":
        new = page.add_ink_annot([[(x + dx, y + dy) for x, y in s] for s in verts])
    elif tname == "Line":
        (x0, y0), (x1, y1) = verts[0], verts[1]
        new = page.add_line_annot((x0 + dx, y0 + dy), (x1 + dx, y1 + dy))
        if ends:
            new.set_line_ends(*ends)
    else:
        pts = [(x + dx, y + dy) for x, y in verts]
        new = page.add_polygon_annot(pts) if tname == "Polygon" else page.add_polyline_annot(pts)
    new.set_border(width=width, clouds=2 if cloudy else -1)
    new.set_colors(stroke=colors.get("stroke"), fill=colors.get("fill") if tname != "Ink" else None)
    new.set_opacity(opacity)
    doc.xref_set_key(new.xref, "NM", fitz.get_pdf_str(info.get("id", "")))
    new.set_info(content=info.get("content", ""), title=info.get("title", ""),
                 creationDate=info.get("creationDate", ""), modDate=pdf_date())
    new.update()
    return True


def delete_annot(page: fitz.Page, aid: str) -> bool:
    with ENGINE_LOCK, unrotated(page):
        a = find(page, aid)
        if a is None:
            return False
        page.delete_annot(a)
    return True


# ================================================================ 涂黑
def redact_area(page: fitz.Page, rect, fill=(0, 0, 0)) -> None:
    """涂黑（7.5）：从内容流中真正删除区域内的文字、图片像素和被覆盖的矢量图形，并以黑块标示。"""
    with ENGINE_LOCK, unrotated(page):
        page.add_redact_annot(fitz.Rect(rect), fill=fill)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        # 删除与该区域重叠的批注（批注内容同样可能泄露信息）
        r = fitz.Rect(rect)
        for a in list(page.annots()):
            if a.type[1] not in SKIP_TYPES and fitz.Rect(a.rect).intersects(r):
                page.delete_annot(a)


# ================================================================ 导出
def flatten(doc: fitz.Document):
    """把批注拼合到页面内容（不再是可编辑的批注）。"""
    with ENGINE_LOCK:
        doc.bake(annots=True, widgets=False)


def scrub_private(doc: fitz.Document):
    """清除元数据与隐藏信息（保留 OCR 透明文字层、链接与表单）。"""
    with ENGINE_LOCK:
        doc.scrub(attached_files=True, clean_pages=True, embedded_files=True, hidden_text=False,
                  javascript=True, metadata=True, redactions=True, remove_links=False,
                  reset_fields=False, reset_responses=False, thumbnails=True, xml_metadata=True)
        doc.set_metadata({})


def summary_text(entries) -> str:
    """批注摘要：entries 为 [(工作区页码, AnnotInfo)]。"""
    lines = ["批注摘要", f"生成时间：{datetime.now():%Y-%m-%d %H:%M}", f"共 {len(entries)} 条", ""]
    for page_no, a in entries:
        when = a.modified[2:14] if a.modified.startswith("D:") else a.modified
        if len(when) >= 12:
            when = f"{when[0:4]}-{when[4:6]}-{when[6:8]} {when[8:10]}:{when[10:12]}"
        lines.append(f"第 {page_no} 页 | {a.label} | 作者：{a.author or '—'} | {when}")
        if a.content.strip():
            for t in a.content.strip().splitlines():
                lines.append(f"    {t}")
    return "\n".join(lines) + "\n"


def stamp_rect_at(point, img_w: int, img_h: int, width_pt: float = 140) -> fitz.Rect:
    """以单击点为中心，按图片长宽比生成图章矩形。"""
    h = width_pt * img_h / max(img_w, 1)
    x, y = point
    return fitz.Rect(x - width_pt / 2, y - h / 2, x + width_pt / 2, y + h / 2)
