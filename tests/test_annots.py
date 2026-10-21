"""批注与图章（技术要求第 9 节）、涂黑（7.5）。"""
import numpy as np
import pymupdf as fitz
import pytest

from pdfedit.core import annots as A
from pdfedit.core.textedit import extract_blocks

SIMSUN = r"C:\Windows\Fonts\simsun.ttc"


def make_page(rotation=0):
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 100), "第一行：需要高亮的重要内容", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((72, 130), "第二行：合同金额五万元整", fontsize=14, fontname="F1", fontfile=SIMSUN)
    p.insert_text((72, 400), "机密信息：身份证号 110101199001011234", fontsize=12, fontname="F1", fontfile=SIMSUN)
    p.draw_rect(fitz.Rect(60, 380, 400, 410), color=(0, 0, 1))
    if rotation:
        p.set_rotation(rotation)
    return d, p


def reopen(d):
    d2 = fitz.open("pdf", d.tobytes())
    return d2, d2[0]


STYLE = A.Style(color=(1, 0, 0), opacity=0.6, width=2, author="张三")


def test_markups_from_text_selection():
    d, p = make_page()
    chars = A.chars_in_order(extract_blocks(p))
    i0 = A.nearest_char(chars, 75, 95)            # “第”
    i1 = A.nearest_char(chars, 200, 125)          # 第二行中部
    quads = A.selection_quads(chars, i0, i1)
    assert len(quads) == 2                        # 跨两行 → 两个四边形
    text = A.selection_text(chars, i0, i1)
    assert text.startswith("第一行") and "\n第二行" in text
    ids = [A.add_markup(p, k, quads, STYLE, text) for k in ("hl", "ul", "so", "sq")]
    d2, p2 = reopen(d)
    got = {a.id: a for a in A.list_annots(p2)}
    assert [got[i].label for i in ids] == ["高亮", "下划线", "删除线", "波浪线"]
    a = got[ids[0]]
    assert a.author == "张三" and abs(a.opacity - 0.6) < 0.01 and a.color == (1.0, 0.0, 0.0)


def test_note_freetext_shapes_ink_and_stamp_roundtrip():
    d, p = make_page()
    ids = {
        "note": A.add_note(p, (300, 60), "请核对金额", STYLE),
        "free": A.add_freetext(p, (300, 150, 520, 190), "文本框批注：中文内容", A.Style(color=(0, 0, 1), author="李四")),
        "rect": A.add_shape(p, "rect", [(60, 200), (200, 260)], STYLE),
        "ellipse": A.add_shape(p, "ellipse", [(220, 200), (300, 260)], STYLE),
        "line": A.add_shape(p, "line", [(60, 280), (200, 280)], STYLE),
        "arrow": A.add_shape(p, "arrow", [(60, 300), (200, 320)], STYLE),
        "polygon": A.add_shape(p, "polygon", [(320, 200), (400, 210), (380, 260)], STYLE),
        "cloud": A.add_shape(p, "cloud", [(420, 200), (520, 200), (520, 260), (420, 260)], STYLE),
        "ink": A.add_ink(p, [[(60, 450), (80, 470), (100, 450)], [(120, 450), (140, 480)]], STYLE),
        "stamp": A.add_stamp(p, (350, 500, 500, 560), A.make_stamp_png("已审核"), STYLE, "已审核"),
    }
    d2, p2 = reopen(d)
    got = {a.id: a for a in A.list_annots(p2)}
    labels = {k: got[v].label for k, v in ids.items()}
    assert labels == {"note": "便签", "free": "文本框", "rect": "矩形", "ellipse": "椭圆", "line": "直线",
                      "arrow": "箭头", "polygon": "多边形", "cloud": "云线", "ink": "画笔", "stamp": "图章"}
    assert got[ids["note"]].content == "请核对金额"
    assert got[ids["free"]].author == "李四"
    st = got[ids["stamp"]]
    assert abs(st.rect.width - 150) < 0.5 and abs(st.rect.height - 60) < 0.5   # 保持指定尺寸
    # 图章确实显示为红色印章（外观为图片）
    pix = p2.get_pixmap(clip=st.rect, dpi=72)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    assert ((arr[..., 0] > 150) & (arr[..., 1] < 100)).sum() > 50
    # 文本框中文可显示（有墨迹）
    pix = p2.get_pixmap(clip=got[ids["free"]].rect, dpi=72)
    assert (np.frombuffer(pix.samples, np.uint8) < 128).sum() > 50


def test_update_move_delete():
    d, p = make_page()
    rid = A.add_shape(p, "rect", [(60, 200), (200, 260)], STYLE)
    lid = A.add_shape(p, "arrow", [(60, 300), (200, 320)], STYLE)
    sid = A.add_stamp(p, (350, 500, 500, 560), A.make_stamp_png("作废"), STYLE)
    fid = A.add_freetext(p, (300, 150, 520, 190), "原文字", A.Style(color=(0, 0, 1)))
    assert A.update_annot(p, rid, color=(0, 0.5, 0), opacity=0.3, width=4, content="改过的说明")
    assert A.update_annot(p, fid, content="新的文本框内容", color=(1, 0, 0))
    assert A.move_annot(p, rid, 10, 20) and A.move_annot(p, lid, 5, 5) and A.move_annot(p, sid, -50, 0)
    d2, p2 = reopen(d)
    got = {a.id: a for a in A.list_annots(p2)}
    r = got[rid]
    assert r.color == (0.0, 0.5, 0.0) and abs(r.opacity - 0.3) < 0.01 and r.width == 4
    assert r.content == "改过的说明"
    assert abs(r.rect.x0 - (60 - 2 + 10)) < 3 and abs(r.rect.y0 - (200 - 2 + 20)) < 3
    assert got[lid].label == "箭头" and abs(got[lid].vertices[0][0] - 65) < 0.5   # 移动后仍是箭头
    assert abs(got[sid].rect.x0 - 300) < 0.5 and abs(got[sid].rect.width - 150) < 0.5
    assert got[fid].content == "新的文本框内容"
    assert A.delete_annot(p2, rid)
    assert rid not in {a.id for a in A.list_annots(p2)}


def test_annots_on_rotated_page_use_unrotated_coords():
    d, p = make_page(rotation=90)
    chars = A.chars_in_order(extract_blocks(p))
    quads = A.selection_quads(chars, A.nearest_char(chars, 75, 95), A.nearest_char(chars, 300, 95))
    aid = A.add_markup(p, "hl", quads, STYLE)
    a = next(x for x in A.list_annots(p) if x.id == aid)
    # 高亮框比文字框四周略大（MuPDF 留白），关键是落在未旋转坐标的同一位置
    assert abs(a.rect.y0 - quads[0].rect.y0) < 6 and abs(a.rect.x0 - quads[0].rect.x0) < 6
    assert p.rotation == 90


def test_redact_removes_text_and_overlapping_annots():
    d, p = make_page()
    A.add_note(p, (200, 395), "敏感批注", STYLE)
    A.redact_area(p, fitz.Rect(60, 385, 400, 405))
    t = p.get_text()
    assert "110101199001011234" not in t and "机密信息" not in t
    assert "第一行" in t
    assert b"110101199001011234" not in d.tobytes(garbage=3)
    assert not [a for a in A.list_annots(p) if a.content == "敏感批注"]
    pix = p.get_pixmap(clip=fitz.Rect(150, 390, 300, 400), dpi=72)
    assert np.frombuffer(pix.samples, np.uint8).mean() < 30          # 黑块


def test_flatten_and_scrub():
    d, p = make_page()
    A.add_shape(p, "rect", [(60, 200), (200, 260)], STYLE)
    d.set_metadata({"author": "某人", "title": "内部文件"})
    A.flatten(d)
    assert not A.list_annots(d[0])
    assert d[0].get_drawings()                          # 批注已变成页面内容
    A.scrub_private(d)
    assert not any(v for v in d.metadata.values() if v and v not in ("PDF 1.7", "PDF 1.4"))


def test_summary_text():
    d, p = make_page()
    A.add_note(p, (300, 60), "请核对金额\n第二行说明", STYLE)
    txt = A.summary_text([(3, a) for a in A.list_annots(p)])
    assert "第 3 页 | 便签 | 作者：张三" in txt and "    请核对金额" in txt
