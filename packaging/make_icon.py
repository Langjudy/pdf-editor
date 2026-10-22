"""生成程序图标：pdfedit/resources/app.png（窗口图标）与 packaging/app.ico（exe / 安装包图标）。

用法：venv\\python.exe packaging\\make_icon.py
"""
import io
import os

import pymupdf as fitz
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def draw(size: int = 256) -> bytes:
    d = fitz.open()
    p = d.new_page(width=256, height=256)
    red, dark = (0.85, 0.15, 0.15), (0.55, 0.08, 0.08)
    # 文档纸张（右上角折角）
    paper = [(48, 16), (168, 16), (212, 60), (212, 240), (48, 240)]
    p.draw_polyline(paper + [paper[0]], color=(0.75, 0.75, 0.78), fill=(1, 1, 1), width=6)
    p.draw_polyline([(168, 16), (168, 60), (212, 60)], color=(0.75, 0.75, 0.78), fill=(0.9, 0.9, 0.92), width=6)
    # 红色标签条与 “PDF” 字样
    p.draw_rect(fitz.Rect(20, 120, 200, 196), color=dark, fill=red, width=0, radius=0.18)
    tw = fitz.TextWriter(p.rect, color=(1, 1, 1))
    font = fitz.Font("helv")
    tw.append((40, 178), "PDF", font=font, fontsize=60)
    tw.write_text(p)
    # 编辑笔
    p.draw_line((150, 100), (226, 24), color=(0.2, 0.45, 0.85), width=14)
    p.draw_line((150, 100), (140, 110), color=(0.15, 0.15, 0.15), width=10)
    pix = p.get_pixmap(matrix=fitz.Matrix(size / 256, size / 256), alpha=True)
    return pix.tobytes("png")


def main():
    png = draw(256)
    res = os.path.join(ROOT, "pdfedit", "resources")
    os.makedirs(res, exist_ok=True)
    with open(os.path.join(res, "app.png"), "wb") as f:
        f.write(png)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    img.save(os.path.join(ROOT, "packaging", "app.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成 pdfedit/resources/app.png 与 packaging/app.ico")


if __name__ == "__main__":
    main()
