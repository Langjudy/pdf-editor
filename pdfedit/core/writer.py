"""文档写入：合并导出、拆分导出、图片导出、区域页生成。

原则：
- 只生成新文件，从不覆盖导入的原文件；
- 先写临时文件，成功后再重命名为目标文件；
- 普通页面用 insert_pdf 整页复制，保留批注、表单域、超链接；
- 保存时 garbage=3/4 合并重复对象，实现字体、图片等共享资源去重。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pymupdf as fitz

from .engine import ENGINE_LOCK, DocumentStore
from .render import display_size

MM = 72.0 / 25.4   # 1 毫米 = 多少 pt

PAPER_SIZES = {    # 纵向尺寸（毫米）
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420),
    "A4": (210, 297), "A5": (148, 210), "Letter": (215.9, 279.4),
}


class ExportCancelled(Exception):
    pass


@dataclass
class SaveOptions:
    compression: str = "balanced"     # none | balanced | min
    paper: str | None = None          # None=保持原尺寸，否则统一缩放到该纸张
    user_pw: str = ""                 # 打开密码
    owner_pw: str = ""                # 权限密码
    flatten_annots: bool = False      # 批注拼合到页面（F-ANN-08）
    scrub: bool = False               # 清理元数据与隐藏信息（涂黑后建议开启，7.5）


@dataclass
class PageItem:
    """导出用的页面描述（与界面层的 PageRef 解耦）。"""
    src: str
    pno: int
    rot: int = 0


# ---------------------------------------------------------------- 合并

def build_document(store: DocumentStore, items: list[PageItem], opts: SaveOptions | None = None,
                   progress=None) -> fitz.Document:
    opts = opts or SaveOptions()
    out = fitz.open()
    with ENGINE_LOCK:
        total = len(items)
        i = 0
        while i < total:
            it = items[i]
            # 合并连续页为一次 insert_pdf 调用，减少开销
            j = i
            while (j + 1 < total and items[j + 1].src == it.src
                   and items[j + 1].pno == items[j].pno + 1):
                j += 1
            src = store.get(it.src).doc
            start = out.page_count
            out.insert_pdf(src, from_page=it.pno, to_page=items[j].pno, final=0)
            for k in range(i, j + 1):
                rot = items[k].rot % 360
                if rot:
                    pg = out[start + k - i]
                    pg.set_rotation((pg.rotation + rot) % 360)
            i = j + 1
            if progress and not progress(i, total):
                out.close()
                raise ExportCancelled()
        _apply_toc(store, items, out)
        if opts.paper:
            out = _fit_to_paper(out, opts.paper)
        if opts.flatten_annots:
            out.bake(annots=True, widgets=False)
        if opts.scrub:
            from .annots import scrub_private
            scrub_private(out)
    return out


def _apply_toc(store: DocumentStore, items: list[PageItem], out: fitz.Document):
    """书签随页面迁移：目标页仍在输出中的书签保留，其余移除。"""
    first_pos: dict = {}
    order: list[str] = []
    for idx, it in enumerate(items):
        first_pos.setdefault((it.src, it.pno), idx)
        if it.src not in order:
            order.append(it.src)
    per_source = []
    for sid in order:
        src = store.get(sid)
        try:
            toc = src.doc.get_toc(simple=True)
        except Exception:  # noqa: BLE001
            toc = []
        entries = []
        for lvl, title, page1 in toc:
            pos = first_pos.get((sid, page1 - 1))
            if pos is not None:
                entries.append([lvl, title, pos + 1])
        if entries:
            per_source.append((src, entries))
    if not per_source:
        return
    new_toc = []
    wrap = len(per_source) > 1
    for src, entries in per_source:
        base = 1
        if wrap:
            new_toc.append([1, src.name, entries[0][2]])
            base = 2
        prev = base - 1
        for lvl, title, page in entries:
            lvl = lvl + base - 1
            lvl = min(lvl, prev + 1)       # 删除条目后保证层级连续
            new_toc.append([lvl, title, page])
            prev = lvl
    try:
        out.set_toc(new_toc)
    except Exception:  # noqa: BLE001
        pass


def _fit_to_paper(doc: fitz.Document, paper: str) -> fitz.Document:
    """混合尺寸统一缩放到指定纸张（自动选择横向/纵向）。"""
    pw, ph = (v * MM for v in PAPER_SIZES[paper])
    out = fitz.open()
    for page in doc:
        w, h = page.rect.width, page.rect.height
        W, H = (max(pw, ph), min(pw, ph)) if w > h else (pw, ph)
        np_ = out.new_page(width=W, height=H)
        np_.show_pdf_page(np_.rect, doc, page.number)   # 自动保持长宽比并居中
    _copy_toc(doc, out)
    doc.close()
    return out


def _copy_toc(src: fitz.Document, dst: fitz.Document):
    try:
        toc = src.get_toc(simple=True)
        if toc:
            dst.set_toc(toc)
    except Exception:  # noqa: BLE001
        pass


def save_document(doc: fitz.Document, path: str, opts: SaveOptions | None = None):
    """先写临时文件，成功后原子替换为目标文件。"""
    opts = opts or SaveOptions()
    kw = {}
    if opts.compression == "none":
        kw.update(garbage=3, deflate=False)
    elif opts.compression == "min":
        try:
            doc.rewrite_images(dpi_threshold=160, dpi_target=120, quality=65)
        except Exception:  # noqa: BLE001
            pass
        kw.update(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1)
    else:
        kw.update(garbage=4, deflate=True)
    if opts.user_pw or opts.owner_pw:
        kw.update(encryption=fitz.PDF_ENCRYPT_AES_256,
                  user_pw=opts.user_pw or None,
                  owner_pw=opts.owner_pw or opts.user_pw,
                  permissions=(fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY
                               | fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT_HQ))
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".~{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with ENGINE_LOCK:
            doc.save(tmp, **kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def export_pdf(store: DocumentStore, items: list[PageItem], path: str,
               opts: SaveOptions | None = None, progress=None) -> str:
    doc = build_document(store, items, opts, progress)
    try:
        save_document(doc, path, opts)
    finally:
        doc.close()
    return path


def export_image(store: DocumentStore, item: PageItem, path: str, dpi: int = 150, jpg_quality: int = 90):
    with ENGINE_LOCK:
        page = store.page(item.src, item.pno)
        from .render import display_matrix
        pix = page.get_pixmap(matrix=display_matrix(page, item.rot, dpi / 72.0), alpha=False)
        folder = os.path.dirname(os.path.abspath(path))
        tmp = os.path.join(folder, f".~{os.getpid()}.{os.path.basename(path)}")
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            pix.save(tmp, output="jpg", jpg_quality=jpg_quality)
        else:
            pix.save(tmp, output="png")
        os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 区域页

@dataclass
class RegionOptions:
    size_mode: str = "original"   # original（与选区等大）| A4 | A3 | ...
    margin_mm: float = 10.0       # 适配纸张时的边距
    rotate: int = 0               # 新页面额外旋转
    remove_outside: bool = False  # 彻底移除选区外内容
    mark_source: bool = False     # 角落标注来源
    source_label: str = ""        # 来源文字（原文件名、原页码）


@dataclass
class RegionShape:
    points: list = field(default_factory=list)   # 原始坐标下的多边形顶点 [(x, y), ...]
    is_rect: bool = True

    @property
    def bbox(self) -> fitz.Rect:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def build_region_page(store: DocumentStore, sid: str, pno: int, view_rot: int,
                      shape: RegionShape, opts: RegionOptions | None = None) -> fitz.Document:
    """把选区内容生成为一个新页面，返回含该页的临时文档。

    view_rot：工作区额外旋转，新页面方向与用户在界面上看到的一致。
    矢量内容保持矢量；扫描图片按原分辨率保留，不重新压缩。
    """
    opts = opts or RegionOptions()
    with ENGINE_LOCK:
        src_page = store.page(sid, pno)
        total_rot = (src_page.rotation + view_rot) % 360
        # 复制到临时文档并清除 /Rotate，使原始坐标 == 页面坐标，便于裁切和涂除
        tmp = fitz.open()
        tmp.insert_pdf(store.get(sid).doc, from_page=pno, to_page=pno)
        tp = tmp[0]
        tp.set_rotation(0)
        full = tp.rect
        clip = shape.bbox & full
        if clip.is_empty or clip.width < 1 or clip.height < 1:
            raise ValueError("选区为空或超出页面范围")

        if opts.remove_outside:
            for r in (fitz.Rect(full.x0, full.y0, full.x1, clip.y0),
                      fitz.Rect(full.x0, clip.y1, full.x1, full.y1),
                      fitz.Rect(full.x0, clip.y0, clip.x0, clip.y1),
                      fitz.Rect(clip.x1, clip.y0, full.x1, clip.y1)):
                if r.width > 0.01 and r.height > 0.01:
                    tp.add_redact_annot(r, fill=False)
            tp.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                text=fitz.PDF_REDACT_TEXT_REMOVE)

        cw, ch = (clip.height, clip.width) if total_rot % 180 == 90 else (clip.width, clip.height)
        if opts.size_mode in PAPER_SIZES:
            pw, ph = (v * MM for v in PAPER_SIZES[opts.size_mode])
            W, H = (max(pw, ph), min(pw, ph)) if cw > ch else (min(pw, ph), max(pw, ph))
            m = max(0.0, opts.margin_mm) * MM
            avail = fitz.Rect(m, m, W - m, H - m)
            if avail.is_empty:
                avail = fitz.Rect(0, 0, W, H)
            k = min(avail.width / cw, avail.height / ch)
            tw, th = cw * k, ch * k
            target = fitz.Rect(avail.x0 + (avail.width - tw) / 2, avail.y0 + (avail.height - th) / 2,
                               avail.x0 + (avail.width + tw) / 2, avail.y0 + (avail.height + th) / 2)
        else:
            W, H, k = cw, ch, 1.0
            target = fitz.Rect(0, 0, W, H)

        out = fitz.open()
        op = out.new_page(width=W, height=H)
        op.show_pdf_page(target, tmp, 0, clip=clip, rotate=(-total_rot) % 360)

        if not shape.is_rect and len(shape.points) >= 3:
            _clip_to_polygon(out, op, shape.points, clip, total_rot, target, H)

        if opts.mark_source and opts.source_label:
            _stamp_label(op, f"{opts.source_label}  比例 {k:.2f}", W, H)

        if opts.rotate % 360:
            op.set_rotation(opts.rotate % 360)
        tmp.close()
        return out


def _stamp_label(page: fitz.Page, label: str, W: float, H: float):
    """在左下角写入来源标注。

    标注先写在独立的临时页上并做字体子集化（只嵌入用到的字），再叠加到目标页，
    这样文字可搜索复制，且不会影响原页面已嵌入的字体。
    """
    fs = 7 if W > 150 else 5
    tmp = fitz.open()
    tp = tmp.new_page(width=W, height=H)
    tw = fitz.TextWriter(tp.rect, color=(0.4, 0.4, 0.4))
    tw.append((4, H - 4), label, font=fitz.Font("cjk"), fontsize=fs)
    tw.write_text(tp)
    tmp.subset_fonts()
    tmp = fitz.open("pdf", tmp.tobytes(garbage=3, deflate=True))
    page.show_pdf_page(page.rect, tmp, 0)
    tmp.close()


def _clip_to_polygon(doc: fitz.Document, page: fitz.Page, points, clip: fitz.Rect,
                     total_rot: int, target: fitz.Rect, page_h: float):
    """用多边形裁剪路径包裹页面内容，选区外内容不显示。"""
    # 原始坐标 → 以 clip 左上为原点 → 旋转 → 缩放到 target
    m = fitz.Matrix(1, 0, 0, 1, -clip.x0, -clip.y0) * fitz.Matrix(total_rot)
    rr = fitz.Rect(0, 0, clip.width, clip.height) * fitz.Matrix(total_rot)
    m = m * fitz.Matrix(1, 0, 0, 1, -rr.x0, -rr.y0)
    k = target.width / max(rr.width, 1e-6)
    m = m * fitz.Matrix(k, k) * fitz.Matrix(1, 0, 0, 1, target.x0, target.y0)
    pts = [fitz.Point(x, y) * m for x, y in points]
    # 转为 PDF 坐标（y 轴向上）
    ops = []
    for i, p in enumerate(pts):
        ops.append(f"{p.x:.3f} {page_h - p.y:.3f} {'m' if i == 0 else 'l'}")
    path = " ".join(ops) + " h W n\n"
    page.clean_contents()
    xref = page.get_contents()[0]
    data = doc.xref_stream(xref)
    doc.update_stream(xref, b"q " + path.encode() + data + b"\nQ\n")
