"""批注服务：版本化增删改（可撤销）、ID 跨版本有效、导出保留 / 拼合 / 清理隐私。"""
import pymupdf as fitz

from conftest import make_pdf
from pdfedit.app.annotservice import AnnotService
from pdfedit.app.textservice import TextService
from pdfedit.app.workspace import Workspace
from pdfedit.core import annots as A
from pdfedit.core.engine import DocumentStore
from pdfedit.core.fonts import FontManager
from pdfedit.core.writer import PageItem, SaveOptions, export_pdf

STYLE = A.Style(color=(1, 0, 0), opacity=0.8, width=2, author="审核人")


def setup(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", 3, "Doc")
    with fitz.open(p) as d:
        d.set_metadata({"author": "原作者", "title": "内部资料"})
        d.save(str(tmp_path / "b.pdf"))
    store, ws = DocumentStore(), Workspace()
    s = store.open_file(str(tmp_path / "b.pdf"))
    ws.append_pages(s.id, range(s.page_count))
    texts = TextService(store, ws, FontManager())
    return store, ws, AnnotService(store, ws, texts)


def ids(svc, ref):
    return {a.id: a for a in svc.annots(ref)}


def test_add_update_move_delete_with_undo(tmp_path):
    store, ws, svc = setup(tmp_path)
    uid = ws.refs[0].uid
    original = ws.refs[0]
    aid = svc.add_shape(uid, "rect", [(100, 200), (200, 260)], STYLE)
    assert aid in ids(svc, ws.refs[0]) and original != ws.refs[0]
    svc.update(uid, aid, color=(0, 0, 1), content="请修改")          # 新版本上按 ID 找到同一批注
    svc.move(uid, aid, 30, 0)
    a = ids(svc, ws.refs[0])[aid]
    assert a.color == (0.0, 0.0, 1.0) and a.content == "请修改" and a.rect.x0 > 120
    svc.delete(uid, aid)
    assert aid not in ids(svc, ws.refs[0])
    for _ in range(4):
        ws.undo()
    assert ws.refs[0] == original and not svc.annots(ws.refs[0])


def test_all_annots_listing(tmp_path):
    store, ws, svc = setup(tmp_path)
    svc.add_note(ws.refs[0].uid, (300, 100), "第一页便签", STYLE)
    svc.add_markup(ws.refs[2].uid, "hl", [fitz.Rect(72, 80, 200, 104).quad], A.Style(author="李四"))
    rows = svc.all_annots()
    assert [(i, a.label, a.author) for i, _r, a in rows] == [(0, "便签", "审核人"), (2, "高亮", "李四")]
    txt = svc.summary(rows)
    assert "第 1 页 | 便签" in txt and "第 3 页 | 高亮" in txt


def test_export_keep_flatten_and_scrub(tmp_path):
    store, ws, svc = setup(tmp_path)
    svc.add_stamp(ws.refs[0].uid, (300, 300, 450, 360), A.make_stamp_png("已审核"), STYLE, "已审核")
    svc.add_shape(ws.refs[1].uid, "ellipse", [(100, 200), (200, 260)], STYLE)
    items = [PageItem(r.src, r.pno, r.rot) for r in ws.refs]
    keep = str(tmp_path / "keep.pdf")
    export_pdf(store, items, keep, SaveOptions())
    with fitz.open(keep) as d:
        assert [len(list(p.annots())) for p in d] == [1, 1, 0]
    flat = str(tmp_path / "flat.pdf")
    export_pdf(store, items, flat, SaveOptions(flatten_annots=True, scrub=True))
    with fitz.open(flat) as d:
        assert [len(list(p.annots())) for p in d] == [0, 0, 0]
        assert d[1].get_drawings()                 # 椭圆已拼合进页面
        assert not d.metadata.get("author") and not d.metadata.get("title")


def test_redaction_flag_propagates(tmp_path):
    store, ws, svc = setup(tmp_path)
    uid = ws.refs[0].uid
    svc.redact(uid, [fitz.Rect(60, 70, 300, 110)])
    assert "Doc-1" not in store.page(ws.refs[0].src, ws.refs[0].pno).get_text()
    assert svc.has_redactions(ws.refs)
    svc.add_note(uid, (300, 300), "涂黑后再加批注", STYLE)       # 新版本继承“已涂黑”标记
    assert svc.has_redactions(ws.refs)
    ws.undo()
    ws.undo()
    assert not svc.has_redactions(ws.refs)
