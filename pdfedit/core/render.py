"""页面几何与渲染。

坐标系约定：
- 原始坐标：未旋转的源页面坐标（文字提取、搜索、show_pdf_page 的 clip 使用此坐标）。
- 显示坐标：先应用源页面自身 /Rotate，再叠加工作区额外旋转 rot，最后按 scale 缩放，
  左上角平移到 (0, 0)。界面上看到的就是显示坐标。
"""
from __future__ import annotations

from collections import OrderedDict

import pymupdf as fitz

from .engine import ENGINE_LOCK, DocumentStore


def display_matrix(page: fitz.Page, rot: int, scale: float) -> fitz.Matrix:
    """旋转后页面坐标（page.rect 空间）→ 显示坐标。"""
    m = fitz.Matrix(scale, scale).prerotate(rot % 360)
    r = page.rect * m
    return m * fitz.Matrix(1, 0, 0, 1, -r.x0, -r.y0)


def source_to_display(page: fitz.Page, rot: int, scale: float) -> fitz.Matrix:
    """原始（未旋转）坐标 → 显示坐标。"""
    return page.rotation_matrix * display_matrix(page, rot, scale)


def matrix_from_geometry(rotation_matrix: fitz.Matrix, rect: fitz.Rect, rot: int,
                         scale: float) -> fitz.Matrix:
    """与 source_to_display 等价，但只依赖缓存的几何信息（无需访问文档）。"""
    m = fitz.Matrix(scale, scale).prerotate(rot % 360)
    r = rect * m
    return rotation_matrix * m * fitz.Matrix(1, 0, 0, 1, -r.x0, -r.y0)


def display_size(page: fitz.Page, rot: int) -> tuple[float, float]:
    w, h = page.rect.width, page.rect.height
    return (h, w) if rot % 180 == 90 else (w, h)


class RenderedImage:
    __slots__ = ("samples", "width", "height", "stride", "x", "y")

    def __init__(self, pix: fitz.Pixmap):
        self.samples = pix.samples
        self.width = pix.width
        self.height = pix.height
        self.stride = pix.stride
        self.x = pix.x
        self.y = pix.y


class Renderer:
    """渲染器。大页面通过 DisplayList 缓存 + 局部裁切实现局部高清渲染。"""

    def __init__(self, store: DocumentStore, max_displaylists: int = 6):
        self.store = store
        self._dls: OrderedDict = OrderedDict()
        self._max = max_displaylists
        self._geo: dict = {}

    def geometry(self, sid: str, pno: int) -> tuple[fitz.Matrix, fitz.Rect]:
        """(页面 rotation_matrix, 旋转后页面矩形)，缓存。"""
        key = (sid, pno)
        g = self._geo.get(key)
        if g is None:
            with ENGINE_LOCK:
                p = self.store.page(sid, pno)
                g = (fitz.Matrix(p.rotation_matrix), fitz.Rect(p.rect))
            self._geo[key] = g
        return g

    def page_size(self, sid: str, pno: int, rot: int = 0) -> tuple[float, float]:
        r = self.geometry(sid, pno)[1]
        w, h = r.width, r.height
        return (h, w) if rot % 180 == 90 else (w, h)

    def to_display(self, sid: str, pno: int, rot: int, scale: float) -> fitz.Matrix:
        rm, rect = self.geometry(sid, pno)
        return matrix_from_geometry(rm, rect, rot, scale)

    def invalidate(self, sid: str, pno: int | None = None):
        for key in list(self._dls):
            if key[0] == sid and (pno is None or key[1] == pno):
                del self._dls[key]
        for key in list(self._geo):
            if key[0] == sid and (pno is None or key[1] == pno):
                del self._geo[key]

    def _displaylist(self, sid: str, pno: int):
        key = (sid, pno)
        dl = self._dls.get(key)
        if dl is None:
            dl = self.store.page(sid, pno).get_displaylist()
            self._dls[key] = dl
            while len(self._dls) > self._max:
                self._dls.popitem(last=False)
        else:
            self._dls.move_to_end(key)
        return dl

    def render_full(self, sid: str, pno: int, rot: int, scale: float) -> RenderedImage:
        """整页渲染（缩略图、预览用），不缓存 DisplayList。"""
        with ENGINE_LOCK:
            page = self.store.page(sid, pno)
            pix = page.get_pixmap(matrix=display_matrix(page, rot, scale), alpha=False)
            return RenderedImage(pix)

    def render_tile(self, sid: str, pno: int, rot: int, scale: float,
                    tile: tuple[int, int, int, int]) -> RenderedImage:
        """渲染显示坐标下的一个矩形块（设备像素）。"""
        with ENGINE_LOCK:
            page = self.store.page(sid, pno)
            m = display_matrix(page, rot, scale)
            clip = fitz.Rect(*tile) * ~m
            dl = self._displaylist(sid, pno)
            pix = dl.get_pixmap(matrix=m, clip=clip, alpha=False)
            return RenderedImage(pix)
