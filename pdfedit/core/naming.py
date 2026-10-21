"""批量导出命名模板（F-EXP-20/21/23）。

支持变量：{原文件名} {工作区页码} {原页码} {序号} ，数字变量可加位数，如 {序号:3} → 001。
"""
from __future__ import annotations

import os
import re

DEFAULT_TEMPLATE = "{原文件名}_{序号:3}"
VARIABLES = ["原文件名", "工作区页码", "原页码", "序号"]

_VAR_RE = re.compile(r"\{([^{}:]+)(?::(\d+))?\}")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def render_name(template: str, values: dict) -> str:
    def sub(m):
        key, width = m.group(1).strip(), m.group(2)
        if key not in values:
            raise ValueError(f"未知变量：{{{key}}}")
        v = values[key]
        if width and isinstance(v, int):
            return str(v).zfill(int(width))
        return str(v)

    name = _VAR_RE.sub(sub, template).strip()
    name = _ILLEGAL.sub("_", name).strip(" .")
    return name or "未命名"


def validate_template(template: str):
    render_name(template, {"原文件名": "a", "工作区页码": 1, "原页码": 1, "序号": 1})


def resolve_conflict(path: str, policy: str, reserved: set) -> str | None:
    """处理重名。policy: suffix 自动加后缀 | overwrite 覆盖 | skip 跳过。

    reserved：本次批量导出中已经占用的路径（防止同批次内互相覆盖）。
    返回最终路径；跳过时返回 None。
    """
    norm = os.path.normcase(os.path.abspath(path))
    exists = os.path.exists(path) or norm in reserved
    if exists:
        if policy == "skip" and norm not in reserved:
            return None
        if policy == "suffix" or norm in reserved:
            base, ext = os.path.splitext(path)
            i = 1
            while True:
                cand = f"{base}({i}){ext}"
                cn = os.path.normcase(os.path.abspath(cand))
                if not os.path.exists(cand) and cn not in reserved:
                    path, norm = cand, cn
                    break
                i += 1
    reserved.add(norm)
    return path
