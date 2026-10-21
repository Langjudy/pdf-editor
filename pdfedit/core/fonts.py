"""字体管理（技术要求 7.3）。

- 扫描系统字体目录，解析 TrueType/OpenType 的 name 表得到中英文字体名（纯 Python，只读文件头，很快）；
- 按 7.3 规则为新文字选择字体：
  原嵌入字体（能覆盖全部新字符时）→ 系统同名字体 → 用户配置的替代字体 → 默认中文字体；
- 写入时只嵌入用到的字形子集（见 textedit.write_overlay）。
"""
from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import asdict, dataclass

import pymupdf as fitz

from .engine import ENGINE_LOCK

FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
]
FONT_EXTS = (".ttf", ".ttc", ".otf")

# 默认中文字体候选（按优先级）
DEFAULT_CJK = ["SimSun", "Microsoft YaHei", "SimHei", "DengXian", "KaiTi", "FangSong"]

# PDF 中常见字体名 → 系统字体族名
ALIASES = {
    "宋体": "SimSun", "新宋体": "NSimSun", "黑体": "SimHei", "楷体": "KaiTi", "楷体_gb2312": "KaiTi",
    "仿宋": "FangSong", "仿宋_gb2312": "FangSong", "微软雅黑": "Microsoft YaHei", "等线": "DengXian",
    "stsong": "STSong", "华文宋体": "STSong", "华文仿宋": "STFangsong", "华文楷体": "STKaiti",
    "华文细黑": "STXihei", "华文中宋": "STZhongsong",
    "timesnewroman": "Times New Roman", "timesnewromanpsmt": "Times New Roman", "times": "Times New Roman",
    "arialmt": "Arial", "helvetica": "Arial", "couriernew": "Courier New", "courier": "Courier New",
    "kaitigb2312": "KaiTi", "fangsonggb2312": "FangSong_GB2312",
    "microsoftyahei": "Microsoft YaHei", "microsoftyaheiui": "Microsoft YaHei", "simsun": "SimSun", "simhei": "SimHei",
}


@dataclass
class FontEntry:
    family: str           # 英文族名
    family_zh: str        # 中文族名（没有则为空）
    style: str            # Regular / Bold / Italic ...
    path: str
    index: int            # TTC 中的序号（仅支持 0）

    @property
    def bold(self) -> bool:
        return "bold" in self.style.lower()

    @property
    def italic(self) -> bool:
        s = self.style.lower()
        return "italic" in s or "oblique" in s

    @property
    def display(self) -> str:
        return f"{self.family_zh}（{self.family}）" if self.family_zh else self.family


def norm(name: str) -> str:
    """统一字体名：去子集前缀、样式后缀、空格符号，小写。"""
    name = name.split("+", 1)[-1]
    name = re.sub(r"[,\-\s](bold|italic|regular|boldmt|bolditalic|semibold|light|medium)(?![a-z]).*$", "",
                  name, flags=re.I)
    name = re.sub(r"[\s\-_,]", "", name).lower()
    return name


# ---------------------------------------------------------------- name 表解析
def _read_name_table(f, offset: int) -> dict:
    f.seek(offset)
    _ver, num_tables = struct.unpack(">IH", f.read(6))
    f.read(6)
    table_off = None
    for _ in range(num_tables):
        tag, _chk, off, _len = struct.unpack(">4sIII", f.read(16))
        if tag == b"name":
            table_off = off
    if table_off is None:
        return {}
    f.seek(table_off)
    _fmt, count, str_off = struct.unpack(">HHH", f.read(6))
    records = [struct.unpack(">HHHHHH", f.read(12)) for _ in range(count)]
    out: dict = {}
    for plat, enc, lang, nid, length, off in records:
        if nid not in (1, 2, 16, 17):
            continue
        f.seek(table_off + str_off + off)
        raw = f.read(length)
        try:
            if plat == 3 or plat == 0:
                s = raw.decode("utf-16-be")
            elif plat == 1 and enc == 0:
                s = raw.decode("mac_roman")
            else:
                continue
        except UnicodeDecodeError:
            continue
        zh = plat == 3 and lang in (0x0804, 0x0404, 0x0C04, 0x1004)
        en = (plat == 3 and lang == 0x0409) or plat in (0, 1)
        key = ("zh" if zh else "en" if en else None)
        if key:
            out.setdefault((key, nid), s)
    return out


def read_font_names(path: str) -> list[tuple[int, dict]]:
    """返回 [(index, names)]；TTC 只取第 0 个字体（PyMuPDF 只能加载第 0 个）。"""
    with open(path, "rb") as f:
        head = f.read(4)
        if head == b"ttcf":
            f.read(4)
            num = struct.unpack(">I", f.read(4))[0]
            offs = struct.unpack(f">{num}I", f.read(4 * num))
            return [(0, _read_name_table(f, offs[0]))]
        return [(0, _read_name_table(f, 0))]


class FontManager:
    def __init__(self, cache_path: str | None = None):
        self.cache_path = cache_path
        self.entries: list[FontEntry] = []
        self._by_name: dict[str, list[FontEntry]] = {}
        self._fonts: dict = {}          # 缓存的 fitz.Font
        self.substitute: str = ""       # 用户配置的替代字体族名
        self.default_cjk: str = "SimSun"
        self._loaded = False

    # ---------- 扫描 ----------
    def ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        files = []
        for d in FONT_DIRS:
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if fn.lower().endswith(FONT_EXTS):
                        p = os.path.join(d, fn)
                        files.append((p, os.path.getmtime(p)))
        sig = sorted(f"{os.path.basename(p)}:{int(m)}" for p, m in files)
        cached = self._read_cache(sig)
        if cached is not None:
            self.entries = cached
        else:
            for p, _ in files:
                try:
                    for idx, names in read_font_names(p):
                        fam = names.get(("en", 16)) or names.get(("en", 1))
                        if not fam:
                            continue
                        zh = names.get(("zh", 16)) or names.get(("zh", 1)) or ""
                        style = names.get(("en", 17)) or names.get(("en", 2)) or "Regular"
                        self.entries.append(FontEntry(fam, zh, style, p, idx))
                except Exception:  # noqa: BLE001  个别损坏字体文件直接跳过
                    continue
            self._write_cache(sig)
        self._index()

    def _index(self):
        self._by_name.clear()
        for e in self.entries:
            for n in (e.family, e.family_zh):
                if n:
                    self._by_name.setdefault(norm(n), []).append(e)

    def _read_cache(self, sig):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("sig") != sig:
                return None
            return [FontEntry(**e) for e in data["entries"]]
        except Exception:  # noqa: BLE001
            return None

    def _write_cache(self, sig):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"sig": sig, "entries": [asdict(e) for e in self.entries]}, f, ensure_ascii=False)
        except OSError:
            pass

    # ---------- 查询 ----------
    def families(self) -> list[FontEntry]:
        """每个族一个代表（Regular 优先），中文字体排前面。"""
        self.ensure_loaded()
        best: dict[str, FontEntry] = {}
        for e in self.entries:
            cur = best.get(e.family)
            if cur is None or (cur.bold or cur.italic) and not (e.bold or e.italic):
                best[e.family] = e
        return sorted(best.values(), key=lambda e: (not e.family_zh, e.family.lower()))

    def find(self, name: str, bold: bool = False, italic: bool = False) -> FontEntry | None:
        """按字体名查找系统字体（支持 PDF 字体名、中文名、别名）。"""
        self.ensure_loaded()
        key = norm(name)
        key = norm(ALIASES.get(key, ALIASES.get(name.split("+", 1)[-1], key)))
        cands = self._by_name.get(key)
        if not cands:
            # 去掉末尾的 mt / ps / psmt 等再试
            k2 = re.sub(r"(psmt|mt|ps)$", "", key)
            cands = self._by_name.get(norm(ALIASES.get(k2, k2)))
        if not cands:
            return None
        def score(e):
            return (e.bold != bold) * 2 + (e.italic != italic)
        return min(cands, key=score)

    def load(self, entry: FontEntry) -> fitz.Font:
        key = entry.path
        f = self._fonts.get(key)
        if f is None:
            with ENGINE_LOCK:
                f = fitz.Font(fontfile=entry.path)
            self._fonts[key] = f
        return f

    @staticmethod
    def covers(font: fitz.Font, text: str) -> bool:
        for ch in set(text):
            if ch.isspace():
                continue
            if not font.has_glyph(ord(ch)):
                return False
        return True

    def default_chain(self) -> list[str]:
        chain = []
        if self.substitute:
            chain.append(self.substitute)
        chain.append(self.default_cjk)
        chain += [n for n in DEFAULT_CJK if n not in chain]
        return chain


@dataclass
class ResolvedFont:
    font: fitz.Font
    name: str              # 实际使用的字体（显示用）
    substituted: bool      # 是否使用了替代字体
    fake_bold: bool        # 字体本身不是粗体但要求加粗 → 模拟加粗


def resolve_font(fm: FontManager, text: str, requested: str | None, orig_name: str = "",
                 orig_buffer: bytes | None = None, bold: bool = False) -> ResolvedFont:
    """按 7.3 规则选择字体。

    requested：用户在属性面板选择的字体族名；None 表示“保持原字体”。
    """
    sample = text or "字A"
    # 1. 原嵌入字体（需要带 Unicode 映射且覆盖全部字符）
    if requested is None and orig_buffer:
        try:
            with ENGINE_LOCK:
                f = fitz.Font(fontbuffer=orig_buffer)
            if fm.covers(f, sample):
                return ResolvedFont(f, orig_name.split("+", 1)[-1], False,
                                    bold and "bold" not in orig_name.lower())
        except Exception:  # noqa: BLE001
            pass
    # 2. 用户指定字体 / 系统同名字体
    first = requested or orig_name
    if first:
        e = fm.find(first, bold)
        if e:
            f = fm.load(e)
            if fm.covers(f, sample):
                # 用户指定的字体或系统同名字体，外观与原字体一致，不算“替代”
                return ResolvedFont(f, e.display, False, bold and not e.bold)
    # 3. 用户替代字体 → 默认中文字体
    for name in fm.default_chain():
        e = fm.find(name, bold)
        if e:
            f = fm.load(e)
            if fm.covers(f, sample):
                return ResolvedFont(f, e.display, True, bold and not e.bold)
    # 4. 实在找不到：MuPDF 内置中文字体
    with ENGINE_LOCK:
        f = fitz.Font("cjk")
    return ResolvedFont(f, "内置中文字体", True, bold)
