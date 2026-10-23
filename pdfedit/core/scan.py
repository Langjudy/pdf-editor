"""扫描页图像处理（技术要求 8.1、8.3）。全部 OpenCV，纯 CPU。

- 页面主图的读取 / 写回，以及图片像素与页面坐标的换算；
- 预处理：纠偏、去黑边、去噪点污渍、对比度增强、二值化；
- 擦除：纯色背景直接填背景色，有表格线 / 底纹时用图像修复补全并保留表格线；
- 把文字“渲染进图片”，可模拟扫描质感。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf as fitz

from .engine import ENGINE_LOCK

MIN_COVER = 0.6          # 主图至少覆盖页面面积的比例


# ================================================================ 页面主图
@dataclass
class PageImage:
    xref: int
    arr: np.ndarray            # RGB (H,W,3) 或灰度 (H,W) uint8
    matrix: fitz.Matrix        # 像素坐标 → 页面原始（未旋转）坐标
    ext: str                   # 原图格式

    @property
    def gray(self) -> np.ndarray:
        return self.arr if self.arr.ndim == 2 else cv2.cvtColor(self.arr, cv2.COLOR_RGB2GRAY)

    @property
    def dpi(self) -> float:
        # matrix 把 1 像素映射为多少 pt
        return 72.0 / max(float(np.hypot(self.matrix.a, self.matrix.b)), 1e-9)

    def page_to_px(self, rect: fitz.Rect, pad: float = 0) -> tuple[int, int, int, int]:
        r = (fitz.Rect(rect) * ~self.matrix)
        r.normalize()
        h, w = self.arr.shape[:2]
        x0 = max(0, int(r.x0 - pad))
        y0 = max(0, int(r.y0 - pad))
        x1 = min(w, int(np.ceil(r.x1 + pad)))
        y1 = min(h, int(np.ceil(r.y1 + pad)))
        return x0, y0, x1, y1

    def px_to_page(self, x: float, y: float) -> fitz.Point:
        return fitz.Point(x, y) * self.matrix


def load_page_image(page: fitz.Page) -> PageImage | None:
    """取页面上覆盖面积最大的图片（扫描底图）。没有足够大的图片时返回 None。"""
    with ENGINE_LOCK:
        doc = page.parent
        prot = page.rotation
        if prot:
            page.set_rotation(0)
        try:
            area = abs(page.rect)
            best = None
            for info in page.get_images(full=True):
                xref = info[0]
                for rect, m in page.get_image_rects(xref, transform=True):
                    a = abs(rect & page.rect)
                    if best is None or a > best[0]:
                        best = (a, xref, m)
        finally:
            if prot:
                page.set_rotation(prot)
        if best is None or best[0] < area * MIN_COVER:
            return None
        _, xref, m = best
        pix = fitz.Pixmap(doc, xref)
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.n - pix.alpha not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n).copy()
        if pix.n == 1:
            arr = arr[:, :, 0]
        ext = doc.extract_image(xref).get("ext", "png")
        # transform 把单位正方形映射到页面；先把像素缩放到单位正方形
        mat = fitz.Matrix(1.0 / pix.width, 0, 0, 1.0 / pix.height, 0, 0) * m
        return PageImage(xref, arr, mat, ext)


def render_region(page: fitz.Page, rect: fitz.Rect, dpi: float = 300) -> PageImage:
    """把页面的一块区域渲染成图像（调用方需已把页面旋转临时设为 0）。

    用于没有整页底图的页面（扫描图像被切块存放、CAD 曲线文字等）：在渲染图上估算样式、擦除，
    再以图片补丁的形式贴回。xref 为 -1 表示不是文档中的图片。
    """
    r = fitz.Rect(rect) & page.rect
    s = dpi / 72.0
    with ENGINE_LOCK:
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s), clip=r, alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n).copy()
    if pix.n == 1:
        arr = arr[:, :, 0]
    mat = fitz.Matrix(1 / s, 0, 0, 1 / s, pix.x / s, pix.y / s)    # 像素 → 页面坐标
    return PageImage(-1, arr, mat, "png")


def encode_image(arr: np.ndarray, prefer_jpeg: bool) -> bytes:
    """二值 / 灰度图存 PNG（无损且小），彩色扫描存高质量 JPEG。"""
    is_bilevel = arr.ndim == 2 and np.isin(arr, (0, 255)).all()
    if prefer_jpeg and not is_bilevel:
        bgr = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        bgr = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise RuntimeError("图像编码失败")
    return buf.tobytes()


def save_page_image(page: fitz.Page, pi: PageImage, arr: np.ndarray):
    with ENGINE_LOCK:
        page.replace_image(pi.xref, stream=encode_image(arr, pi.ext in ("jpeg", "jpg", "jpx")))
    pi.arr = arr


# ================================================================ 预处理
@dataclass
class PreOptions:
    deskew: bool = True
    border: bool = True
    despeckle: bool = True
    enhance: bool = False
    binarize: bool = False


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    """文字 / 墨迹掩码（白底黑字），自适应阈值，对光照不均更稳。"""
    blk = max(15, (min(gray.shape) // 40) | 1)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, blk, 15)


def local_ink(gray: np.ndarray) -> np.ndarray:
    """小范围（一行文字）的墨迹掩码：Otsu 全局阈值，粗笔画内部也能完整识别。

    自适应阈值的窗口在小裁剪区里太小，会把粗笔画识别成空心轮廓。
    """
    t, m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if float(np.median(gray)) - t < 25:      # 几乎没有墨迹：阈值落在噪声里
        m[:] = 0
    return m


def line_strokes(gray: np.ndarray, inner: tuple, dpi: float) -> tuple[np.ndarray, np.ndarray]:
    """在裁剪区内找出属于某一行的笔画。

    inner：行框（裁剪区内坐标）。去掉表格线后按连通块选取，笔画有 30% 以上落在行框内就算这一行的。
    返回 (笔画掩码, 表格线掩码)。
    """
    ink = local_ink(gray)
    ix0, iy0, ix1, iy1 = inner
    # 表格线远长于单个字的笔画：最短长度取 1.6 倍行高（大字的横画不会被误当成表格线）
    lines = line_masks(ink, max(int(dpi / 300 * 60), int((iy1 - iy0) * 1.6)), max(5, int(dpi / 300 * 8)))
    strokes = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.dilate(lines, np.ones((3, 3), np.uint8))))
    box = np.zeros_like(ink)
    box[iy0:iy1, ix0:ix1] = 255
    n, labels, stats, _ = cv2.connectedComponentsWithStats(strokes, 8)
    inside = np.bincount(labels[box > 0].ravel(), minlength=n)
    keep = inside >= 0.3 * np.maximum(stats[:, cv2.CC_STAT_AREA], 1)
    keep[0] = False
    return (keep[labels] * 255).astype(np.uint8), lines


def estimate_skew(gray: np.ndarray, max_angle: float = 15.0) -> float:
    """投影法估计倾斜角（度，逆时针为正，与 cv2 旋转方向一致）。"""
    h, w = gray.shape
    k = 1200.0 / max(h, w)
    small = cv2.resize(gray, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else gray
    mask = _ink_mask(small)
    # 去掉贴边的大块黑边，避免干扰
    mask[:, :4] = mask[:, -4:] = 0
    mask[:4, :] = mask[-4:, :] = 0
    sh, sw = mask.shape
    center = (sw / 2, sh / 2)

    def score(a):
        m = cv2.getRotationMatrix2D(center, a, 1.0)
        rot = cv2.warpAffine(mask, m, (sw, sh), flags=cv2.INTER_NEAREST)
        prof = rot.sum(axis=1, dtype=np.float64)
        return float(np.var(prof))

    best = max(np.arange(-max_angle, max_angle + 0.01, 1.0), key=score)
    for step in (0.25, 0.05):
        cands = np.arange(best - 4 * step, best + 4 * step + 1e-9, step)
        best = max(cands, key=score)
    return float(round(best, 2))


def background_color(arr: np.ndarray) -> np.ndarray:
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    bright = gray > np.percentile(gray, 60)
    return np.median(arr[bright], axis=0).astype(np.uint8) if bright.any() else np.array(255, np.uint8)


def rotate(arr: np.ndarray, angle: float, bg) -> np.ndarray:
    h, w = arr.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    border = tuple(int(v) for v in np.atleast_1d(bg)) if arr.ndim == 3 else int(np.atleast_1d(bg)[0])
    return cv2.warpAffine(arr, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=border)


def remove_borders(arr: np.ndarray, bg) -> np.ndarray:
    """去黑边：与图像边缘相连的大块暗色区域填成背景色。"""
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    dark = (gray < 110).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
    h, w = gray.shape
    out = arr.copy()
    min_area = h * w * 0.002
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        touches = x == 0 or y == 0 or x + bw >= w or y + bh >= h
        if touches and area > min_area:
            out[labels == i] = bg
    return out


def despeckle(arr: np.ndarray, bg, max_area: int | None = None) -> np.ndarray:
    """去噪点、污渍：去掉孤立的小墨点（远小于一个笔画的连通块）。"""
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    mask = _ink_mask(gray)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if max_area is None:
        max_area = max(4, int((arr.shape[1] / 2480.0) ** 2 * 30))   # 300dpi A4 约 30 像素
    small = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        # 只去掉既小又孤立的点（周围没有其他墨迹），避免误删标点、i 上的点
        if area <= max_area and max(bw, bh) <= 2.5 * np.sqrt(max_area):
            pad = int(max(bw, bh) * 3) + 2
            y0, y1 = max(0, y - pad), min(mask.shape[0], y + bh + pad)
            x0, x1 = max(0, x - pad), min(mask.shape[1], x + bw + pad)
            if (mask[y0:y1, x0:x1] > 0).sum() <= area * 1.5:
                small[i] = True
    out = arr.copy()
    out[small[labels]] = bg
    return cv2.medianBlur(out, 3) if out.ndim == 2 else out


def enhance(arr: np.ndarray) -> np.ndarray:
    """对比度增强（CLAHE，作用于亮度）。"""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    if arr.ndim == 2:
        return clahe.apply(arr)
    lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def binarize(arr: np.ndarray) -> np.ndarray:
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return 255 - _ink_mask(gray)


def preprocess(arr: np.ndarray, opts: PreOptions) -> tuple[np.ndarray, dict]:
    info = {"angle": 0.0}
    bg = background_color(arr)
    out = arr
    if opts.border:
        out = remove_borders(out, bg)
    if opts.deskew:
        gray = out if out.ndim == 2 else cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        a = estimate_skew(gray)
        info["angle"] = a
        if abs(a) >= 0.1:
            out = rotate(out, a, bg)
            if opts.border:
                out = remove_borders(out, bg)
    if opts.despeckle:
        out = despeckle(out, bg)
    if opts.enhance:
        out = enhance(out)
    if opts.binarize:
        out = binarize(out)
    return out, info


# ================================================================ 擦除与写入
def line_masks(ink: np.ndarray, min_len: int, max_thick: int | None = None) -> np.ndarray:
    """检测表格线：又长又细的水平 / 垂直线段。

    max_thick：线的最大粗细（像素）。大字的描边、外发光连成的宽色带虽然很长但很厚，不是表格线。
    """
    h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1)))
    v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len)))
    if max_thick is not None:
        for m, axis in ((h, 3), (v, 2)):            # 水平线看高度，垂直线看宽度
            n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            thick = stats[:, axis] > max_thick
            thick[0] = False
            if thick.any():
                m[thick[labels]] = 0
    return cv2.bitwise_or(h, v)


@dataclass
class EraseInfo:
    method: str              # fill / inpaint
    kept_lines: bool


def _fit_background(cf: np.ndarray, ring: np.ndarray):
    """用环带像素拟合平滑背景（每通道二次曲面，可贴合渐变），迭代剔除环带里的其他图案。

    返回 (整个裁剪区的背景估计, 背景残差标准差, 环带内点比例)。
    """
    H, W, C = cf.shape
    ys, xs = np.nonzero(ring)
    if len(xs) > 40000:
        sel = np.random.default_rng(0).choice(len(xs), 40000, replace=False)
        ys, xs = ys[sel], xs[sel]

    def design(x, y):
        u = x / max(W, 1) - 0.5
        v = y / max(H, 1) - 0.5
        return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)

    A = design(xs.astype(np.float32), ys.astype(np.float32))
    vals = cf[ys, xs]
    keep = np.ones(len(xs), bool)
    coef = None
    for _ in range(4):
        if keep.sum() < 12:
            break
        coef, *_r = np.linalg.lstsq(A[keep], vals[keep], rcond=None)
        res = np.abs(vals - A @ coef).max(axis=1)
        mad = float(np.median(res[keep]))
        keep = res < max(8.0, 3.5 * 1.4826 * mad)
    if coef is None:
        med = np.median(vals, axis=0)
        return np.broadcast_to(med, cf.shape).astype(np.float32), 99.0, 0.0
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    bg = (design(gx.ravel(), gy.ravel()) @ coef).reshape(H, W, C).astype(np.float32)
    resid = (vals - A @ coef)[keep]
    return bg, float(resid.std()) if len(resid) else 99.0, float(keep.mean())


def erase_text(arr: np.ndarray, box: tuple, dpi: float, everything: bool = False) -> tuple[np.ndarray, EraseInfo]:
    """擦除像素框 box=(x0,y0,x1,y1) 内的文字，使该处恢复为干净背景。

    1. 用文字框外一圈环带拟合背景（支持渐变），环带中的其他图案作为离群点剔除；
    2. 与背景色差大的像素都算前景（深色字、浅色描边、彩色外发光都包括），再用滞后阈值连带淡的光晕；
    3. 去掉表格线（长横 / 竖线保留不擦）；
    4. 背景平滑：用拟合背景 + 同等噪点填充（无接缝）；背景有纹理：图像修复。
    everything=True：区域擦除工具，擦除框内一切与背景不同的内容（手写字、污点、印章）。
    """
    x0, y0, x1, y1 = box
    H0, W0 = arr.shape[:2]
    h = max(1, y1 - y0)
    halo = 0 if everything else max(3, int(h * 0.5))           # 描边 / 外发光可能超出文字框
    ix0, iy0 = max(0, x0 - halo), max(0, y0 - halo)
    ix1, iy1 = min(W0, x1 + halo), min(H0, y1 + halo)
    ring = max(6, int(h * 0.5), int(dpi / 300 * 12))
    X0, Y0 = max(0, ix0 - ring), max(0, iy0 - ring)
    X1, Y1 = min(W0, ix1 + ring), min(H0, iy1 + ring)
    crop = arr[Y0:Y1, X0:X1]
    cf = crop.astype(np.float32)
    if cf.ndim == 2:
        cf = cf[:, :, None]
    ch, cw = cf.shape[:2]
    inner = np.zeros((ch, cw), bool)
    inner[iy0 - Y0:iy1 - Y0, ix0 - X0:ix1 - X0] = True
    core = np.zeros((ch, cw), bool)
    core[max(0, y0 - Y0 - 2):y1 - Y0 + 2, max(0, x0 - X0 - 2):x1 - X0 + 2] = True
    ring_mask = ~inner
    if ring_mask.sum() < 50:            # 文字贴着图像边缘：退化为用框内亮度分布估计
        ring_mask = np.ones_like(inner)
    bg, rstd, inliers = _fit_background(cf, ring_mask)
    diff = np.abs(cf - bg).max(axis=2)
    thr = max(22.0, 4.0 * rstd)
    hi = (diff > thr).astype(np.uint8) * 255
    lo = diff > thr * 0.45

    # 表格线：远长于字的笔画
    lines = (line_masks(hi, max(int(dpi / 300 * 60), int(h * 1.6)), max(5, int(dpi / 300 * 8)))
             if not everything else np.zeros_like(hi))
    if lines.any():
        # 真正的表格线会延伸到文字区域之外；完全在框内的细长线段是文字描边的边缘，不保留
        n, labels, _st, _c = cv2.connectedComponentsWithStats(lines, 8)
        outside = np.bincount(labels[~inner].ravel(), minlength=n) > 0
        outside[0] = False
        lines = (outside[labels] * 255).astype(np.uint8)
    lines_present = bool(lines.any())
    cand = cv2.bitwise_and(hi, cv2.bitwise_not(cv2.dilate(lines, np.ones((3, 3), np.uint8))))
    if everything:
        mask = (cand > 0) & core
    else:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
        area = np.maximum(stats[:, cv2.CC_STAT_AREA], 1)
        in_inner = np.bincount(labels[inner].ravel(), minlength=n)
        in_core = np.bincount(labels[core].ravel(), minlength=n)
        # 属于这一行的连通块：大部分落在（含光晕的）框内，且与文字框本身有交集
        keep = (in_inner >= 0.5 * area) & (in_core > 0)
        keep[0] = False
        mask = keep[labels]
        # 滞后阈值：把与之相连的淡光晕、抗锯齿边缘也连带进来（限制在框内）
        grow = lo & inner & ~(lines > 0)
        k = np.ones((5, 5), np.uint8)
        m8 = mask.astype(np.uint8)
        for _ in range(min(40, halo // 2 + 2)):
            nxt = cv2.dilate(m8, k) & grow.astype(np.uint8) | m8
            if (nxt == m8).all():
                break
            m8 = nxt
        mask = m8 > 0
    mask8 = cv2.dilate(mask.astype(np.uint8) * 255, np.ones((3, 3), np.uint8),
                       iterations=max(1, int(dpi / 300 * 2)))
    if everything:
        mask8[~core] = 0
    else:
        mask8[~inner] = 0
        mask8[lines > 0] = 0                # 膨胀后仍不覆盖表格线

    out = arr.copy()
    region = out[Y0:Y1, X0:X1]
    sel = mask8 > 0
    smooth = rstd < 12 and inliers > 0.55
    if smooth:
        noise = np.random.default_rng(0).normal(0, min(rstd, 6), cf.shape)
        fill = np.clip(bg + noise, 0, 255)
        # 边缘羽化：掩码外 2~3 像素渐变过渡，避免出现填充区的硬边
        a = sel.astype(np.float32)
        soft = cv2.GaussianBlur(a, (0, 0), max(1.0, dpi / 300 * 2))
        alpha = np.maximum(a, np.clip(soft * 2, 0, 1))
        if not everything:
            alpha[~inner] = 0
            alpha[lines > 0] = 0
        reg = region.astype(np.float32)
        if reg.ndim == 2:
            reg = reg[:, :, None]
        blended = reg * (1 - alpha[:, :, None]) + fill * alpha[:, :, None]
        blended = np.clip(blended, 0, 255).astype(np.uint8)
        region[:] = blended[:, :, 0] if region.ndim == 2 else blended
        method = "fill"
    else:
        region[:] = cv2.inpaint(region, mask8, max(3, int(dpi / 300 * 5)), cv2.INPAINT_TELEA)
        method = "inpaint"
    return out, EraseInfo(method, lines_present)


def composite_rgba(arr: np.ndarray, rgba: np.ndarray, x: int, y: int, texture: bool = False) -> np.ndarray:
    """把 RGBA 文字图层贴到扫描图上（可模拟扫描质感：轻微模糊与噪点）。"""
    out = arr.copy()
    h, w = rgba.shape[:2]
    H, W = out.shape[:2]
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(W, x + w), min(H, y + h)
    if dx1 <= dx0 or dy1 <= dy0:
        return out
    patch = rgba[sy0:sy0 + dy1 - dy0, sx0:sx0 + dx1 - dx0].astype(np.float32)
    alpha = patch[:, :, 3:4] / 255.0
    color = patch[:, :, :3]
    if texture:
        alpha = cv2.GaussianBlur(alpha, (3, 3), 0.7)[:, :, None]
        color = color + np.random.default_rng(1).normal(0, 8, color.shape)
    dst = out[dy0:dy1, dx0:dx1].astype(np.float32)
    if out.ndim == 2:
        g = color @ np.array([0.299, 0.587, 0.114], np.float32)
        dst = dst * (1 - alpha[:, :, 0]) + g * alpha[:, :, 0]
    else:
        dst = dst * (1 - alpha) + color * alpha
    out[dy0:dy1, dx0:dx1] = np.clip(dst, 0, 255).astype(np.uint8)
    return out
