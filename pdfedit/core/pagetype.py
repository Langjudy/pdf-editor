"""页面类型自动识别（技术要求 3.2）。

按页统计：文字字符数 N、图片覆盖面积比例 R、矢量路径数量 P、乱码比例，
再按规则判定。阈值集中在 THRESHOLDS，便于在样本库上调优。
"""
from __future__ import annotations

import re

import pymupdf as fitz

TEXT = "text"        # 矢量文字页
SCAN = "scan"        # 扫描图片页
OUTLINE = "outline"  # 文字转曲线页
GARBLED = "garbled"  # 乱码文字页
MIXED = "mixed"      # 混合页
BLANK = "blank"      # 空白页
REGION = "region"    # 区域页（由区域选取生成，不参与自动识别）

LABELS = {
    TEXT: "矢量", SCAN: "扫描", OUTLINE: "曲线", GARBLED: "乱码",
    MIXED: "混合", BLANK: "空白", REGION: "区域",
}
ALL_TYPES = [TEXT, SCAN, OUTLINE, GARBLED, MIXED, BLANK]

THRESHOLDS = {
    "scan_ratio": 0.80,      # 图片覆盖 ≥80% 视为扫描底图
    "few_chars": 10,         # 字符数少于此视为“几乎无文字”
    "outline_paths": 200,    # 路径数超过此视为大量矢量线条
    "garbled_ratio": 0.30,   # 无法映射字符占比超过此视为乱码
    "mixed_image": 0.25,     # 文字页上图片占比超过此视为混合
}


_SPACE_RE = re.compile(r"\s+")
# 无法映射的字符：替换符、私用区、控制字符
_BAD_RE = re.compile("[�-\x00-\x08\x0b\x0c\x0e-\x1f]")


def page_stats(page: fitz.Page) -> dict:
    # 只用 C 层的提取 + 正则统计，避免逐字符 Python 循环长时间占用 GIL（会卡住界面）
    area = abs(page.rect) or 1.0
    text = _SPACE_RE.sub("", page.get_text("text", flags=0))
    n = len(text)
    bad = len(_BAD_RE.findall(text))
    # 图片覆盖面积：逐个裁剪到页面内后累加，封顶 1.0（重叠时近似）
    img_area = 0.0
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            img_area += abs(r)
    ratio = min(1.0, img_area / area)
    try:
        paths = len(page.get_cdrawings())
    except Exception:  # noqa: BLE001
        paths = 0
    return {"chars": n, "bad": bad, "image_ratio": ratio, "paths": paths}


def classify_stats(s: dict) -> str:
    t = THRESHOLDS
    n, ratio, paths = s["chars"], s["image_ratio"], s["paths"]
    garbled = n >= t["few_chars"] and s["bad"] / max(n, 1) > t["garbled_ratio"]
    hits = []
    if ratio >= t["scan_ratio"] and n < t["few_chars"]:
        hits.append(SCAN)
    if n >= t["few_chars"]:
        hits.append(GARBLED if garbled else TEXT)
    if n < t["few_chars"] and ratio < t["mixed_image"] and paths > t["outline_paths"]:
        hits.append(OUTLINE)
    # 文字页上还有大面积图片 → 混合页（如扫描件上加了文字层以外的矢量文字）
    if n >= t["few_chars"] and ratio >= t["mixed_image"]:
        if ratio >= t["scan_ratio"]:
            # 扫描底图 + 文字层：带 OCR 文字层的扫描页
            return SCAN if not garbled else MIXED
        hits.append(MIXED)
    if not hits:
        if ratio >= t["mixed_image"]:
            return SCAN
        if n == 0 and paths == 0:
            return BLANK
        if paths > t["outline_paths"] or n == 0:
            return OUTLINE    # 没有可提取文字的图形页（含文字转曲线）：按扫描页方式编辑
        return TEXT           # 少量真实文字 + 简单图形的矢量页
    if MIXED in hits or len(set(hits)) > 1:
        return MIXED
    return hits[0]


def classify(page: fitz.Page) -> str:
    return classify_stats(page_stats(page))
