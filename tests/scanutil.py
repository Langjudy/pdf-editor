"""测试用：把矢量 PDF 页渲染成“扫描件”（倾斜、黑边、噪点、模糊、灰底、JPEG、红章）。"""
import sys

import cv2
import numpy as np
import pymupdf as fitz


def fake_scan(page, dpi=300, angle=2.5, stamp=True, seed=0):
    rng = np.random.default_rng(seed)
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).copy()
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if stamp:
        h, w = img.shape[:2]
        c = (int(w * 0.72), int(h * 0.22))
        cv2.circle(img, c, int(w * 0.08), (40, 40, 210), 8)
        cv2.putText(img, "STAMP", (c[0] - int(w * 0.05), c[1] + 15), cv2.FONT_HERSHEY_SIMPLEX, 2.2,
                    (40, 40, 210), 6)
    img = (img.astype(np.float32) * 0.92 + 12).clip(0, 255).astype(np.uint8)   # 灰底
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    img = cv2.warpAffine(img, M, (w, h), borderValue=(60, 60, 60))             # 倾斜 + 黑边
    img = cv2.GaussianBlur(img, (3, 3), 0.8)
    noise = rng.normal(0, 6, img.shape)
    img = (img + noise).clip(0, 255).astype(np.uint8)
    for _ in range(40):                                                         # 污点
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.circle(img, (x, y), int(rng.integers(1, 4)), (30, 30, 30), -1)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpg.tobytes(), page.rect


def make_scan_pdf(src_path, out_path, pages, **kw):
    src = fitz.open(src_path)
    out = fitz.open()
    for i, pno in enumerate(pages):
        jpg, rect = fake_scan(src[pno], seed=i, **kw)
        p = out.new_page(width=rect.width, height=rect.height)
        p.insert_image(p.rect, stream=jpg)
    out.save(out_path)


if __name__ == "__main__":
    make_scan_pdf(sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3].split(",")])


def poster_page_image(seed=0):
    """模拟海报照片：蓝色渐变底 + 红色粗体大字（白色描边、粉色外发光）+ 小号深色说明文字。"""
    rng = np.random.default_rng(seed)
    W, H = 1700, 1200
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    bg = np.zeros((H, W, 3), np.float32)
    bg[..., 0] = 150 + 40 * yy / H + 15 * xx / W          # R
    bg[..., 1] = 195 + 30 * yy / H                         # G
    bg[..., 2] = 235 - 10 * yy / H                         # B
    img = bg.copy()

    def text_alpha(text, fontfile, size_px, x, y):
        d = fitz.open()
        p = d.new_page(width=W, height=H)
        f = fitz.Font(fontfile=fontfile)
        tw = fitz.TextWriter(p.rect)
        tw.append((x, y), text, font=f, fontsize=size_px)
        tw.write_text(p)
        pix = p.get_pixmap(alpha=True)
        return np.frombuffer(pix.samples, np.uint8).reshape(H, W, 4)[..., 3].astype(np.float32) / 255

    a = text_alpha("办公室消防安全提示", "C:/Windows/Fonts/simhei.ttf", 120, 180, 300)
    k = np.ones((3, 3), np.uint8)
    outline = cv2.dilate((a > 0.3).astype(np.uint8), k, iterations=9).astype(np.float32)
    glow = cv2.GaussianBlur(cv2.dilate((a > 0.3).astype(np.uint8), k, iterations=16).astype(np.float32), (0, 0), 9)
    for layer, color in ((glow * 0.55, (250, 190, 200)), (outline, (255, 255, 255)), (a, (200, 20, 30))):
        img = img * (1 - layer[..., None]) + np.array(color, np.float32) * layer[..., None]
    b = text_alpha("避免插座短路、发热风险。", "C:/Windows/Fonts/simhei.ttf", 40, 200, 500)
    img = img * (1 - b[..., None]) + np.array((20, 25, 40), np.float32) * b[..., None]
    img = cv2.GaussianBlur(img, (0, 0), 1.2) + rng.normal(0, 3, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return jpg.tobytes(), (W, H)


def poster_doc():
    jpg, (W, H) = poster_page_image()
    d = fitz.open()
    p = d.new_page(width=W * 0.48, height=H * 0.48)     # 约 150 DPI
    p.insert_image(p.rect, stream=jpg)
    return d
