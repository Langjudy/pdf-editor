"""后台渲染 / 识别线程。

单个工作线程按优先级处理任务：主视图图块 > 主视图预览 > 缩略图 > 页面类型识别。
主视图图块任务每次重绘时整体替换，过期的请求直接丢弃，保证缩放平移流畅。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..core import pagetype
from ..core.engine import ENGINE_LOCK, DocumentStore
from ..core.render import RenderedImage, Renderer


def to_qimage(img: RenderedImage) -> QImage:
    return QImage(img.samples, img.width, img.height, img.stride, QImage.Format_RGB888).copy()


class RenderService(QObject):
    tileReady = Signal(object, QImage, int, int)     # key, image, x, y（设备像素）
    imageReady = Signal(object, QImage)              # key, image（预览/缩略图）
    typeReady = Signal(str, int, str)                # sid, pno, type
    classifyProgress = Signal(int)                   # 剩余待识别页数

    def __init__(self, store: DocumentStore):
        super().__init__()
        self.store = store
        self.renderer = Renderer(store)
        self._cv = threading.Condition()
        self._tiles: list = []                 # [(key, sid, pno, rot, scale, rect)]
        self._images: OrderedDict = OrderedDict()   # key -> (sid, pno, rot, scale, priority)
        self._classify: deque = deque()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="render", daemon=True)
        self._thread.start()

    # ---------- 请求 ----------
    def set_tile_jobs(self, jobs: list):
        with self._cv:
            self._tiles = list(jobs)
            self._cv.notify()

    def request_image(self, key, sid: str, pno: int, rot: int, scale: float, priority: int = 1):
        """priority：0=预览（高），1=缩略图。同 key 重复请求会被合并。"""
        with self._cv:
            if key in self._images:
                self._images.move_to_end(key)
                return
            self._images[key] = (sid, pno, rot, scale, priority)
            self._cv.notify()

    def cancel_images(self, keep_keys: set):
        """丢弃不再可见的缩略图请求。"""
        with self._cv:
            for k in list(self._images):
                if k not in keep_keys and self._images[k][4] == 1:
                    del self._images[k]

    def classify(self, sid: str, pnos):
        with self._cv:
            self._classify.extend((sid, p) for p in pnos)
            self._cv.notify()

    def invalidate_source(self, sid: str):
        with ENGINE_LOCK:
            self.renderer.invalidate(sid)

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=2)

    # ---------- 工作线程 ----------
    def _next(self):
        with self._cv:
            while True:
                if self._stop:
                    return None
                if self._tiles:
                    return ("tile", self._tiles.pop(0))
                if self._images:
                    # 先取高优先级（预览），同级取最近请求的（LIFO，可见区域优先）
                    best = None
                    for k, v in reversed(self._images.items()):
                        if best is None or v[4] < self._images[best][4]:
                            best = k
                            if v[4] == 0:
                                break
                    return ("image", (best, self._images.pop(best)))
                if self._classify:
                    job = self._classify.popleft()
                    return ("classify", (job, len(self._classify)))
                self._cv.wait()

    def _run(self):
        while True:
            job = self._next()
            if job is None:
                return
            kind, data = job
            try:
                if kind == "tile":
                    key, sid, pno, rot, scale, rect = data
                    if sid not in self.store.sources:
                        continue
                    img = self.renderer.render_tile(sid, pno, rot, scale, rect)
                    self.tileReady.emit(key, to_qimage(img), img.x, img.y)
                elif kind == "image":
                    key, (sid, pno, rot, scale, _) = data
                    if sid not in self.store.sources:
                        continue
                    img = self.renderer.render_full(sid, pno, rot, scale)
                    self.imageReady.emit(key, to_qimage(img))
                else:
                    (sid, pno), remaining = data
                    src = self.store.sources.get(sid)
                    if src is None or pno >= src.page_count:
                        continue
                    if src.page_types[pno] is None:
                        with ENGINE_LOCK:
                            t = pagetype.classify(src.doc[pno])
                        src.page_types[pno] = t
                        self.typeReady.emit(sid, pno, t)
                    self.classifyProgress.emit(remaining)
                    time.sleep(0.002)   # 主动让出 GIL，保证界面线程及时处理渲染结果
            except Exception as e:  # noqa: BLE001  单页失败不影响其他页面
                print(f"[render] {kind} 失败：{e}")
