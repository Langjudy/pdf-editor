"""安装包自检：PdfEditor.exe --selftest <结果文件>

在打包后的程序中实际跑一遍关键链路（不显示窗口），用于确认打包没有遗漏模块、模型或插件：
导入 PDF → 渲染 → 主窗口 → 字体扫描 → 文字编辑 → OCR 模型识别 → 批注 → 导出。
结果写入指定文件，全部通过时最后一行为 OK。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback


def run(out_path: str) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines = []

    def log(msg):
        lines.append(msg)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    t0 = time.time()
    try:
        import pymupdf as fitz
        from PySide6.QtWidgets import QApplication

        from . import __version__
        log(f"版本 {__version__}，Python {sys.version.split()[0]}，frozen={getattr(sys, 'frozen', False)}")
        tmp = tempfile.mkdtemp(prefix="pdfedit-selftest-")
        src = os.path.join(tmp, "selftest.pdf")
        d = fitz.open()
        p = d.new_page()
        p.insert_text((72, 100), "Selftest PDF 自检文字 12345", fontsize=24, fontname="china-s")
        d.save(src)
        app = QApplication.instance() or QApplication([])

        from .app import project
        project.app_data_dir = lambda: tmp           # 不影响真实的自动保存
        from .ui.main_window import MainWindow
        w = MainWindow()
        w.import_files([src])
        app.processEvents()
        log(f"导入与主窗口：{len(w.ws)} 页")

        img = w.service.renderer.render_full(w.ws.refs[0].src, w.ws.refs[0].pno, 0, 2.0)
        log(f"渲染：{img.width}x{img.height}")

        w._ensure_fonts()
        log(f"系统字体：{len(w.fonts.entries)} 个，默认中文字体 {w.fonts.default_cjk}")

        ref = w.ws.refs[0]
        b = w.texts.blocks(ref)[0]
        w.texts.edit(ref.uid, b.id, "自检修改后的文字", b.style())
        log("文字编辑：" + ("通过" if "自检修改后的文字" in w.store.page(w.ws.refs[0].src, w.ws.refs[0].pno).get_text()
                         else "失败"))

        from .core import ocr
        page = w.store.page(w.ws.refs[0].src, w.ws.refs[0].pno)
        lines_ocr = ocr.ocr_page(page, ocr.OcrEngine())
        text = "".join(x.text for x in lines_ocr)
        log(f"OCR：{text!r}")
        if "自检" not in text:
            raise RuntimeError("OCR 未识别出预期文字")

        # 扫描图像切块存放的页面（1.0.1 修复：曾报“该页没有可编辑的扫描底图”）
        import cv2
        import numpy as np

        from .core import pagetype
        pix = d[0].get_pixmap(dpi=200)
        arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        sd = fitz.open()
        sp = sd.new_page(width=d[0].rect.width, height=d[0].rect.height)
        H = arr.shape[0]
        for k in range(4):
            y0, y1 = H * k // 4, H * (k + 1) // 4
            ok, part = cv2.imencode(".jpg", cv2.cvtColor(np.ascontiguousarray(arr[y0:y1]), cv2.COLOR_RGB2BGR))
            sp.insert_image(fitz.Rect(0, sp.rect.height * y0 / H, sp.rect.width, sp.rect.height * y1 / H),
                            stream=part.tobytes())
        tiled = os.path.join(tmp, "tiled.pdf")
        sd.save(tiled)
        w.import_files([tiled])
        tref = w.ws.refs[-1]
        src = w.store.get(tref.src)
        src.page_types[0] = pagetype.classify(src.doc[0])
        w.scans.commit_ocr(tref.src, 0, w.scans.ocr_key(tref.src, 0))
        tref = w.ws.refs[-1]
        tblocks = w.texts.blocks(tref)
        tb = next((b for b in tblocks if "自检" in b.text), None)
        if tb is None:
            raise RuntimeError(f"切块扫描页 OCR 结果中没有找到“自检”：{[b.text for b in tblocks]}")
        from .core.scanedit import ScanEditOptions
        w.texts.edit(tref.uid, tb.id, "Tiled OK", None, scan_opts=ScanEditOptions(mode="vector"))
        tt = w.store.page(w.ws.refs[-1].src, w.ws.refs[-1].pno).get_text()
        log(f"切块扫描页（{src.page_types[0]}）修改：" + ("通过" if "Tiled OK" in tt else "失败"))
        if "Tiled OK" not in tt:
            raise RuntimeError("切块扫描页修改失败")
        w.ws.delete([w.ws.refs[-1].uid])

        from .core import annots as A
        w.annots.add_shape(ref.uid, "rect", [(60, 60), (300, 120)], A.Style())
        w.annots.add_stamp(ref.uid, (300, 300, 440, 360), A.make_stamp_png("已审核"), A.Style(), "已审核")
        log(f"批注：{[a.label for a in w.annots.annots(w.ws.refs[0])]}")

        from .app.exporter import ExportJob, run_export
        out = os.path.join(tmp, "out.pdf")
        run_export(w.store, ExportJob("all_one", list(w.ws.refs), [1], out))
        with fitz.open(out) as od:
            log(f"导出：{od.page_count} 页，{len(list(od[0].annots()))} 个批注")
        w.ws.dirty = False
        w.close()
        log(f"用时 {time.time() - t0:.1f} 秒")
        log("OK")
        return 0
    except Exception:  # noqa: BLE001
        log("失败：\n" + traceback.format_exc())
        return 1
