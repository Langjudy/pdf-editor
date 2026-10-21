"""工作区页面顺序规则（技术要求 5.4 验收要点）与页面操作。"""
import pytest

from pdfedit.app.workspace import Workspace, parse_range


def tags(ws):
    return [f"{r.src}-{r.pno + 1}" for r in ws.refs]


def make_ws():
    ws = Workspace()
    ws.append_pages("A", range(10))
    ws.append_pages("B", range(5))
    return ws


def test_import_order_rule_1_and_2():
    ws = make_ws()
    assert tags(ws) == [f"A-{i}" for i in range(1, 11)] + [f"B-{i}" for i in range(1, 6)]
    ws.append_pages("C", range(3))
    assert tags(ws)[-4:] == ["B-5", "C-1", "C-2", "C-3"]


def test_relative_order_kept_after_move_swap_delete():
    ws = make_ws()
    uids = [r.uid for r in ws.refs]
    ws.move([uids[1], uids[3]], 8)            # 把 A2、A4 拖到 A9 前
    assert tags(ws)[:9] == ["A-1", "A-3", "A-5", "A-6", "A-7", "A-8", "A-2", "A-4", "A-9"]
    ws.delete([uids[0]])
    others = [t for t in tags(ws) if t not in ("A-2", "A-4")]
    assert others == ["A-3", "A-5", "A-6", "A-7", "A-8", "A-9", "A-10", "B-1", "B-2", "B-3", "B-4", "B-5"]
    ws.swap(0, len(ws) - 1)
    assert tags(ws)[0] == "B-5" and tags(ws)[-1] == "A-3"


def test_undo_restores_exact_order_rule_4():
    ws = make_ws()
    def state():
        return ws.refs      # 含 uid、来源、页号、旋转的完整快照

    history = [state()]
    uids = [r.uid for r in ws.refs]
    ops = [
        lambda: ws.move([uids[2], uids[12]], 0),
        lambda: ws.swap(3, 7),
        lambda: ws.delete([uids[5], uids[6]]),
        lambda: ws.duplicate([uids[0]]),
        lambda: ws.rotate([uids[1]], 90),
        lambda: ws.reverse(),
        lambda: ws.interleave(),
        lambda: ws.move_to_position([uids[14]], 1),
    ]
    for op in ops:
        op()
        if state() != history[-1]:      # 未改变顺序的操作不产生撤销记录
            history.append(state())
    assert len(history) >= 7
    for expected in reversed(history[:-1]):
        ws.undo()
        assert state() == expected
    assert ws.can_undo()          # 再往前是两次导入，导入本身也可撤销
    for expected in history[1:]:
        ws.redo()
        assert state() == expected


def test_undo_at_least_50_steps():
    ws = make_ws()
    first = tags(ws)
    for i in range(60):
        ws.swap(0, 1 + i % 10)
    for _ in range(60):
        ws.undo()
    assert tags(ws) == first


def test_duplicate_inserts_after_original():
    ws = make_ws()
    uids = [r.uid for r in ws.refs]
    ws.duplicate([uids[0], uids[2]])
    assert tags(ws)[:5] == ["A-1", "A-1", "A-2", "A-3", "A-3"]
    assert len({r.uid for r in ws.refs}) == len(ws)


def test_move_to_position():
    ws = make_ws()
    uids = [r.uid for r in ws.refs]
    ws.move_to_position([uids[0], uids[1]], 5)
    assert tags(ws)[4:6] == ["A-1", "A-2"]


def test_interleave_duplex():
    ws = Workspace()
    ws.append_pages("F", range(3))          # 正面 1,3,5
    ws.append_pages("R", range(3))          # 背面倒序扫描 6,4,2
    ws.interleave()
    assert tags(ws) == ["F-1", "R-3", "F-2", "R-2", "F-3", "R-1"]


def test_parse_range():
    assert parse_range("1-5, 8，12-15", 20) == [0, 1, 2, 3, 4, 7, 11, 12, 13, 14]
    assert parse_range("18-", 20) == [17, 18, 19]
    with pytest.raises(ValueError):
        parse_range("0-3", 10)
    with pytest.raises(ValueError):
        parse_range("abc", 10)
