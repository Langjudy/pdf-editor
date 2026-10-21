import os
import sys

import pymupdf as fitz
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_pdf(path, n, tag, size=(595, 842), toc=True):
    doc = fitz.open()
    for i in range(n):
        p = doc.new_page(width=size[0], height=size[1])
        p.insert_text((72, 100), f"{tag}-{i + 1}", fontsize=24)
    if toc and n:
        doc.set_toc([[1, f"{tag} start", 1]] + ([[2, f"{tag} last", n]] if n > 1 else []))
    doc.save(path)
    doc.close()
    return str(path)


@pytest.fixture
def samples(tmp_path):
    return {
        "A": make_pdf(tmp_path / "A.pdf", 10, "A"),
        "B": make_pdf(tmp_path / "B.pdf", 5, "B"),
        "C": make_pdf(tmp_path / "C.pdf", 3, "C"),
        "dir": tmp_path,
    }


def page_tags(path):
    """返回导出文件每页的标记文字，如 ['A-1', 'B-2']。"""
    with fitz.open(path) as d:
        return [p.get_text().strip().split("\n")[0] for p in d]


# 以下测试直接使用 Windows 系统字体文件（宋体、黑体、仿宋、楷体），其他平台跳过
if sys.platform != "win32":
    collect_ignore = ["test_textedit.py", "test_textservice.py", "test_scan.py",
                      "test_scanservice.py", "test_annots.py"]
