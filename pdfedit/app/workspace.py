"""工作区：所有导入页面组成的有序列表。

所有页面操作只修改工作区列表，不改动原文件。撤销/重做采用整表快照：
列表元素是不可变的 PageRef，一个快照只是一个元组，1000 页 × 100 步也很小，
并且能保证“撤销后页面顺序精确恢复”（技术要求 5.4）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable

MAX_UNDO = 100


@dataclass(frozen=True)
class PageRef:
    uid: int          # 工作区内唯一标识（复制出的副本有新的 uid）
    src: str          # 来源文档 id
    pno: int          # 来源文档中的页号（从 0 开始）
    rot: int = 0      # 工作区额外旋转（0/90/180/270）
    kind: str = "page"  # page | region | blank


class Workspace:
    def __init__(self):
        self._refs: tuple[PageRef, ...] = ()
        self._undo: list[tuple[tuple[PageRef, ...], str]] = []
        self._redo: list[tuple[tuple[PageRef, ...], str]] = []
        self._uid = 0
        self.listeners: list[Callable[[], None]] = []
        self.dirty = False

    # ---------- 基础 ----------
    @property
    def refs(self) -> tuple[PageRef, ...]:
        return self._refs

    def __len__(self):
        return len(self._refs)

    def new_ref(self, src: str, pno: int, rot: int = 0, kind: str = "page") -> PageRef:
        self._uid += 1
        return PageRef(self._uid, src, pno, rot % 360, kind)

    def clone(self, ref: PageRef) -> PageRef:
        return self.new_ref(ref.src, ref.pno, ref.rot, ref.kind)

    def index_of(self, uid: int) -> int:
        for i, r in enumerate(self._refs):
            if r.uid == uid:
                return i
        return -1

    def by_uid(self, uid: int) -> PageRef | None:
        i = self.index_of(uid)
        return self._refs[i] if i >= 0 else None

    def _commit(self, new_refs: Iterable[PageRef], label: str):
        new_refs = tuple(new_refs)
        if new_refs == self._refs:
            return
        self._undo.append((self._refs, label))
        if len(self._undo) > MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()
        self._refs = new_refs
        self.dirty = True
        self._notify()

    def _notify(self):
        for cb in list(self.listeners):
            cb()

    def reset(self, refs: Iterable[PageRef] = ()):
        """加载项目时使用：替换全部内容并清空撤销历史。"""
        self._refs = tuple(refs)
        self._undo.clear()
        self._redo.clear()
        self._uid = max([r.uid for r in self._refs], default=0)
        self.dirty = False
        self._notify()

    # ---------- 撤销 / 重做 ----------
    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][1] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][1] if self._redo else ""

    def undo(self):
        if not self._undo:
            return
        refs, label = self._undo.pop()
        self._redo.append((self._refs, label))
        self._refs = refs
        self.dirty = True
        self._notify()

    def redo(self):
        if not self._redo:
            return
        refs, label = self._redo.pop()
        self._undo.append((self._refs, label))
        self._refs = refs
        self.dirty = True
        self._notify()

    # ---------- 页面操作 ----------
    def insert(self, refs: list[PageRef], at: int | None = None, label: str = "插入页面"):
        cur = list(self._refs)
        at = len(cur) if at is None else max(0, min(at, len(cur)))
        cur[at:at] = refs
        self._commit(cur, label)

    def append_pages(self, src: str, pnos: Iterable[int], at: int | None = None,
                     kind: str = "page", label: str = "导入") -> list[PageRef]:
        refs = [self.new_ref(src, p, 0, kind) for p in pnos]
        self.insert(refs, at, label)
        return refs

    def upgrade_page(self, src: str, pno: int, new_src: str, new_pno: int) -> int:
        """把所有指向 (src, pno) 的页面换成新版本，同时更新撤销 / 重做历史，不产生撤销记录。

        用于 OCR 文字层这类不改变页面外观的“升级”：撤销其他操作时不会丢失识别结果。
        返回当前工作区中被替换的页面数。
        """
        def up(refs):
            return tuple(replace(r, src=new_src, pno=new_pno) if (r.src, r.pno) == (src, pno) else r
                         for r in refs)
        before = self._refs
        self._refs = up(self._refs)
        self._undo = [(up(r), lbl) for r, lbl in self._undo]
        self._redo = [(up(r), lbl) for r, lbl in self._redo]
        n = sum(1 for a, b in zip(before, self._refs) if a is not b)
        if n:
            self.dirty = True
            self._notify()
        return n

    def delete(self, uids: Iterable[int]):
        s = set(uids)
        self._commit([r for r in self._refs if r.uid not in s], "删除页面")

    def move(self, uids: Iterable[int], to_index: int):
        """把选中页（保持其相对顺序）移动到当前列表的插入位置 to_index 处。"""
        s = set(uids)
        moving = [r for r in self._refs if r.uid in s]
        if not moving:
            return
        to_index = max(0, min(to_index, len(self._refs)))
        before = sum(1 for r in self._refs[:to_index] if r.uid not in s)
        rest = [r for r in self._refs if r.uid not in s]
        rest[before:before] = moving
        self._commit(rest, "移动页面")

    def move_to_position(self, uids: Iterable[int], position: int):
        """移动到指定页码（从 1 开始）：移动后第一张选中页位于该页码。"""
        s = set(uids)
        moving = [r for r in self._refs if r.uid in s]
        rest = [r for r in self._refs if r.uid not in s]
        at = max(0, min(position - 1, len(rest)))
        rest[at:at] = moving
        self._commit(rest, "移动到指定页码")

    def swap(self, i: int, j: int):
        cur = list(self._refs)
        cur[i], cur[j] = cur[j], cur[i]
        self._commit(cur, "交换页面")

    def replace_at(self, i: int, new: PageRef, label: str = "替换页面"):
        cur = list(self._refs)
        cur[i] = new
        self._commit(cur, label)

    def replace_many(self, changes: dict[int, PageRef], label: str):
        """一次替换多个位置的页面（合并为一步撤销）。"""
        cur = list(self._refs)
        for i, new in changes.items():
            cur[i] = new
        self._commit(cur, label)

    def duplicate(self, uids: Iterable[int]) -> list[PageRef]:
        """复制选中页，副本紧跟在各自原页面之后。"""
        s = set(uids)
        cur, copies = [], []
        for r in self._refs:
            cur.append(r)
            if r.uid in s:
                c = self.clone(r)
                cur.append(c)
                copies.append(c)
        self._commit(cur, "复制页面")
        return copies

    def paste(self, templates: list[PageRef], at: int | None = None) -> list[PageRef]:
        refs = [self.clone(t) for t in templates]
        self.insert(refs, at, "粘贴页面")
        return refs

    def rotate(self, uids: Iterable[int], angle: int):
        s = set(uids)
        self._commit([replace(r, rot=(r.rot + angle) % 360) if r.uid in s else r
                      for r in self._refs], "旋转页面")

    def reverse(self, uids: Iterable[int] | None = None):
        """倒序排列（选中页在原占位上倒序；未指定则全部倒序）。"""
        cur = list(self._refs)
        idx = list(range(len(cur))) if uids is None else \
            [i for i, r in enumerate(cur) if r.uid in set(uids)]
        vals = [cur[i] for i in idx][::-1]
        for i, v in zip(idx, vals):
            cur[i] = v
        self._commit(cur, "倒序排列")

    def interleave(self, uids: Iterable[int] | None = None, reverse_back: bool = True):
        """双面扫描整理：前半为正面、后半为背面（默认背面为倒序），交错合并。"""
        cur = list(self._refs)
        idx = list(range(len(cur))) if uids is None else \
            [i for i, r in enumerate(cur) if r.uid in set(uids)]
        if len(idx) < 2:
            return
        vals = [cur[i] for i in idx]
        half = (len(vals) + 1) // 2
        front, back = vals[:half], vals[half:]
        if reverse_back:
            back = back[::-1]
        merged = []
        for k in range(half):
            merged.append(front[k])
            if k < len(back):
                merged.append(back[k])
        for i, v in zip(idx, merged):
            cur[i] = v
        self._commit(cur, "奇偶页重排")


# ---------------------------------------------------------------- 页码范围

def parse_range(text: str, count: int) -> list[int]:
    """解析页码范围（如 "1-5, 8, 12-15"），返回从 0 开始的索引列表（去重、保持顺序）。"""
    text = text.replace("，", ",").replace("、", ",").replace("～", "-").replace("~", "-") \
        .replace("—", "-").replace("–", "-").replace("－", "-")
    out: list[int] = []
    seen = set()
    for part in filter(None, (p.strip() for p in re.split(r"[,;；\s]+", text))):
        m = re.fullmatch(r"(\d+)\s*(?:-\s*(\d*))?", part)
        if not m:
            raise ValueError(f"无法识别：{part}")
        a = int(m.group(1))
        if m.group(2) is None:
            b = a
        elif m.group(2) == "":
            b = count            # "5-" 表示到最后一页
        else:
            b = int(m.group(2))
        if a < 1 or b < 1 or a > count or b > count:
            raise ValueError(f"页码超出范围（共 {count} 页）：{part}")
        step = 1 if b >= a else -1
        for p in range(a, b + step, step):
            if p not in seen:
                seen.add(p)
                out.append(p - 1)
    if not out:
        raise ValueError("未输入页码")
    return out
