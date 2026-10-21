"""导出服务：把工作区页面按各种模式导出为新文件（技术要求第 6 节）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..core.engine import GEN_SOURCE_ID, DocumentStore
from ..core.naming import DEFAULT_TEMPLATE, render_name, resolve_conflict
from ..core.writer import ExportCancelled, PageItem, SaveOptions, export_image, export_pdf
from .workspace import PageRef

MODES = {
    "all_one": "全部页面合并为一个 PDF",
    "sel_one": "选中页面合并为一个 PDF",
    "sel_each": "选中页面每页一个 PDF",
    "all_each": "全部页面每页一个 PDF",
    "chunk": "按固定页数拆分",
    "by_source": "按来源文件拆分",
    "images": "导出为图片",
}


@dataclass
class ExportJob:
    mode: str
    refs: list[PageRef]                 # 参与导出的页面（已按工作区顺序）
    positions: list[int]                # 每页的工作区页码（从 1 开始）
    target: str                         # 单文件模式为文件路径，其余为文件夹
    template: str = DEFAULT_TEMPLATE
    conflict: str = "suffix"            # suffix | overwrite | skip
    chunk_size: int = 10
    image_format: str = "png"
    image_dpi: int = 150
    save: SaveOptions = field(default_factory=SaveOptions)


@dataclass
class ExportResult:
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    folder: str = ""


def origin_source(store: DocumentStore, ref: PageRef) -> tuple[str, int]:
    """返回 (来源文档 id, 原页号 0 起)；区域页追溯到其来源页。"""
    if ref.src == GEN_SOURCE_ID:
        org = store.gen_origins.get(ref.pno)
        if org:
            return org
    return ref.src, ref.pno


def origin_of(store: DocumentStore, ref: PageRef) -> tuple[str, int]:
    """返回页面的原文件名与原页码（从 1 开始）。"""
    sid, pno = origin_source(store, ref)
    if sid == GEN_SOURCE_ID:
        return "空白页", pno + 1
    return store.get(sid).name, pno + 1


def _items(refs: list[PageRef]) -> list[PageItem]:
    return [PageItem(r.src, r.pno, r.rot) for r in refs]


def run_export(store: DocumentStore, job: ExportJob, progress=None) -> ExportResult:
    """progress(done, total, text) 返回 False 表示取消。"""
    res = ExportResult()
    total_refs = len(job.refs)
    if total_refs == 0:
        raise ValueError("没有可导出的页面")

    def tick(done, total, text=""):
        if progress and not progress(done, total, text):
            raise ExportCancelled()

    if job.mode in ("all_one", "sel_one"):
        res.folder = os.path.dirname(os.path.abspath(job.target))
        export_pdf(store, _items(job.refs), job.target, job.save,
                   progress=lambda d, t: progress(d, t, "合并页面") if progress else True)
        res.files.append(job.target)
        return res

    folder = job.target
    os.makedirs(folder, exist_ok=True)
    res.folder = folder
    reserved: set = set()

    # 组装分组：每组 = (页面列表, 命名用的首页信息)
    groups: list[list[int]] = []
    if job.mode in ("sel_each", "all_each", "images"):
        groups = [[i] for i in range(total_refs)]
    elif job.mode == "chunk":
        n = max(1, job.chunk_size)
        groups = [list(range(i, min(i + n, total_refs))) for i in range(0, total_refs, n)]
    elif job.mode == "by_source":
        order: dict[str, list[int]] = {}
        for i, r in enumerate(job.refs):
            sid, _ = origin_source(store, r)
            order.setdefault(sid, []).append(i)
        groups = list(order.values())
    else:
        raise ValueError(f"未知导出模式：{job.mode}")

    ext = "." + (job.image_format if job.mode == "images" else "pdf")
    for gi, group in enumerate(groups, start=1):
        first = job.refs[group[0]]
        name, orig_page = origin_of(store, first)
        if job.mode == "by_source":
            base = name
        else:
            base = render_name(job.template, {
                "原文件名": name, "工作区页码": job.positions[group[0]],
                "原页码": orig_page, "序号": gi,
            })
        path = resolve_conflict(os.path.join(folder, base + ext), job.conflict, reserved)
        if path is None:
            res.skipped.append(base + ext)
            continue
        tick(gi - 1, len(groups), os.path.basename(path))
        if job.mode == "images":
            export_image(store, _items([first])[0], path, job.image_dpi)
        else:
            export_pdf(store, _items([job.refs[i] for i in group]), path, job.save)
        res.files.append(path)
    tick(len(groups), len(groups))
    return res
