"""工作区项目文件（F-PG-32）与自动保存 / 崩溃恢复（技术要求 10.3）。

项目文件为 JSON：记录源文件路径、工作区页面顺序、选区；内部生成页（区域页、
空白页）以 base64 嵌入。不保存密码，重新打开加密文件时再次询问。
"""
from __future__ import annotations

import base64
import json
import os
from typing import Callable

from ..core.engine import ENGINE_LOCK, GEN_SOURCE_ID, DocumentStore, Source
from .regions import RegionStore
from .workspace import PageRef, Workspace

PROJECT_EXT = ".pdfproj"
VERSION = 1


def app_data_dir() -> str:
    """程序数据目录：Windows %LOCALAPPDATA%，macOS ~/Library/Application Support，Linux ~/.local/share。"""
    import sys
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or home
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    d = os.path.join(base, "PdfEdit")
    os.makedirs(d, exist_ok=True)
    return d


def autosave_path() -> str:
    return os.path.join(app_data_dir(), "autosave" + PROJECT_EXT)


def running_flag_path() -> str:
    return os.path.join(app_data_dir(), "running.flag")


def save_project(path: str, store: DocumentStore, ws: Workspace, regions: RegionStore):
    with ENGINE_LOCK:
        gen = store.gen.doc
        gen_bytes = gen.tobytes(garbage=3, deflate=True) if gen.page_count else b""
    used = {r.src for r in ws.refs} | {sid for sid, _ in store.gen_origins.values()}
    data = {
        "version": VERSION,
        "sources": [
            {"id": s.id, "path": s.path, "name": s.name, "overrides": s.type_overrides}
            for s in store.sources.values() if s.id != GEN_SOURCE_ID and s.id in used
        ],
        "generated": base64.b64encode(gen_bytes).decode("ascii"),
        "gen_origins": {str(k): list(v) for k, v in store.gen_origins.items()},
        "gen_ocr": sorted(store.gen_ocr),
        "gen_redacted": sorted(store.gen_redacted),
        "refs": [[r.uid, r.src, r.pno, r.rot, r.kind] for r in ws.refs],
        "regions": regions.to_json(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_project(path: str, store: DocumentStore, ws: Workspace, regions: RegionStore,
                 opener: Callable[[str, str], Source | None]) -> list[str]:
    """加载项目到（已清空的）store / ws / regions。

    opener(path, sid)：负责打开源文件（含密码询问），失败返回 None。
    返回问题列表（缺失的源文件等）。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != VERSION:
        raise ValueError("不支持的项目文件版本")
    problems = []
    ok_sources = {GEN_SOURCE_ID}
    for s in data["sources"]:
        if not s.get("path") or not os.path.exists(s["path"]):
            problems.append(f"源文件不存在：{s.get('path')}")
            continue
        src = opener(s["path"], s["id"])
        if src is None:
            problems.append(f"无法打开：{s['path']}")
            continue
        src.type_overrides = {int(k): v for k, v in (s.get("overrides") or {}).items()}
        ok_sources.add(src.id)
    store.load_generated(base64.b64decode(data.get("generated") or ""), data.get("gen_origins") or {},
                         data.get("gen_ocr") or [], data.get("gen_redacted") or [])
    refs = []
    for uid, sid, pno, rot, kind in data["refs"]:
        if sid in ok_sources and pno < store.get(sid).page_count:
            refs.append(PageRef(uid, sid, pno, rot, kind))
    dropped = len(data["refs"]) - len(refs)
    if dropped:
        problems.append(f"{dropped} 个页面因源文件缺失未能恢复")
    ws.reset(refs)
    regions.load_json(data.get("regions") or {})
    return problems
