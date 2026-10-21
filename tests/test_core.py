"""导入、导出、命名、区域页生成。"""
import os

import pymupdf as fitz
import pytest

from conftest import make_pdf, page_tags
from pdfedit.app.exporter import ExportJob, run_export
from pdfedit.app.workspace import Workspace
from pdfedit.core import pagetype
from pdfedit.core.engine import DocumentStore, PasswordRequired
from pdfedit.core.naming import render_name, resolve_conflict
from pdfedit.core.writer import (PageItem, RegionOptions, RegionShape, SaveOptions,
                                 build_region_page, export_pdf)


def load(store, ws, *paths):
    for p in paths:
        s = store.open_file(p)
        ws.append_pages(s.id, range(s.page_count))


def test_merge_export_matches_workspace_order(samples, tmp_path):
    store, ws = DocumentStore(), Workspace()
    load(store, ws, samples["A"], samples["B"])
    uids = [r.uid for r in ws.refs]
    ws.move([uids[12]], 0)
    ws.rotate([uids[1]], 90)
    out = str(tmp_path / "out" / "merged.pdf")
    job = ExportJob("all_one", list(ws.refs), list(range(1, len(ws) + 1)), out)
    run_export(store, job)
    assert page_tags(out)[:3] == ["B-3", "A-1", "A-2"]
    with fitz.open(out) as d:
        assert d.page_count == 15
        assert d[2].rotation == 90
        toc = d.get_toc()
        # 多来源时按来源在工作区中首次出现的顺序分组（B-3 已被拖到最前）
        assert [t[1] for t in toc] == ["B", "B start", "B last", "A", "A start", "A last"]
    # 原文件不被修改
    assert page_tags(samples["A"])[0] == "A-1"


def test_bookmark_of_deleted_page_removed(samples, tmp_path):
    store, ws = DocumentStore(), Workspace()
    load(store, ws, samples["A"])
    ws.delete([ws.refs[-1].uid])
    out = str(tmp_path / "x.pdf")
    export_pdf(store, [PageItem(r.src, r.pno) for r in ws.refs], out)
    with fitz.open(out) as d:
        assert [t[1] for t in d.get_toc()] == ["A start"]


def test_split_each_with_template_and_conflicts(samples, tmp_path):
    store, ws = DocumentStore(), Workspace()
    load(store, ws, samples["C"])
    folder = str(tmp_path / "split")
    job = ExportJob("all_each", list(ws.refs), [1, 2, 3], folder)
    res = run_export(store, job)
    assert [os.path.basename(f) for f in res.files] == ["C_001.pdf", "C_002.pdf", "C_003.pdf"]
    res2 = run_export(store, job)    # 再导一次：自动加后缀
    assert os.path.basename(res2.files[0]) == "C_001(1).pdf"
    job.conflict = "skip"
    res3 = run_export(store, job)
    assert res3.files == [] and len(res3.skipped) == 3


def test_chunk_and_by_source(samples, tmp_path):
    store, ws = DocumentStore(), Workspace()
    load(store, ws, samples["A"], samples["B"])
    ws.move([ws.refs[10].uid], 0)          # B-1 放到最前
    job = ExportJob("by_source", list(ws.refs), list(range(1, 16)), str(tmp_path / "src"))
    res = run_export(store, job)
    names = sorted(os.path.basename(f) for f in res.files)
    assert names == ["A.pdf", "B.pdf"]
    assert page_tags(str(tmp_path / "src" / "B.pdf")) == ["B-1", "B-2", "B-3", "B-4", "B-5"]
    job = ExportJob("chunk", list(ws.refs), list(range(1, 16)), str(tmp_path / "chunk"), chunk_size=4)
    assert len(run_export(store, job).files) == 4


def test_export_images(samples, tmp_path):
    store, ws = DocumentStore(), Workspace()
    load(store, ws, samples["C"])
    job = ExportJob("images", list(ws.refs)[:1], [1], str(tmp_path / "img"), image_dpi=72)
    f = run_export(store, job).files[0]
    assert f.endswith(".png") and os.path.getsize(f) > 0


def test_encrypted_input_and_output(tmp_path):
    p = make_pdf(tmp_path / "plain.pdf", 2, "E")
    with fitz.open(p) as d:
        d.save(str(tmp_path / "enc.pdf"), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="123", owner_pw="456")
    store = DocumentStore()
    with pytest.raises(PasswordRequired):
        store.open_file(str(tmp_path / "enc.pdf"))
    with pytest.raises(PasswordRequired):
        store.open_file(str(tmp_path / "enc.pdf"), "wrong")
    s = store.open_file(str(tmp_path / "enc.pdf"), "123")
    out = str(tmp_path / "out_enc.pdf")
    export_pdf(store, [PageItem(s.id, 0)], out, SaveOptions(user_pw="abc"))
    with fitz.open(out) as d:
        assert d.needs_pass and d.authenticate("abc")


def test_resource_dedupe(tmp_path):
    """同一页复制多次导出，字体只保存一份，体积不随页数线性增长太多。"""
    p = make_pdf(tmp_path / "f.pdf", 1, "F")
    store = DocumentStore()
    s = store.open_file(p)
    one, many = str(tmp_path / "1.pdf"), str(tmp_path / "50.pdf")
    export_pdf(store, [PageItem(s.id, 0)], one)
    export_pdf(store, [PageItem(s.id, 0)] * 50, many)
    with fitz.open(many) as d:
        fonts = {f[0] for pg in d for f in pg.get_fonts()}
    assert len(fonts) == 1


def test_image_import(tmp_path):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 100), False)
    pix.set_rect(pix.irect, (255, 0, 0))
    img = str(tmp_path / "red.png")
    pix.save(img)
    store = DocumentStore()
    s = store.open_file(img)
    assert s.page_count == 1
    assert pagetype.classify(s.doc[0]) == pagetype.SCAN


def test_page_types(tmp_path):
    store = DocumentStore()
    s = store.open_file(make_pdf(tmp_path / "t.pdf", 1, "Hello world text"))
    assert pagetype.classify(s.doc[0]) == pagetype.TEXT
    d = fitz.open()
    p = d.new_page()
    for i in range(300):
        p.draw_line((10, i + 10), (300, i + 10))
    d.new_page()
    d.save(str(tmp_path / "o.pdf"))
    s2 = store.open_file(str(tmp_path / "o.pdf"))
    assert pagetype.classify(s2.doc[0]) == pagetype.OUTLINE
    assert pagetype.classify(s2.doc[1]) == pagetype.BLANK


def test_naming():
    v = {"原文件名": "图纸", "工作区页码": 7, "原页码": 3, "序号": 12}
    assert render_name("{原文件名}_{序号:3}", v) == "图纸_012"
    assert render_name("{原文件名}-P{原页码:2}-{工作区页码}", v) == "图纸-P03-7"
    assert render_name("a/b:{序号}", v) == "a_b_12"
    with pytest.raises(ValueError):
        render_name("{不存在}", v)


def test_resolve_conflict(tmp_path):
    reserved = set()
    a = resolve_conflict(str(tmp_path / "x.pdf"), "overwrite", reserved)
    b = resolve_conflict(str(tmp_path / "x.pdf"), "overwrite", reserved)  # 同批次不互相覆盖
    assert a != b


# ---------------------------------------------------------------- 区域页

def cad_like(path):
    d = fitz.open()
    p = d.new_page(width=1190, height=842)       # A3 横向
    p.draw_rect(fitz.Rect(100, 100, 300, 250), color=(0, 0, 1))
    p.insert_text((120, 180), "PART-A", fontsize=20)
    p.insert_text((800, 600), "TITLE-BLOCK", fontsize=20)
    d.save(path)
    return path


def test_region_rect_keeps_vector_text(tmp_path):
    store = DocumentStore()
    s = store.open_file(cad_like(str(tmp_path / "cad.pdf")))
    shape = RegionShape([(90, 90), (310, 90), (310, 260), (90, 260)])
    out = build_region_page(store, s.id, 0, 0, shape)
    pg = out[0]
    assert abs(pg.rect.width - 220) < 0.5 and abs(pg.rect.height - 170) < 0.5
    assert "PART-A" in pg.get_text()            # 矢量文字仍可搜索
    assert pg.get_images() == []                 # 未被转成图片


def test_region_fit_a4_and_remove_outside(tmp_path):
    store = DocumentStore()
    s = store.open_file(cad_like(str(tmp_path / "cad.pdf")))
    shape = RegionShape([(90, 90), (310, 90), (310, 260), (90, 260)])
    out = build_region_page(store, s.id, 0, 0, shape,
                            RegionOptions(size_mode="A4", margin_mm=10, remove_outside=True))
    pg = out[0]
    assert pg.rect.width > pg.rect.height        # 选区横向 → A4 横向
    assert abs(pg.rect.width - 842) < 1
    data = out.tobytes()
    assert b"TITLE-BLOCK" not in data            # 选区外文字被真正移除（非隐藏）
    assert "TITLE" not in pg.get_text()
    assert "PART-A" in pg.get_text()


def test_region_on_rotated_page(tmp_path):
    path = cad_like(str(tmp_path / "cad.pdf"))
    with fitz.open(path) as d:
        d[0].set_rotation(90)
        d.save(str(tmp_path / "rot.pdf"))
    store = DocumentStore()
    s = store.open_file(str(tmp_path / "rot.pdf"))
    shape = RegionShape([(90, 90), (310, 90), (310, 260), (90, 260)])
    out = build_region_page(store, s.id, 0, 0, shape)
    pg = out[0]
    # 页面旋转 90°，看到的选区是竖向：宽 170、高 220
    assert abs(pg.rect.width - 170) < 0.5 and abs(pg.rect.height - 220) < 0.5
    assert "PART-A" in pg.get_text()


def test_region_polygon(tmp_path):
    store = DocumentStore()
    s = store.open_file(cad_like(str(tmp_path / "cad.pdf")))
    tri = RegionShape([(90, 90), (310, 90), (90, 260)], is_rect=False)
    out = build_region_page(store, s.id, 0, 0, tri, RegionOptions(mark_source=True, source_label="cad 第1页"))
    pg = out[0]
    assert pg.read_contents().startswith(b"q ")
    # 三角形外的右下角应为白色
    pix = pg.get_pixmap()
    assert pix.pixel(pix.width - 5, pix.height - 30) == (255, 255, 255)
    # 来源标注可被搜索复制，且只嵌入字体子集
    assert "cad 第1页" in pg.get_text()
    assert len(out.tobytes(garbage=3, deflate=True)) < 100_000


def test_project_roundtrip(samples, tmp_path):
    from pdfedit.app import project
    from pdfedit.app.regions import RegionStore
    store, ws, regs = DocumentStore(), Workspace(), RegionStore()
    load(store, ws, samples["A"], samples["B"])
    ws.move([ws.refs[12].uid], 0)
    ws.rotate([ws.refs[1].uid], 90)
    doc = build_region_page(store, ws.refs[0].src, ws.refs[0].pno, 0,
                            RegionShape([(50, 50), (300, 50), (300, 200), (50, 200)]))
    pno = store.append_generated(doc)[0]
    store.gen_origins[pno] = (ws.refs[0].src, ws.refs[0].pno)
    ws.insert([ws.new_ref("__gen__", pno, 0, "region")], 1)
    regs.add(ws.refs[2].uid, [(10, 10), (90, 10), (90, 90), (10, 90)], True, "标题栏")
    path = str(tmp_path / "w.pdfproj")
    project.save_project(path, store, ws, regs)

    store2, ws2, regs2 = DocumentStore(), Workspace(), RegionStore()
    problems = project.load_project(path, store2, ws2, regs2, lambda p, sid: store2.open_file(p, None, sid))
    assert problems == []
    assert ws2.refs == ws.refs
    assert regs2.get(ws.refs[2].uid)[0].name == "标题栏"
    out = str(tmp_path / "re.pdf")
    export_pdf(store2, [PageItem(r.src, r.pno, r.rot) for r in ws2.refs], out)
    tags = page_tags(out)
    assert tags[0] == "B-3" and tags[2] == "A-1" and len(tags) == 16
