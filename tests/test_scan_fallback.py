"""没有整页底图时的覆盖式编辑：扫描图像切块存放、CAD 曲线文字（用户反馈：别的电脑上报“没有可编辑的扫描底图”）。"""
import cv2
import numpy as np
import pymupdf as fitz
import pytest

from scanutil import fake_scan
from pdfedit.app.textservice import TextService
from pdfedit.core import ocr as ocrmod
from pdfedit.core.fonts import FontManager
from pdfedit.core.scan import load_page_image, render_region
from pdfedit.core.scanedit import ScanEditOptions, erase_area, scan_edit_block
from test_scan import form_page


@pytest.fixture(scope="module")
def engine():
    return ocrmod.OcrEngine()


@pytest.fixture(scope="module")
def fm():
    m = FontManager()
    m.ensure_loaded()
    return m


def tiled_scan_doc():
    """扫描图像被切成 6 条横条分别存放（常见于扫描仪、CAD 打印成 PDF）。"""
    jpg, rect = fake_scan(form_page())
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    d = fitz.open()
    p = d.new_page(width=rect.width, height=rect.height)
    n = 6
    H = img.shape[0]
    for k in range(n):
        y0, y1 = H * k // n, H * (k + 1) // n
        ok, part = cv2.imencode(".jpg", img[y0:y1], [cv2.IMWRITE_JPEG_QUALITY, 90])
        p.insert_image(fitz.Rect(0, rect.height * y0 / H, rect.width, rect.height * y1 / H), stream=part.tobytes())
    return d


def outline_text(page, text, x, y, size):
    """把文字转成填充的矢量轮廓画到页面上（模拟 CAD SHX 字体 / 文字转曲线）。"""
    scale = 8
    w = int(size * len(text) * 1.1 * scale) + 40
    h = int(size * 1.6 * scale)
    tmp = fitz.open()
    tp = tmp.new_page(width=w / scale, height=h / scale)
    tw = fitz.TextWriter(tp.rect)
    tw.append((2, size * 1.1), text, font=fitz.Font(fontfile=r"C:\Windows\Fonts\simhei.ttf"), fontsize=size)
    tw.write_text(tp)
    pix = tp.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    contours, hier = cv2.findContours((g < 128).astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    shape = page.new_shape()
    for c in contours:
        if len(c) >= 3:
            shape.draw_polyline([(x + pt[0][0] / scale, y + pt[0][1] / scale - size * 1.1) for pt in c] + [
                (x + c[0][0][0] / scale, y + c[0][0][1] / scale - size * 1.1)])
    shape.finish(fill=(0, 0, 0), color=None, even_odd=True)
    shape.commit()


def cad_doc():
    d = fitz.open()
    p = d.new_page(width=842, height=595)
    p.draw_rect(fitz.Rect(20, 20, 822, 575), color=(0, 0, 0), width=1.5)
    p.draw_line((100, 300), (600, 300), color=(0, 0, 0), width=0.8)          # 尺寸线（穿过文字附近）
    p.draw_line((100, 290), (100, 310))
    p.draw_line((600, 290), (600, 310))
    outline_text(p, "450", 330, 290, 24)
    outline_text(p, "印度铁路配件包装方案", 120, 120, 28)
    return d


def ocr_layer(d, engine):
    ocrmod.write_text_layer(d[0], ocrmod.ocr_page(d[0], engine))
    return fitz.open("pdf", d.tobytes())


def block_with(page, s):
    return next(b for b in TextService.page_blocks(page, True) if s in b.text)


def ink_ratio(page, rect):
    reg = render_region(page, rect)
    g = reg.arr if reg.arr.ndim == 2 else cv2.cvtColor(reg.arr, cv2.COLOR_RGB2GRAY)
    return float((g < 100).mean())


# ---------------------------------------------------------------- 切块扫描件
def test_tiled_scan_has_no_single_page_image():
    assert load_page_image(tiled_scan_doc()[0]) is None


def test_tiled_scan_edit_and_delete(engine, fm):
    d = ocr_layer(tiled_scan_doc(), engine)
    p = d[0]
    b = block_with(p, "北京")
    old = fitz.Rect(b.bbox)
    res = scan_edit_block(p, b, "上海示例科技公司", fm, ScanEditOptions(mode="vector"))
    assert "上海示例科技公司" in p.get_text() and "北京" not in p.get_text()
    # 二次 JPEG 压缩后宋体与仿宋笔画接近，风格估算只做尽力判断（面板可手动改），这里只检查字号
    assert res.style is not None and res.style.label in ("宋体", "仿宋") and abs(res.style.size - 14) < 2.5
    # 原字已被补丁盖住：重新 OCR 该区域只剩新文字
    reg = render_region(p, old + (-5, -5, 60, 5))
    text = "".join(x.text for x in ocrmod.ocr_image(reg.arr, reg.matrix, engine))
    assert "北京" not in text and "上海" in text
    b = block_with(p, "第一章")
    r = fitz.Rect(b.bbox)
    scan_edit_block(p, b, "", fm)
    assert ink_ratio(p, r) < 0.01 and "第一章" not in p.get_text()


def test_tiled_scan_image_mode(engine, fm):
    d = ocr_layer(tiled_scan_doc(), engine)
    p = d[0]
    b = block_with(p, "伍万")
    scan_edit_block(p, b, "捌万元整", fm, ScanEditOptions(mode="image"))
    reg = render_region(p, fitz.Rect(b.bbox) + (-5, -5, 40, 5))
    text = "".join(x.text for x in ocrmod.ocr_image(reg.arr, reg.matrix, engine))
    assert "捌万元整" in text and "伍万" not in text
    assert "捌万元整" in p.get_text()


def test_tiled_scan_region_erase(engine):
    d = tiled_scan_doc()
    p = d[0]
    r = fitz.Rect(60, 280, 400, 320)
    assert ink_ratio(p, r) > 0.01
    erase_area(p, r)
    assert ink_ratio(p, r) < 0.005


# ---------------------------------------------------------------- CAD 曲线文字
def test_cad_outline_text_edit_keeps_dimension_line(engine, fm):
    d = ocr_layer(cad_doc(), engine)
    p = d[0]
    n_before = len(p.get_drawings())
    b = block_with(p, "450")
    res = scan_edit_block(p, b, "480", fm, ScanEditOptions(mode="vector"))
    assert "vector" in res.erase_methods                # 曲线文字直接从内容中删除，不需要补丁
    assert len(p.get_images()) == 0
    assert "480" in p.get_text() and "450" not in p.get_text()
    lines = [dr for dr in p.get_drawings() if dr["rect"].width > 400 and dr["rect"].height < 2]
    assert lines, "尺寸线被误删"
    assert len(p.get_drawings()) < n_before
    b = block_with(p, "印度铁路")
    title = fitz.Rect(b.bbox)
    scan_edit_block(p, b, "", fm)
    assert ink_ratio(p, title) < 0.005
    assert any(dr["rect"].width > 700 for dr in p.get_drawings())     # 图框保留


# ---------------------------------------------------------------- 端到端：导入 → 判定 → OCR → 修改
@pytest.mark.parametrize("make, keyword, new_text", [
    (tiled_scan_doc, "北京", "上海示例科技公司"),
    (cad_doc, "450", "480"),
])
def test_end_to_end_through_services(tmp_path, engine, fm, make, keyword, new_text):
    from pdfedit.app.scanservice import ScanService
    from pdfedit.app.workspace import Workspace
    from pdfedit.core import pagetype
    from pdfedit.core.engine import DocumentStore
    path = str(tmp_path / "doc.pdf")
    make().save(path)
    store, ws = DocumentStore(), Workspace()
    s = store.open_file(path)
    s.page_types = [pagetype.classify(s.doc[0])]
    assert s.page_types[0] in (pagetype.SCAN, pagetype.OUTLINE)
    ws.append_pages(s.id, [0])
    texts = TextService(store, ws, fm)
    scans = ScanService(store, ws, texts)
    scans.engine = engine
    assert texts.is_scan(ws.refs[0])
    scans.commit_ocr(s.id, 0, scans.ocr_key(s.id, 0))
    b = next(b for b in texts.blocks(ws.refs[0]) if keyword in b.text)
    texts.edit(ws.refs[0].uid, b.id, b.text.replace(keyword, new_text), None,
               scan_opts=ScanEditOptions(mode="vector"))
    t = store.page(ws.refs[0].src, ws.refs[0].pno).get_text()
    assert new_text in t and keyword not in t
    ws.undo()
    assert keyword in store.page(ws.refs[0].src, ws.refs[0].pno).get_text()


def test_ocr_layer_keeps_latin_words_together(engine):
    """OCR 文字层中英文单词、编号不能被拆成单个字母（否则搜不到 DWG-001 这类图号）。"""
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 100), "Drawing No. DWG-001  Scale 1:50", fontsize=16)
    pix = p.get_pixmap(dpi=200)
    sd = fitz.open()
    sp = sd.new_page()
    sp.insert_image(sp.rect, pixmap=pix)
    ocrmod.write_text_layer(sp, ocrmod.ocr_page(sp, engine))
    blocks = [b.text for b in TextService.page_blocks(sp, True)]
    assert any("DWG-001" in t and "Drawing" in t for t in blocks), blocks
    hit = sp.search_for("DWG-001")
    assert hit and abs(hit[0].x0 - p.search_for("DWG-001")[0].x0) < 6
