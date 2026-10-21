"""后台 OCR 队列（F-SCN-12）：批量识别在独立线程运行，显示进度、可取消，不阻塞界面。

识别推理不持有引擎锁；只有渲染页面和写入文字层的短暂时间持锁，因此识别期间仍可正常浏览和编辑。
"""
from __future__ import annotations

import threading
from collections import deque

from PySide6.QtCore import QObject, Signal

from ..app.scanservice import ScanService


class OcrWorker(QObject):
    pageDone = Signal(str, int, int)        # src, pno, 新版本页号
    pageFailed = Signal(str, int, str)
    progress = Signal(int, int)             # 已完成, 总数
    idle = Signal()

    def __init__(self, scans: ScanService):
        super().__init__()
        self.scans = scans
        self._q: deque = deque()
        self._queued: set = set()
        self._cv = threading.Condition()
        self._gen = 0
        self._stop = False
        self.done = 0
        self.total = 0
        self._busy = False
        self._thread = threading.Thread(target=self._run, name="ocr", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy or bool(self._q)

    def enqueue(self, keys) -> int:
        n = 0
        with self._cv:
            if not self._q and not self._busy:
                self.done = self.total = 0
            for k in keys:
                if k not in self._queued:
                    self._q.append(k)
                    self._queued.add(k)
                    n += 1
            self.total += n
            self._cv.notify()
        self.progress.emit(self.done, self.total)
        return n

    def cancel(self):
        with self._cv:
            self._gen += 1
            self._q.clear()
            self._queued.clear()
            self.total = self.done
        self.progress.emit(self.done, self.total)

    def stop(self):
        with self._cv:
            self._stop = True
            self._gen += 1
            self._q.clear()
            self._cv.notify()
        self._thread.join(timeout=3)

    def _run(self):
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                key = self._q.popleft()
                gen = self._gen
                self._busy = True
            src, pno = key
            try:
                new = self.scans.ocr_key(src, pno, cancelled=lambda: self._gen != gen)
                if new is not None and self._gen == gen:
                    self.pageDone.emit(src, pno, new)
            except Exception as e:  # noqa: BLE001  单页失败不影响其他页面
                self.pageFailed.emit(src, pno, f"{type(e).__name__}: {e}")
            with self._cv:
                self._queued.discard(key)
                if self._gen == gen:
                    self.done += 1
                self._busy = False
                empty = not self._q
            self.progress.emit(self.done, self.total)
            if empty:
                self.idle.emit()
