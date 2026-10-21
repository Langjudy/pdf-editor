"""扫描服务：OCR 升级不占撤销历史、扫描页编辑 / 替换 / 擦除可撤销、恢复原始页面。"""
import pymupdf as fitz
import pytest

from pdfedit.app.scanservice import ScanService
from pdfedit.app.textservice import TextService
from pdfedit.app.workspace import Workspace
from pdfedit.core import pagetype
from pdfedit.core.engine import DocumentStore
from pdfedit.core.fonts import FontManager
from pdfedit.core.scan import PreOptions
from pdfedit.core.scanedit import ScanEditOptions
from test_scan import form_page, scan_doc


@pytest.fixture(scope="module")
def fm():
    m = FontManager()
    m.ensure_loaded()
    return m


@pytest.fixture(scope="module")
def scan_file(tmp_path_factory):
    d = scan_doc(page_fn=form_page)
    d2 = scan_doc(angle=3.0, page_fn=form_page)
    d.insert_pdf(d2)
    path = str(tmp_path_factory.mktemp("scan") / "单据扫描.pdf")
    d.save(path)
    return path


@pytest.fixture(scope="module")
def ocr_cache():
    return {}


def setup(path, fm, ocr_cache):
    store, ws = DocumentStore(), Workspace()
    s = store.open_file(path)
    s.page_types = [pagetype.SCAN] * s.page_count
    ws.append_pages(s.id, range(s.page_count))
    texts = TextService(store, ws, fm)
    scans = ScanService(store, ws, texts)
    # OCR 结果在测试间复用：把缓存的识别行直接写成新版本
    if "lines" not in ocr_cache:
        new = scans.ocr_key(s.id, 0)
        from pdfedit.core.ocr import ocr_page
        ocr_cache["lines"] = ocr_page(store.page(s.id, 0), scans.engine)
        scans.commit_ocr(s.id, 0, new)
    else:
        from pdfedit.core.ocr import write_text_layer
        new, page = scans._copy(s.id, 0)
        write_text_layer(page, ocr_cache["lines"])
        store.gen_ocr.add(new)
        scans.commit_ocr(s.id, 0, new)
    return store, ws, texts, scans, s


def text_of(store, ref):
    return store.page(ref.src, ref.pno).get_text()


def test_ocr_upgrade_survives_undo(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    assert "北京某某贸易公司" in text_of(store, ws.refs[0])
    assert texts.has_ocr(ws.refs[0])
    # OCR 前做过的操作，撤销后识别结果仍在
    ws2_refs = ws.refs
    ws.rotate([ws.refs[1].uid], 90)
    ws.undo()
    assert ws.refs == ws2_refs and "北京" in text_of(store, ws.refs[0])
    # 撤销到导入之前再重做，OCR 版本依旧
    while ws.can_undo():
        ws.undo()
    ws.redo()
    assert "北京" in text_of(store, ws.refs[0])


def test_scan_blocks_are_lines(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    blocks = texts.blocks(ws.refs[0])
    assert all(len(b.lines) == 1 for b in blocks)
    assert {"客户名称", "北京某某贸易公司", "合同金额", "伍万元整"} <= {b.text for b in blocks}


def test_scan_edit_and_undo(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    ref0 = ws.refs[0]
    b = next(b for b in texts.blocks(ref0) if b.text == "伍万元整")
    res = texts.edit(ref0.uid, b.id, "陆万元整", None, scan_opts=ScanEditOptions(mode="image"))
    assert res.style is not None
    assert "陆万元整" in text_of(store, ws.refs[0])
    ws.undo()
    assert ws.refs[0] == ref0


def test_scan_replace(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    hits = texts.find([(0, ws.refs[0])], "某某")
    assert len(hits) == 1
    n, _subs = texts.replace(hits, "某某", "示例")
    assert n == 1 and "北京示例贸易公司" in text_of(store, ws.refs[0])


def test_erase_and_restore(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    scans.erase(ws.refs[0].uid, fitz.Rect(60, 280, 400, 320))
    assert "第一章" not in text_of(store, ws.refs[0])
    assert scans.restore_original([ws.refs[0].uid]) == 1
    assert ws.refs[0].src == s.id and ws.refs[0].pno == 0
    ws.undo()
    assert ws.refs[0].src == "__gen__"


def test_preprocess_pages(scan_file, fm, ocr_cache):
    store, ws, texts, scans, s = setup(scan_file, fm, ocr_cache)
    changes, skipped, angles = scans.preprocess_pages([ws.refs[1]], PreOptions())
    assert not skipped and abs(angles[0] + 3.0) < 0.3
    ws.replace_many(changes, "图像预处理")
    assert ws.refs[1].src == "__gen__"
    ws.undo()
    assert ws.refs[1].src == s.id
