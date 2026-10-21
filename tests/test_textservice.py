"""文字编辑服务：写时复制、撤销重做、跨页替换、权限密码。"""
import pymupdf as fitz
import pytest

from conftest import page_tags
from pdfedit.app.textservice import EditLocked, TextService
from pdfedit.app.workspace import Workspace
from pdfedit.core.engine import DocumentStore
from pdfedit.core.fonts import FontManager
from pdfedit.core.writer import PageItem, export_pdf

SIMSUN = r"C:\Windows\Fonts\simsun.ttc"


@pytest.fixture(scope="module")
def fm():
    m = FontManager()
    m.ensure_loaded()
    return m


def make_doc(path, pages=3, **save_kw):
    d = fitz.open()
    for i in range(pages):
        p = d.new_page()
        p.insert_text((72, 100), f"第{i + 1}页 项目名称：旧名称", fontsize=12, fontname="F1", fontfile=SIMSUN)
    d.save(str(path), **save_kw)
    return str(path)


def setup(path, fm):
    store, ws = DocumentStore(), Workspace()
    s = store.open_file(path)
    ws.append_pages(s.id, range(s.page_count))
    return store, ws, TextService(store, ws, fm), s


def page_text(store, ref):
    return store.page(ref.src, ref.pno).get_text()


def test_edit_copy_on_write_and_undo(tmp_path, fm):
    path = make_doc(tmp_path / "a.pdf")
    store, ws, ts, src = setup(path, fm)
    ref0 = ws.refs[0]
    b = ts.blocks(ref0)[0]
    ts.edit(ref0.uid, b.id, "第1页 项目名称：新名称", b.style())
    new = ws.refs[0]
    assert new.uid == ref0.uid and new.src == "__gen__"
    assert "新名称" in page_text(store, new)
    assert "旧名称" in page_text(store, ref0)            # 旧版本（源文件页）不变
    assert "旧名称" in fitz.open(path)[0].get_text()      # 原文件不变
    ws.undo()
    assert ws.refs[0] == ref0
    ws.redo()
    assert "新名称" in page_text(store, ws.refs[0])
    # 再编辑一次（在生成页版本上），撤销两次回到原始
    b = ts.blocks(ws.refs[0])[0]
    ts.delete(ws.refs[0].uid, b.id)
    assert "项目名称" not in page_text(store, ws.refs[0])
    ws.undo()
    ws.undo()
    assert ws.refs[0] == ref0


def test_replace_all_pages_single_undo(tmp_path, fm):
    store, ws, ts, _ = setup(make_doc(tmp_path / "a.pdf", pages=4), fm)
    before = ws.refs
    hits = ts.find(list(enumerate(ws.refs)), "旧名称")
    assert len(hits) == 4
    n, _ = ts.replace(hits[:3], "旧名称", "新名称")      # 只替换勾选的前 3 处
    assert n == 3
    assert ["新名称" in page_text(store, r) for r in ws.refs] == [True, True, True, False]
    ws.undo()
    assert ws.refs == before


def test_export_after_edit(tmp_path, fm):
    store, ws, ts, _ = setup(make_doc(tmp_path / "a.pdf"), fm)
    b = ts.blocks(ws.refs[1])[0]
    ts.edit(ws.refs[1].uid, b.id, "第2页 已修改", b.style())
    out = str(tmp_path / "out.pdf")
    export_pdf(store, [PageItem(r.src, r.pno, r.rot) for r in ws.refs], out)
    assert page_tags(out) == ["第1页 项目名称：旧名称", "第2页 已修改", "第3页 项目名称：旧名称"]


def test_permission_password(tmp_path, fm):
    perm = fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY      # 不允许修改
    path = make_doc(tmp_path / "locked.pdf", encryption=fitz.PDF_ENCRYPT_AES_256,
                    owner_pw="owner123", user_pw="", permissions=perm)
    store, ws, ts, src = setup(path, fm)
    assert src.edit_locked
    b = ts.blocks(ws.refs[0])[0]
    with pytest.raises(EditLocked):
        ts.edit(ws.refs[0].uid, b.id, "x", b.style())
    assert not ts.unlock(src.id, "wrong")
    assert ts.unlock(src.id, "owner123")
    ts.edit(ws.refs[0].uid, b.id, "解锁后修改", b.style())
    assert "解锁后修改" in page_text(store, ws.refs[0])


def test_unlocked_plain_file(tmp_path, fm):
    _store, _ws, _ts, src = setup(make_doc(tmp_path / "a.pdf"), fm)
    assert not src.edit_locked


def test_add_text_on_region_like_generated_page(tmp_path, fm):
    store, ws, ts, _ = setup(make_doc(tmp_path / "a.pdf"), fm)
    from pdfedit.core.textedit import TextStyle
    ts.add(ws.refs[2].uid, (72, 300), "新增文字", TextStyle(size=20))
    ts.add(ws.refs[2].uid, (72, 400), "再加一行", TextStyle(size=20))
    t = page_text(store, ws.refs[2])
    assert "新增文字" in t and "再加一行" in t and "旧名称" in t
