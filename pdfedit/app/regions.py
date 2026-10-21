"""区域选区管理（技术要求 6.4）。

选区挂在工作区页面（uid）上，顶点保存为源页面的原始（未旋转）坐标，
因此页面旋转、缩放都不影响选区位置。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Region:
    id: int
    name: str
    points: list = field(default_factory=list)   # [(x, y), ...] 原始坐标
    is_rect: bool = True

    def bbox(self):
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def rect_points(x0, y0, x1, y1):
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class RegionStore:
    def __init__(self):
        self.by_uid: dict[int, list[Region]] = {}
        self._id = 0
        self.listeners: list[Callable[[int], None]] = []

    def _notify(self, uid: int):
        for cb in list(self.listeners):
            cb(uid)

    def get(self, uid: int) -> list[Region]:
        return self.by_uid.get(uid, [])

    def find(self, uid: int, rid: int) -> Region | None:
        for r in self.get(uid):
            if r.id == rid:
                return r
        return None

    def add(self, uid: int, points, is_rect: bool, name: str | None = None) -> Region:
        self._id += 1
        lst = self.by_uid.setdefault(uid, [])
        reg = Region(self._id, name or f"选区{len(lst) + 1}", [tuple(p) for p in points], is_rect)
        lst.append(reg)
        self._notify(uid)
        return reg

    def set_points(self, uid: int, rid: int, points):
        r = self.find(uid, rid)
        if r:
            r.points = [tuple(p) for p in points]
            self._notify(uid)

    def rename(self, uid: int, rid: int, name: str):
        r = self.find(uid, rid)
        if r and name.strip():
            r.name = name.strip()
            self._notify(uid)

    def remove(self, uid: int, rid: int):
        lst = self.by_uid.get(uid, [])
        self.by_uid[uid] = [r for r in lst if r.id != rid]
        self._notify(uid)

    def clear(self, uid: int):
        if self.by_uid.pop(uid, None) is not None:
            self._notify(uid)

    def apply_template(self, from_uid: int, to_uids, region_ids=None) -> int:
        """把一页上的选区原样复制到其他页面（版式相同的图纸）。返回复制数量。"""
        src = [r for r in self.get(from_uid) if region_ids is None or r.id in region_ids]
        n = 0
        for uid in to_uids:
            if uid == from_uid:
                continue
            for r in src:
                self.add(uid, copy.deepcopy(r.points), r.is_rect, r.name)
                n += 1
        return n

    # 项目文件序列化
    def to_json(self) -> dict:
        return {str(uid): [{"name": r.name, "points": r.points, "rect": r.is_rect} for r in lst]
                for uid, lst in self.by_uid.items() if lst}

    def load_json(self, data: dict):
        self.by_uid.clear()
        for uid, lst in (data or {}).items():
            for r in lst:
                self.add(int(uid), r["points"], r.get("rect", True), r.get("name"))
