"""矢量文字编辑（技术要求第 7 节）。"""
import pymupdf as fitz
import pytest

from pdfedit.core.fonts import FontManager
from pdfedit.core.textedit import (TextStyle, add_text, compile_pattern, delete_block, edit_block,
                                   extract_blocks, find_in_blocks, replace_matches)

SIMSUN = r"C:\Windows\Fonts\simsun.ttc"


@pytest.fixture(scope="module")
def fm():
    m = FontManager()
    m.ensure_loaded()
    return m


def word_like_page():
    """模拟 Word 导出：嵌入 TrueType 字体，两段文字 + 一个表格框线。"""
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 100), "合同编号：HT-2024-001", fontsize=12, fontname="F1", fontfile=SIMSUN)
    p.insert_text((72, 112), "甲方：某某科技有限公司", fontsize=12, fontname="F1", fontfile=SIMSUN)
    para = ("本合同自双方签字盖章之日起生效，有效期一年。\n"
            "期满前三十日内双方可协商续签事宜。")
    p.insert_textbox(fitz.Rect(72, 200, 330, 260), para, fontsize=11, fontname="F1", fontfile=SIMSUN)
    p.draw_rect(fitz.Rect(60, 80, 400, 300), color=(0, 0, 1))
    p.insert_text((72, 400), "Total amount: USD 1000", fontsize=11)   # Helvetica，未嵌入
    return d, p


def block_with(blocks, s):
    return next(b for b in blocks if s in b.text)


def texts(page):
    return page.get_text()


def test_extract_blocks():
    _d, p = word_like_page()
    blocks = extract_blocks(p)
    assert any("合同编号" in b.text for b in blocks)
    b = block_with(blocks, "本合同")
    assert len(b.lines) >= 2


def test_edit_line_true_delete_and_neighbors_intact(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "合同编号")
    ln = next(l for l in b.lines if "合同编号" in l.text)
    one = type(b)(b.id, ln.bbox, [ln])
    res = edit_block(p, one, "合同编号：HT-2025-888", one.style(), fm)
    t = texts(p)
    assert "HT-2025-888" in t and "HT-2024-001" not in t
    assert "甲方：某某科技有限公司" in t                     # 紧邻下一行不受影响
    assert len(p.get_drawings()) == 1                       # 框线保留
    assert not res.substituted                               # 原嵌入字体（宋体）可覆盖
    new = block_with(extract_blocks(p), "HT-2025-888").lines[0]
    assert abs(new.origin[1] - ln.origin[1]) < 0.5 and abs(new.origin[0] - ln.origin[0]) < 0.5
    assert abs(new.size - ln.size) < 0.1


def test_edit_twice(fm):
    _d, p = word_like_page()
    for new in ("第一次修改", "第二次修改"):
        b = block_with(extract_blocks(p), "甲方" if new == "第一次修改" else "第一次修改")
        edit_block(p, b, new, b.style(), fm)
    t = texts(p)
    assert "第二次修改" in t and "第一次修改" not in t and "甲方" not in t


def test_paragraph_reflow_stays_within_width(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "本合同")
    longer = b.text.replace("\n", "") + "如有争议，双方应友好协商解决；协商不成的，提交合同签订地人民法院诉讼解决。"
    edit_block(p, b, longer, b.style(), fm)
    nb = block_with(extract_blocks(p), "如有争议")
    assert len(nb.lines) > len(b.lines)
    for ln in nb.lines:
        assert ln.bbox[2] <= b.bbox[2] + 2


def test_style_change(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "甲方")
    st = TextStyle(font="SimHei", size=16, color=(1, 0, 0), bold=True, char_spacing=2)
    res = edit_block(p, b, "甲方：新公司", st, fm)
    assert "黑体" in res.font_name
    ln = block_with(extract_blocks(p), "新公司").lines[0]
    assert abs(ln.size - 16) < 0.1 and ln.color == 0xFF0000
    assert ln.bbox[2] - ln.bbox[0] > 16 * 6 + 2 * 5 - 1      # 6 个字 + 5 个字间距


def test_substitute_font_for_missing_glyphs(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "Total")
    res = edit_block(p, b, "总金额：1000 美元", b.style(), fm)
    assert res.substituted                     # Helvetica/Arial 没有中文 → 默认中文字体
    assert "总金额：1000 美元" in texts(p)


def test_move_block(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "甲方")
    edit_block(p, b, b.text, b.style(), fm, offset=(150, 550))     # 移到空白处
    found = [x for x in extract_blocks(p) if "甲方" in x.text]
    assert len(found) == 1                                          # 原位置已删除
    nb = found[0]
    assert abs(nb.bbox[0] - (b.bbox[0] + 150)) < 1 and abs(nb.bbox[1] - (b.bbox[1] + 550)) < 1


def test_delete_block_keeps_graphics(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "本合同")
    delete_block(p, b)
    t = texts(p)
    assert "本合同" not in t and "期满前" not in t and "合同编号" in t
    assert len(p.get_drawings()) == 1


def test_add_text_box(fm):
    _d, p = word_like_page()
    add_text(p, (100, 500), "新增批注文字\n第二行", TextStyle(size=14, color=(0, 0, 1)), fm)
    blocks = extract_blocks(p)
    b = block_with(blocks, "新增批注文字")
    assert abs(b.lines[0].origin[0] - 100) < 0.5 and abs(b.lines[0].origin[1] - 500) < 0.5
    assert "第二行" in texts(p)


def test_rotated_page_edit_keeps_position(fm):
    d, p = word_like_page()
    p.set_rotation(90)
    b = block_with(extract_blocks(p), "甲方")
    edit_block(p, b, "甲方：旋转页测试", b.style(), fm)
    assert p.rotation == 90
    nb = block_with(extract_blocks(p), "旋转页测试")
    assert abs(nb.lines[0].origin[0] - b.lines[0].origin[0]) < 0.5
    assert abs(nb.lines[0].origin[1] - b.lines[0].origin[1]) < 0.5


def test_rotated_text_keeps_direction(fm):
    d = fitz.open()
    p = d.new_page()
    p.insert_text((200, 400), "竖向标注文字", fontsize=12, fontname="F1", fontfile=SIMSUN, rotate=90)
    b = block_with(extract_blocks(p), "竖向标注")
    edit_block(p, b, "改后的竖向标注", b.style(), fm)
    nb = block_with(extract_blocks(p), "改后的")
    assert all(abs(a - c) < 1e-3 for a, c in zip(nb.lines[0].dir, b.lines[0].dir))
    assert abs(nb.lines[0].origin[0] - b.lines[0].origin[0]) < 0.5
    assert abs(nb.lines[0].origin[1] - b.lines[0].origin[1]) < 0.5


def test_find_options():
    _d, p = word_like_page()
    blocks = extract_blocks(p)
    assert len(find_in_blocks(blocks, compile_pattern("total"))) == 1
    assert len(find_in_blocks(blocks, compile_pattern("total", case=True))) == 0
    assert len(find_in_blocks(blocks, compile_pattern("USD", whole=True))) == 1
    assert len(find_in_blocks(blocks, compile_pattern("US", whole=True))) == 0
    assert [m.text for m in find_in_blocks(blocks, compile_pattern(r"HT-\d{4}", regex=True))] == ["HT-2024"]


def test_replace_plain_and_regex(fm):
    _d, p = word_like_page()
    blocks = extract_blocks(p)
    pat = compile_pattern("合同")
    ms = find_in_blocks(blocks, pat)
    n, _subs = replace_matches(p, blocks, ms, pat, "协议", False, fm)
    assert n == len(ms) == 2          # “合同编号”“本合同”
    t = texts(p)
    assert "合同" not in t and "协议编号" in t and "本协议自双方" in t
    blocks = extract_blocks(p)
    pat = compile_pattern(r"HT-(\d{4})-(\d+)", regex=True)
    ms = find_in_blocks(blocks, pat)
    replace_matches(p, blocks, ms, pat, r"NO.\2/\1", True, fm)
    assert "NO.001/2024" in texts(p)


def test_replace_only_selected_hits(fm):
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 100), "苹果 苹果 苹果", fontsize=12, fontname="F1", fontfile=SIMSUN)
    blocks = extract_blocks(p)
    pat = compile_pattern("苹果")
    ms = find_in_blocks(blocks, pat)
    replace_matches(p, blocks, [ms[1]], pat, "香蕉", False, fm)
    assert "苹果 香蕉 苹果" in texts(p)


def test_output_file_small_and_fonts_subset(fm, tmp_path):
    d, p = word_like_page()
    before = len(d.tobytes(garbage=3, deflate=True))
    b = block_with(extract_blocks(p), "Total")
    edit_block(p, b, "总金额：一千美元", b.style(), fm)       # 用系统宋体（18 MB）替代
    after = len(d.tobytes(garbage=3, deflate=True))
    assert after - before < 60_000                          # 只嵌入子集


def test_bigger_font_lines_do_not_overlap(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "本合同")
    edit_block(p, b, b.text, TextStyle(font="SimHei", size=18), fm)
    nb = block_with(extract_blocks(p), "本合同")
    for a, c in zip(nb.lines, nb.lines[1:]):
        assert c.bbox[1] >= a.bbox[3] - 1          # 下一行顶边不高于上一行底边


def test_re_edit_uses_same_name_system_font_without_warning(fm):
    _d, p = word_like_page()
    b = block_with(extract_blocks(p), "Total")
    r1 = edit_block(p, b, "总计金额", b.style(), fm)
    assert r1.substituted                            # Helvetica → 宋体，属于替代
    b = block_with(extract_blocks(p), "总计金额")
    r2 = edit_block(p, b, "总计金额（含税）", b.style(), fm)
    assert not r2.substituted                        # 再次编辑：复用原字体 / 系统同名字体，不再提示替代
    assert "SimSun" in r2.font_name or "宋体" in r2.font_name
