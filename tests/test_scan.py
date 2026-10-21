"""扫描件处理（技术要求第 8 节）：预处理、OCR 文字层、覆盖式编辑、区域擦除。"""
import os

import cv2
import numpy as np
import pymupdf as fitz
import pytest

from scanutil import fake_scan
from pdfedit.core import ocr as ocrmod
from pdfedit.core.fonts import FontManager
from pdfedit.core.scan import PreOptions, estimate_skew, load_page_image, preprocess, save_page_image
from pdfedit.core.scanedit import ScanEditOptions, erase_area, estimate_style, scan_edit_block
from pdfedit.core.textedit import extract_blocks

SPEC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "PDF编辑软件技术要求说明书.pdf")
SIMSUN = r"C:\Windows\Fonts\simsun.ttc"


def scan_doc(angle=0.0, stamp=False, page_fn=None):
    if page_fn is None:
        if not os.path.exists(SPEC):
            pytest.skip("缺少样例文件 PDF编辑软件技术要求说明书.pdf（未随仓库提供）")
        src = fitz.open(SPEC)
        page = src[0]
    else:
        page = page_fn()
    jpg, rect = fake_scan(page, angle=angle, stamp=stamp)
    d = fitz.open()
    p = d.new_page(width=rect.width, height=rect.height)
    p.insert_image(p.rect, stream=jpg)
    return d


def form_page():
    """带表格线的单据：用于验证擦除时保留表格线。"""
    d = fitz.open()
    p = d.new_page()
    for y in (100, 140, 180):
        p.draw_line((60, y), (540, y), width=1.2)
    for x in (60, 200, 540):
        p.draw_line((x, 100), (x, 180), width=1.2)
    p.insert_text((70, 128), "客户名称", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((210, 128), "北京某某贸易公司", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((70, 168), "合同金额", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((210, 168), "伍万元整", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((70, 300), "第一章 总则", fontsize=22, fontname="F2", fontfile=r"C:\Windows\Fonts\simhei.ttf")
    return p


@pytest.fixture(scope="module")
def engine():
    return ocrmod.OcrEngine()


@pytest.fixture(scope="module")
def fm():
    m = FontManager()
    m.ensure_loaded()
    return m


@pytest.fixture(scope="module")
def ocr_form(engine):
    """OCR 过的表格扫描页（模块内共享，避免重复识别）。"""
    d = scan_doc(page_fn=form_page)
    lines = ocrmod.ocr_page(d[0], engine)
    ocrmod.write_text_layer(d[0], lines)
    return fitz.open("pdf", d.tobytes())


def page_gray(page, dpi=100):
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).astype(np.int16)


# ---------------------------------------------------------------- 预处理
def test_deskew_estimate_and_preprocess():
    d = scan_doc(angle=4.0)
    pi = load_page_image(d[0])
    assert pi is not None and pi.arr.shape[1] > 2000
    a = estimate_skew(pi.gray)
    assert abs(a - (-4.0)) < 0.3                  # 扫描时逆时针转了 4°，需顺时针转回
    out, info = preprocess(pi.arr, PreOptions(deskew=True, border=True, despeckle=True))
    assert abs(estimate_skew(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY))) < 0.3
    corners = [out[5, 5], out[5, -5], out[-5, 5], out[-5, -5]]
    assert all(np.mean(c) > 180 for c in corners)  # 黑边已去除
    save_page_image(d[0], pi, out)
    assert load_page_image(d[0]).arr.shape == out.shape


def test_binarize_option():
    d = scan_doc()
    pi = load_page_image(d[0])
    out, _ = preprocess(pi.arr, PreOptions(deskew=False, border=False, despeckle=False, binarize=True))
    assert out.ndim == 2 and set(np.unique(out)) <= {0, 255}


# ---------------------------------------------------------------- OCR
def test_ocr_layer_searchable_and_invisible(engine):
    d = scan_doc(stamp=True)
    before = page_gray(d[0])
    lines = ocrmod.ocr_page(d[0], engine)
    text = "".join(l.text for l in lines)
    for kw in ("PDF编辑软件技术要求说明书", "项目概述", "建设目标", "适用范围"):
        assert kw in text
    ocrmod.write_text_layer(d[0], lines)
    after = page_gray(d[0])
    assert np.abs(after - before).max() <= 2         # 外观不变
    assert ocrmod.has_text_layer(d[0])
    hits = d[0].search_for("技术要求说明书")
    assert hits and hits[0].y0 < 150                 # 能搜到，位置在页面顶部标题处
    n = ocrmod.remove_text_layer(d[0])
    assert n > 100 and not d[0].get_text().strip()


def test_remove_layer_keeps_visible_text(engine):
    d = scan_doc()
    p = d[0]
    p.insert_text((50, 820), "可见批注", fontname="china-s", fontsize=10)
    lines = ocrmod.ocr_page(p, engine)
    ocrmod.write_text_layer(p, lines[:3])
    ocrmod.remove_text_layer(p)
    assert p.get_text().strip() == "可见批注"


# ---------------------------------------------------------------- 覆盖式编辑
def _block(page, s):
    return next(b for b in extract_blocks(page) if s in b.text)


def test_estimate_style(ocr_form, fm):
    p = ocr_form[0]
    pi = load_page_image(p)
    b = _block(p, "第一章")
    st = estimate_style(pi, fitz.Rect(b.lines[0].bbox), b.lines[0].text, fm)
    assert st.label == "黑体", st.scores
    assert abs(st.size - 22) < 2.5
    assert max(st.color) < 0.35                      # 黑色墨迹
    b = _block(p, "北京")
    st = estimate_style(pi, fitz.Rect(b.lines[0].bbox), b.lines[0].text, fm)
    assert st.label == "宋体", st.scores
    assert abs(st.size - 14) < 2


def test_scan_edit_vector_keeps_table_lines(ocr_form, fm, engine):
    d = fitz.open("pdf", ocr_form.tobytes())
    p = d[0]
    b = _block(p, "北京")
    lines_before = _table_line_pixels(p)
    res = scan_edit_block(p, b, b.text.replace("北京某某贸易公司", "上海示例科技公司"), fm,
                          ScanEditOptions(mode="vector"))
    t = p.get_text()
    assert "上海示例科技公司" in t and "北京某某" not in t
    # 原图像中的旧字已擦除：对页面图像再次 OCR，看不到旧文字
    pi = load_page_image(p)
    img_text = "".join(l.text for l in ocrmod.ocr_image(
        cv2.cvtColor(pi.gray, cv2.COLOR_GRAY2RGB) if pi.arr.ndim == 2 else pi.arr,
        pi.matrix, engine))
    assert "北京" not in img_text
    assert "客户名称" in img_text and "伍万元整" in img_text     # 其他文字不受影响
    assert _table_line_pixels(p) > lines_before * 0.97            # 表格线保留
    assert res.style.label == "宋体"


def test_scan_edit_image_mode(ocr_form, fm, engine):
    d = fitz.open("pdf", ocr_form.tobytes())
    p = d[0]
    b = _block(p, "伍万")
    scan_edit_block(p, b, b.text.replace("伍万元整", "捌万元整"), fm,
                    ScanEditOptions(mode="image", texture=True))
    pi = load_page_image(p)
    img_text = "".join(l.text for l in ocrmod.ocr_image(pi.arr, pi.matrix, engine))
    assert "捌万元整" in img_text and "伍万" not in img_text   # 新文字已画进图片
    assert "捌万元整" in p.get_text()                           # 透明文字层同步更新
    assert not any(s["type"] == 0 for s in p.get_texttrace())   # 没有新增可见矢量文字


def test_proofread_only_changes_layer_not_image(ocr_form, fm):
    d = fitz.open("pdf", ocr_form.tobytes())
    p = d[0]
    before = page_gray(p)
    b = _block(p, "客户名称")
    scan_edit_block(p, b, b.text.replace("客户名称", "客戶名稱"), fm, ScanEditOptions(proofread_only=True))
    assert "客戶名稱" in p.get_text()
    assert np.abs(page_gray(p) - before).max() <= 2


def test_delete_line(ocr_form, fm):
    d = fitz.open("pdf", ocr_form.tobytes())
    p = d[0]
    b = _block(p, "第一章")
    scan_edit_block(p, b, "", fm)
    assert "第一章" not in p.get_text()
    pi = load_page_image(p)
    r = fitz.Rect(b.bbox)
    x0, y0, x1, y1 = pi.page_to_px(r)
    crop = pi.gray[y0:y1, x0:x1]
    assert (crop < 100).mean() < 0.01                # 原字迹基本擦净


def test_erase_area(ocr_form):
    d = fitz.open("pdf", ocr_form.tobytes())
    p = d[0]
    r = fitz.Rect(60, 280, 400, 320)
    erase_area(p, r)
    pi = load_page_image(p)
    x0, y0, x1, y1 = pi.page_to_px(r)
    assert (pi.gray[y0:y1, x0:x1] < 100).mean() < 0.005
    assert "第一章" not in p.get_text()


def _table_line_pixels(page) -> int:
    pi = load_page_image(page)
    x0, y0, x1, y1 = pi.page_to_px(fitz.Rect(55, 95, 545, 185))
    g = pi.gray[y0:y1, x0:x1]
    ink = (g < 128).astype(np.uint8)
    h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (80, 1)))
    v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 80)))
    return int(h.sum() + v.sum())


def four_fonts_page():
    d = fitz.open()
    p = d.new_page()
    fonts = [("宋", "simsun.ttc"), ("黑", "simhei.ttf"), ("仿", "simfang.ttf"), ("楷", "simkai.ttf")]
    for i, (lab, ff) in enumerate(fonts):
        p.insert_text((60, 100 + i * 60), f"{lab}体样例：北京某某贸易有限公司合同", fontsize=14,
                      fontname=f"F{i}", fontfile="C:/Windows/Fonts/" + ff)
        p.insert_text((60, 130 + i * 60), f"{lab}体大字标题", fontsize=24,
                      fontname=f"F{i}", fontfile="C:/Windows/Fonts/" + ff)
    return p


def test_font_style_classification(engine, fm):
    d = scan_doc(page_fn=four_fonts_page)
    ocrmod.write_text_layer(d[0], ocrmod.ocr_page(d[0], engine))
    pi = load_page_image(d[0])
    expect = {"宋": "宋体", "黑": "黑体", "仿": "仿宋", "楷": "楷体"}
    checked = 0
    for b in extract_blocks(d[0]):
        for ln in b.lines:
            key = ln.text[:1]
            if key not in expect:
                continue
            st = estimate_style(pi, fitz.Rect(ln.bbox), ln.text, fm)
            assert st.label == expect[key], (ln.text, st.scores)
            big = "大字" in ln.text
            assert abs(st.size - (24 if big else 14)) < (3 if big else 2), (ln.text, st.size)
            checked += 1
    assert checked >= 7


# ---------------------------------------------------------------- 彩色海报（用户反馈的场景）
from scanutil import poster_doc  # noqa: E402


@pytest.fixture(scope="module")
def poster_pdf(engine):
    d = poster_doc()
    ocrmod.write_text_layer(d[0], ocrmod.ocr_page(d[0], engine))
    return d.tobytes()


def _poster_service(poster_pdf, fm):
    from pdfedit.app.textservice import TextService
    from pdfedit.app.workspace import Workspace
    from pdfedit.core import pagetype
    from pdfedit.core.engine import DocumentStore
    store, ws = DocumentStore(), Workspace()
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "poster.pdf")
    open(path, "wb").write(poster_pdf)
    s = store.open_file(path)
    s.page_types = [pagetype.SCAN]
    ws.append_pages(s.id, [0])
    return store, ws, TextService(store, ws, fm)


def _residue(store, ref, rect_pt):
    """原标题区域内残留的红色字 / 白色描边像素数。"""
    pi = load_page_image(store.page(ref.src, ref.pno))
    x0, y0, x1, y1 = pi.page_to_px(rect_pt)
    c = pi.arr[y0:y1, x0:x1].astype(int)
    red = int(((c[..., 0] - c[..., 2]) > 40).sum())
    white = int((c.min(axis=2) > 235).sum())
    return red, white


@pytest.mark.parametrize("mode", ["vector", "image"])
def test_poster_title_edit_leaves_clean_background(poster_pdf, fm, mode):
    store, ws, ts = _poster_service(poster_pdf, fm)
    ref = ws.refs[0]
    b = next(b for b in ts.blocks(ref) if "消防" in b.text)
    assert ts.is_scan_block(ref, b)
    title = fitz.Rect(b.bbox)
    ts.delete(ref.uid, b.id)
    assert _residue(store, ws.refs[0], title + (-20, -20, 20, 20)) == (0, 0)
    ws.undo()
    b = next(b for b in ts.blocks(ws.refs[0]) if "消防" in b.text)
    ts.edit(ws.refs[0].uid, b.id, "测试文本文稿1", None, scan_opts=ScanEditOptions(mode=mode))
    red, white = _residue(store, ws.refs[0], fitz.Rect(title.x0 + title.width * 0.75, title.y0,
                                                       title.x1 + 20, title.y1))
    assert red == 0 and white == 0            # 原“提示”二字位置干净（新文字更短，不覆盖此处）
    assert "测试文本文稿1" in store.page(ws.refs[0].src, ws.refs[0].pno).get_text()


@pytest.mark.parametrize("mode", ["vector", "image"])
def test_move_edited_text_leaves_nothing_behind(poster_pdf, fm, mode):
    store, ws, ts = _poster_service(poster_pdf, fm)
    b = next(b for b in ts.blocks(ws.refs[0]) if "消防" in b.text)
    ts.edit(ws.refs[0].uid, b.id, "测试文本文稿1", None, scan_opts=ScanEditOptions(mode=mode))
    nb = next(b for b in ts.blocks(ws.refs[0]) if "测试文本" in b.text)
    assert ts.is_scan_block(ws.refs[0], nb) == (mode == "image")   # 矢量写入的是可见文字
    old_rect = fitz.Rect(nb.bbox)
    ts.edit(ws.refs[0].uid, nb.id, nb.text, nb.style(), offset=(0, 200),
            scan_opts=ScanEditOptions(mode=mode, force=True))
    page = store.page(ws.refs[0].src, ws.refs[0].pno)
    assert page.get_text().count("测试文本文稿1") == 1           # 只剩一份
    moved = next(b for b in ts.blocks(ws.refs[0]) if "测试文本" in b.text)
    assert fitz.Rect(moved.bbox).y0 > old_rect.y1                # 在新位置
    assert not page.get_textbox(old_rect).strip()                # 原位置没有文字
    if mode == "image":
        red, _w = _residue(store, ws.refs[0], old_rect)
        assert red == 0                                           # 原位置图像中的字也已擦除
