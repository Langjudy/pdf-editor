"""界面回归测试（离屏运行）：Delete 键在选中批注 / 文字块时不应删除整页。"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import make_pdf  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from pdfedit.app import project
    monkeypatch.setattr(project, "app_data_dir", lambda: str(tmp_path))   # 不碰真实的自动保存
    from pdfedit.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    w.import_files([make_pdf(tmp_path / "a.pdf", 2, "Page")])
    app.processEvents()
    yield w
    w.ws.dirty = False
    w.close()


def press_delete(w):
    w.viewer.setFocus()
    QTest.keyClick(w.viewer.viewport(), Qt.Key_Delete)
    QApplication.processEvents()


def test_delete_key_removes_selected_annot_not_page(win):
    uid = win.ws.refs[0].uid
    from pdfedit.core.annots import Style
    aid = win.annots.add_shape(uid, "rect", [(100, 200), (200, 260)], Style())
    win.set_tool("a_annot")
    win.viewer.select_annot(uid, aid)
    press_delete(win)
    assert len(win.ws) == 2
    assert not win.annots.annots(win.ws.refs[0])


def test_delete_key_removes_selected_text_block_not_page(win):
    win.set_tool("text")
    uid = win.ws.refs[0].uid
    blocks = win.texts.blocks(win.ws.refs[0])
    win.viewer.set_selected_block(uid, blocks[0].id)
    press_delete(win)
    assert len(win.ws) == 2
    assert "Page-1" not in win.store.page(win.ws.refs[0].src, win.ws.refs[0].pno).get_text()


def test_delete_key_still_deletes_page_when_nothing_selected(win):
    win.set_tool("hand")
    win.thumbs.select_rows([1], 1)
    press_delete(win)
    assert len(win.ws) == 1


if sys.platform != "win32":
    pytestmark = pytest.mark.skip("界面测试依赖 Windows 字体环境")
