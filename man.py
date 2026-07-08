#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
man.py — FreeBSD man 手册英文 GitBook 项目生成器（单体脚本）

功能：
  1. 从 en/freebsd-src-main.zip 提取并转换所有实际安装的 man 页面（man1-man9）
     为 Markdown（GitBook 格式），输出到 en/manN/ 下。
  2. 精准识别 MLINKS 别名（如 vi/edit），别名独立生成文件（重复内容，标题用别名），
     别名清单写入 .github/aliases.txt。
  3. 每个页面的 .Dd（日期）与 .Dt（标题+章节）单独写入 .github/dates/，方便比较更新。
  4. 精准识别交叉引用 .Xr，生成 markdown 链接，目标不存在时降级为纯文本。
  5. 生成 SUMMARY.md（根目录），man2/man3 按子目录二级标题分组，
     man2/man3 建空 README.md。
  6. 所有 md 文件名小写、Windows 兼容。
  7. 集成 AutoCorrect、md-padding 清理（仅生成差异报告，人工逐条复核）。

设计：
  - 纯 Python 标准库实现，无第三方依赖（mandoc 在 Windows 无预编译版本，
    且 mandoc 不直接输出 markdown；本脚本内置 mdoc→markdown 解析器）。
  - 幂等：重复运行覆盖已有文件。
  - 流程主体发生在 en/ 文件夹；SUMMARY.md、.github/ 为特别指定输出。

用法：
  python man.py preview man      # 仅转换 man(1) 预览
  python man.py all              # 转换所有 man 页面
  python man.py summary          # 仅重新生成 SUMMARY.md
  python man.py clean            # 运行 AutoCorrect/md-padding 差异报告
  python man.py dates            # 仅重新生成 .github/dates/
  python man.py aliases          # 仅重新生成 .github/aliases.txt

依赖：
  - Python 3.9+（标准库 zipfile/re/os/sys/json/pathlib）
  - 可选：mandoc（用于 lint 校验，非转换必需）
  - 可选：autocorrect、md-padding（用于最终清理差异报告）
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

# ============================================================
# 配置
# ============================================================

ROOT = Path(__file__).resolve().parent
EN_DIR = ROOT / "en"
ZIP_PATH = EN_DIR / "freebsd-src-main.zip"
SRC_DIR = EN_DIR / "freebsd-src-main"  # 解压目录
GITHUB_DIR = ROOT / ".github"
DATES_DIR = GITHUB_DIR / "dates"
ALIASES_FILE = GITHUB_DIR / "aliases.txt"
SUMMARY_FILE = ROOT / "SUMMARY.md"

# man 章节中文名（SUMMARY.md 用）
SECTION_TITLES = {
    1: "man1",
    2: "man2",
    3: "man3",
    4: "man4",
    5: "man5",
    6: "man6",
    7: "man7",
    8: "man8",
    9: "man9",
}

# mdoc .Sh 章节中文翻译
SH_TITLES = {
    "NAME": "名称",
    "SYNOPSIS": "概要",
    "DESCRIPTION": "描述",
    "OPTIONS": "选项",
    "EXIT STATUS": "退出状态",
    "EXAMPLES": "实例",
    "SEE ALSO": "参见",
    "STANDARDS": "标准",
    "HISTORY": "历史",
    "AUTHORS": "作者",
    "BUGS": "缺陷",
    "CAVEATS": "注意事项",
    "DIAGNOSTICS": "诊断",
    "ERRORS": "错误",
    "ENVIRONMENT": "环境变量",
    "FILES": "文件",
    "LEGAL": "法律条款",
    "WARNING": "警告",
    "RETURN VALUES": "返回值",
    "COMPATIBILITY": "兼容性",
    "IMPLEMENTATION NOTES": "实现说明",
    "PROGRAMMING GUIDE": "编程指南",
    "INTERNALS": "内部实现",
    "HARDWARE": "硬件",
    "PROTOCOLS": "协议",
    "DESCRIPTIONS": "描述",
    "DEVICE FLAGS": "设备标志",
    "LOADER TUNABLES": "加载器可调参数",
    "SYSCTL VARIABLES": "sysctl 变量",
    "AUTOCONFIGURATION": "自动配置",
    "DIAGNOSTICS": "诊断",
    "SYSTEM MANAGER'S MANUAL": "系统管理员手册",
}

# 实际安装 man 页面的源码树位置（man1-man9）
# share/man/manN/ 下的直接安装；命令 man 页面分散在 bin/sbin/usr.bin/ 等
MAN_SOURCE_DIRS = [
    "share/man/man{N}",       # 系统手册
    "bin",                    # 基本命令 (cat.1 等)
    "sbin",                   # 系统命令
    "usr.bin",                # 用户命令
    "usr.sbin",               # 系统管理命令
    "libexec",                # 库可执行文件
    "stand",                  # 引导加载器
    "gnu/usr.bin",            # GNU 工具
    "gnu/usr.sbin",           # GNU 系统工具
    "cddl/usr.bin",           # CDDL 工具
    "cddl/usr.sbin",          # CDDL 系统工具
    "secure/usr.bin",         # 安全工具
    "secure/usr.sbin",        # 安全系统工具
    "kerberos5/usr.bin",      # Kerberos
    "kerberos5/usr.sbin",     # Kerberos 系统
    "kerberos5/lib",          # Kerberos 库
    "crypto/openssh",         # OpenSSH
    "lib",                    # 库（man2/man3 分散在 lib/libc/ 等）
    "libexec/rtld-elf",       # 运行时链接器
    "sys",                    # 内核（部分 man4/man9）
]


# ============================================================
# 工具函数
# ============================================================

def log(msg: str) -> None:
    print(f"[man.py] {msg}", flush=True)


def safe_filename(name: str) -> str:
    """转小写，替换 Windows 非法字符。"""
    name = name.lower()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def section_from_suffix(filename: str) -> Optional[int]:
    """从文件名提取章节号，如 man.1 -> 1, strcpy.3 -> 3, glxsb.4.i386 -> 4。"""
    m = re.search(r'\.(\d+)(?:\.[a-z0-9]+)?$', filename)
    if m:
        return int(m.group(1))
    return None


def read_zip_text(zf: zipfile.ZipFile, name: str) -> str:
    return zf.read(name).decode("utf-8", "replace")


# ============================================================
# 数据源：zip 解压与 man 页面扫描
# ============================================================

def extract_zip(force: bool = False) -> None:
    """解压 freebsd-src-main.zip 到 en/freebsd-src-main/。"""
    if SRC_DIR.exists() and not force:
        # 检查是否已解压完整（通过标志文件）
        if (SRC_DIR / "README.md").exists() or (SRC_DIR / "share").exists():
            log(f"已解压到 {SRC_DIR}，跳过（使用 force=True 强制重解压）")
            return
    if not ZIP_PATH.exists():
        raise FileNotFoundError(f"未找到 {ZIP_PATH}")
    log(f"解压 {ZIP_PATH.name} 到 {SRC_DIR}...")
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(EN_DIR)
    log("解压完成")


def scan_man_files() -> List[Path]:
    """扫描所有实际安装的 man 页面源文件（.1-.9），返回路径列表。"""
    if not SRC_DIR.exists():
        raise FileNotFoundError(f"源码树不存在：{SRC_DIR}，请先解压")
    results: List[Path] = []
    seen: Set[Path] = set()
    # 1. share/man/manN/ 下所有文件
    for n in range(1, 10):
        d = SRC_DIR / "share" / "man" / f"man{n}"
        if d.exists():
            for p in d.iterdir():
                if p.is_file() and re.match(rf'^[^.]+\.{n}(\.[a-z0-9]+)?$', p.name):
                    if p not in seen:
                        seen.add(p)
                        results.append(p)
    # 2. 分散在 bin/sbin/usr.bin/ 等的命令 man 页面
    skip_dirs = {"contrib", "tests", "tools", "release", "packages"}
    for top in ["bin", "sbin", "usr.bin", "usr.sbin", "libexec", "stand",
                "gnu/usr.bin", "gnu/usr.sbin", "cddl/usr.bin", "cddl/usr.sbin",
                "secure/usr.bin", "secure/usr.sbin", "kerberos5/usr.bin",
                "kerberos5/usr.sbin", "kerberos5/lib", "crypto/openssh",
                "libexec/rtld-elf"]:
        d = SRC_DIR / top
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            # 跳过 contrib/tests/tools 等
            rel = p.relative_to(SRC_DIR).as_posix()
            if any(rel.startswith(s + "/") for s in skip_dirs):
                continue
            n = section_from_suffix(p.name)
            if n is None or n < 1 or n > 9:
                continue
            # 文件名应为 命令.N 格式
            if not re.match(rf'^[^/]+\.{n}(\.[a-z0-9]+)?$', p.name):
                continue
            if p not in seen:
                seen.add(p)
                results.append(p)
    # 3. lib/ 下的库函数 man2/man3（如 lib/libc/string/strcpy.3）
    lib_dir = SRC_DIR / "lib"
    if lib_dir.exists():
        for p in lib_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC_DIR).as_posix()
            if any(rel.startswith(s + "/") for s in skip_dirs):
                continue
            n = section_from_suffix(p.name)
            if n is None or n not in (2, 3, 9):
                continue
            if not re.match(rf'^[^/]+\.{n}(\.[a-z0-9]+)?$', p.name):
                continue
            if p not in seen:
                seen.add(p)
                results.append(p)
    # 4. sys/ 下的内核 man4/man9
    sys_dir = SRC_DIR / "sys"
    if sys_dir.exists():
        for p in sys_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC_DIR).as_posix()
            if any(rel.startswith(s + "/") for s in skip_dirs):
                continue
            n = section_from_suffix(p.name)
            if n is None or n not in (4, 9):
                continue
            if not re.match(rf'^[^/]+\.{n}(\.[a-z0-9]+)?$', p.name):
                continue
            if p not in seen:
                seen.add(p)
                results.append(p)
    return sorted(results)


# ============================================================
# MLINKS 别名解析
# ============================================================

def parse_mlinks() -> Dict[str, str]:
    """从所有 Makefile 解析 MLINKS，返回 {别名目标文件相对路径: 主文件相对路径}。

    MLINKS 格式：
      MLINKS = cat.1 catcat.1 \\
               dog.1 dogdog.1
    每对 (主, 别名)，别名文件链接到主文件。
    返回的 key 为别名（如 catcat.1），value 为主文件（如 cat.1）。
    """
    aliases: Dict[str, str] = {}
    pattern = re.compile(r'^MLINKS\s*[+:]?=\s*(.*)$', re.MULTILINE)
    if not SRC_DIR.exists():
        return aliases
    for mk in SRC_DIR.rglob("Makefile"):
        try:
            text = mk.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 合并续行
        text = re.sub(r'\\\s*\n\s*', ' ', text)
        for m in pattern.finditer(text):
            chunk = m.group(1)
            # 截取到下一个变量赋值或行尾
            chunk = re.split(r'\s+\w+\s*[+:]?=', chunk)[0]
            tokens = chunk.split()
            # 成对处理
            for i in range(0, len(tokens) - 1, 2):
                main = tokens[i]
                alias = tokens[i + 1]
                if main == alias:
                    continue
                # 仅处理 .1-.9 文件
                if not re.match(r'^[^.\s]+\.\d+(\.[a-z0-9]+)?$', main):
                    continue
                if not re.match(r'^[^.\s]+\.\d+(\.[a-z0-9]+)?$', alias):
                    continue
                # 别名 → 主
                if alias not in aliases:
                    aliases[alias] = main
    return aliases


# ============================================================
# mdoc 解析器
# ============================================================

class MdocLine:
    """一行 mdoc，可能是注释、宏、文本。"""
    __slots__ = ("raw", "macro", "args", "is_macro", "is_comment", "is_blank")

    def __init__(self, raw: str):
        self.raw = raw
        stripped = raw.rstrip("\n")
        self.is_comment = stripped.startswith(r".\"") or stripped.startswith("\\\"")
        self.is_blank = (stripped == "")
        if stripped.startswith(".") and not stripped.startswith(r".\""):
            # 宏行
            parts = stripped[1:].split(None, 1)
            self.macro = parts[0] if parts else ""
            self.args = parts[1] if len(parts) > 1 else ""
            self.is_macro = True
        else:
            self.macro = ""
            self.args = stripped
            self.is_macro = False


class MdocParser:
    """解析 mdoc 文本为 token 流，供渲染器使用。

    解析策略：逐行处理，维护上下文（当前列表、显示块等）。
    保留 macro 语义，渲染器决定输出。
    """

    def __init__(self, text: str):
        self.lines = [MdocLine(l) for l in text.splitlines()]

    def parse(self) -> List[dict]:
        """返回 token 列表，每个 token 为 dict:
        {type: 'macro'|'text'|'blank', macro, args, raw}
        """
        tokens: List[dict] = []
        for ln in self.lines:
            if ln.is_comment:
                continue
            if ln.is_blank:
                tokens.append({"type": "blank"})
                continue
            tokens.append({
                "type": "macro" if ln.is_macro else "text",
                "macro": ln.macro,
                "args": ln.args,
                "raw": ln.raw,
            })
        return tokens


# ============================================================
# markdown 渲染器
# ============================================================

class CrossRefDB:
    """交叉引用数据库：name+N → 输出路径（相对 SUMMARY）。"""

    def __init__(self):
        # key: (name_lower, section), value: 相对路径如 man1/man.1.md
        self.entries: Dict[Tuple[str, int], str] = {}
        # 别名：alias_lower → (主 name_lower, section)
        self.alias_map: Dict[str, Tuple[str, int]] = {}

    def register(self, name: str, section: int, rel_path: str) -> None:
        self.entries[(name.lower(), section)] = rel_path

    def register_alias(self, alias: str, main: str, section: int) -> None:
        self.alias_map[alias.lower()] = (main.lower(), section)

    def resolve(self, name: str, section: int) -> Optional[str]:
        """返回相对路径，或 None（不存在）。"""
        key = (name.lower(), section)
        if key in self.entries:
            return self.entries[key]
        # 查别名
        a = self.alias_map.get(name.lower())
        if a and a[1] == section:
            return self.entries.get(a)
        return None


class MarkdownRenderer:
    """把 mdoc token 流渲染为 GitBook markdown。

    参考 CLAUDE.md 的 mdoc→markdown 映射规则。
    """

    def __init__(self, tokens: List[dict], page_name: str, page_section: int,
                 xref: Optional[CrossRefDB] = None,
                 current_section: int = 1):
        self.tokens = tokens
        self.page_name = page_name  # 命令名（如 man）
        self.page_section = page_section  # 章节号
        self.xref = xref or CrossRefDB()
        self.current_section = current_section  # 当前文件所属章节，用于计算跨章节链接
        self.out: List[str] = []
        # 列表栈
        self.list_stack: List[dict] = []  # 每项 {type, compact, counter}
        # 显示块栈
        self.display_stack: List[str] = []  # 'literal'
        # SYNOPSIS 标志
        self.in_synopsis = False
        # 当前章节
        self.current_sh = ""
        # 缓冲：SYNOPSIS 整行处理
        self.synopsis_lines: List[str] = []

    # ---------- 输出辅助 ----------
    def emit(self, s: str = "") -> None:
        self.out.append(s)

    def blank(self) -> None:
        if self.out and self.out[-1] != "":
            self.out.append("")

    # ---------- macro 参数解析 ----------
    @staticmethod
    def split_args(s: str) -> List[str]:
        """拆分 macro 参数，保留引号内容。"""
        if not s:
            return []
        # 用 shlex 风格拆分，但 mdoc 引号简单
        args: List[str] = []
        i = 0
        n = len(s)
        while i < n:
            while i < n and s[i] in " \t":
                i += 1
            if i >= n:
                break
            if s[i] == '"':
                i += 1
                start = i
                buf = []
                while i < n and s[i] != '"':
                    if s[i] == '\\' and i + 1 < n:
                        buf.append(s[i + 1])
                        i += 2
                    else:
                        buf.append(s[i])
                        i += 1
                args.append("".join(buf))
                if i < n:
                    i += 1  # 跳过结尾 "
            else:
                start = i
                while i < n and s[i] not in " \t":
                    i += 1
                args.append(s[start:i])
        return args

    # ---------- 内联 macro 渲染 ----------
    def render_inline(self, s: str) -> str:
        """渲染内联 macro 文本（可能含多个 macro），返回 markdown 字符串。

        mdoc 内联 macro 以 . 开头，但一行可能有多个（用空格分隔）。
        本函数处理常见的内联 macro：.Nm .Fl .Ar .Op .Xr .Cd .Va .Vt .Fa
        .Ic .Li .Em .Sy .Dq .Sq .Ql .Qq .Pq .Fx .Tn .An .Ns .Pa .Ev .Cm .No
        """
        if not s:
            return ""
        # 按 macro 拆分：macro 以 . 开头后跟字母
        # 但文本中 . 也可能是普通句点。mdoc 中 macro 总是在行首或紧跟空格后的 .X
        # 简化：用正则找 \.[A-Z][a-z]+ 模式
        result: List[str] = []
        i = 0
        n = len(s)
        text_buf: List[str] = []

        def flush_text():
            if text_buf:
                result.append("".join(text_buf))
                text_buf.clear()

        while i < n:
            # 识别 macro：.后跟大写字母+小写字母
            m = re.match(r'\.([A-Z][a-z]+)(\s+|$)', s[i:])
            if m:
                flush_text()
                macro = m.group(1)
                rest_start = i + m.end()
                # 取该 macro 的参数（直到下一个 macro 或行尾）
                rest = s[rest_start:]
                # 找下一个 macro
                next_m = re.search(r'\s\.([A-Z][a-z]+)(\s+|$)', rest)
                if next_m:
                    arg_str = rest[:next_m.start()]
                    consumed = rest_start + next_m.start()
                else:
                    arg_str = rest
                    consumed = n
                args = self.split_args(arg_str)
                result.append(self.render_one_macro(macro, args))
                i = consumed
            else:
                text_buf.append(s[i])
                i += 1
        flush_text()
        return "".join(result)

    def render_one_macro(self, macro: str, args: List[str]) -> str:
        """渲染单个内联 macro。"""
        if macro == "Nm":
            # 名称：用 page_name（无参数时）或参数
            name = args[0] if args else self.page_name
            return f"`{name}`"
        if macro == "Fl":
            # 选项标志
            if args:
                return f"`-{''.join(args)}`"
            return "`-`"
        if macro == "Ar":
            if args:
                return f"`{' '.join(args)}`"
            return "`...`"
        if macro == "Op":
            # 可选参数 [contents]
            inner = self.render_inline(" ".join(args))
            return f"[{inner}]"
        if macro == "Cm":
            return f"`{' '.join(args)}`"
        if macro == "Ic":
            return f"`{' '.join(args)}`"
        if macro == "Li":
            return f"`{' '.join(args)}`"
        if macro == "Pa":
            # 路径：项目规范用加粗
            if args:
                return f"**{' '.join(args)}**"
            return "**/**"
        if macro == "Va":
            return f"`{' '.join(args)}`"
        if macro == "Vt":
            return f"`{' '.join(args)}`"
        if macro == "Fa":
            return f"`{' '.join(args)}`"
        if macro == "Ev":
            return f"`{' '.join(args)}`"
        if macro == "Dv":
            return f"`{' '.join(args)}`"
        if macro == "Er":
            return f"`{' '.join(args)}`"
        if macro == "Cd":
            return f"`{' '.join(args)}`"
        if macro == "Em":
            # 强调：项目规范无斜体，用文本
            return " ".join(args)
        if macro == "Sy":
            # 符号：保留原文
            return " ".join(args)
        if macro == "No":
            # 普通文本
            return " ".join(args)
        if macro == "Tn":
            # 商标：保留英文
            return " ".join(args)
        if macro == "Fx":
            # FreeBSD 版本
            return f"FreeBSD {' '.join(args)}".strip()
        if macro == "Nx" or macro == "Ox" or macro == "Bx" or macro == "Bsx":
            return " ".join(args)
        if macro == "An":
            # 作者名：保留英文
            return " ".join(args)
        if macro == "Ql":
            # 引用字面量
            return f"`{' '.join(args)}`"
        if macro == "Dq":
            # 双引号 → 中文引号
            inner = self.render_inline(" ".join(args))
            return f"“{inner}”"
        if macro == "Sq":
            inner = self.render_inline(" ".join(args))
            return f"‘{inner}’"
        if macro == "Qq":
            inner = self.render_inline(" ".join(args))
            return f"“{inner}”"
        if macro == "Pq":
            # 括号引用
            inner = self.render_inline(" ".join(args))
            return f"({inner})"
        if macro == "Brq":
            inner = self.render_inline(" ".join(args))
            return f"{{{inner}}}"
        if macro == "Ns":
            # 无空格连接：忽略（前文已直接拼接）
            return ""
        if macro == "Xr":
            # 交叉引用：name section
            if len(args) >= 2:
                name = args[0]
                sec = args[1]
                try:
                    sec_n = int(re.match(r'\d+', sec).group(0))
                except (AttributeError, ValueError):
                    sec_n = 0
                link = self.xref.resolve(name, sec_n) if sec_n else None
                if link:
                    return f"[{name}({sec})]({link})"
                return f"{name}({sec})"
            return " ".join(args)
        if macro == "Sx":
            # 章节引用：内部锚点
            title = " ".join(args)
            anchor = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
            return f"[{title}](#{anchor})"
        if macro == "Rs":
            return ""
        if macro == "%A" or macro == "%T" or macro == "%B" or macro == "%J" \
                or macro == "%D" or macro == "%I" or macro == "%P" or macro == "%V" \
                or macro == "%C" or macro == "%N" or macro == "%Q" or macro == "%R":
            return " ".join(args)
        # 未知 macro：保留参数
        return " ".join(args)

    # ---------- 列表处理 ----------
    def list_open(self, args: List[str]) -> None:
        """处理 .Bl。"""
        spec = " ".join(args)
        entry = {
            "type": "item",  # 默认
            "compact": "-compact" in spec,
            "column": "-column" in spec,
            "tag": "-tag" in spec,
            "enum": "-enum" in spec,
            "hang": "-hang" in spec,
            "offset": "",
            "width": "",
            "counter": 0,
            "header": [],  # column 表头
            "rows": [],  # column 行
        }
        m = re.search(r'-offset\s+(\S+)', spec)
        if m:
            entry["offset"] = m.group(1)
        m = re.search(r'-width\s+(\S+)', spec)
        if m:
            entry["width"] = m.group(1)
        self.list_stack.append(entry)

    def list_close(self) -> None:
        """处理 .El。"""
        if not self.list_stack:
            return
        entry = self.list_stack.pop()
        if entry["column"] and entry["rows"]:
            self.render_column_table(entry)
        elif entry["enum"] and not entry["compact"]:
            # enum 已在 It 时输出
            pass
        self.blank()

    def render_column_table(self, entry: dict) -> None:
        """渲染 -column 列表为 markdown 表格。"""
        rows = entry["rows"]
        if not rows:
            return
        # 第一行可能是表头
        header = rows[0]
        body = rows[1:]
        ncols = max(len(r) for r in rows)
        # 补齐
        for r in rows:
            while len(r) < ncols:
                r.append("")
        # 表头
        self.emit("| " + " | ".join(header) + " |")
        self.emit("|" + "|".join(["---"] * ncols) + "|")
        for r in body:
            self.emit("| " + " | ".join(r) + " |")

    # ---------- 主渲染 ----------
    def render(self) -> str:
        i = 0
        n = len(self.tokens)
        while i < n:
            tok = self.tokens[i]
            if tok["type"] == "blank":
                # 段落分隔：不输出连续空行
                if self.out and self.out[-1] != "" and not self.list_stack:
                    # 在列表内不随意加空行
                    pass
                i += 1
                continue
            if tok["type"] == "text":
                # 纯文本行（在显示块内或列表项内）
                if self.display_stack:
                    self.emit(tok["raw"].rstrip())
                elif self.list_stack:
                    # 列表项内的续行文本
                    pass
                else:
                    self.emit(self.render_inline(tok["args"]))
                i += 1
                continue
            # macro
            macro = tok["macro"]
            args_str = tok["args"]
            args = self.split_args(args_str)

            if macro == "Dd":
                # 日期：不输出到正文（由 dates 模块处理）
                i += 1
                continue
            if macro == "Dt":
                i += 1
                continue
            if macro == "Os":
                i += 1
                continue
            if macro == "Sh":
                title_en = args_str.strip()
                title_zh = SH_TITLES.get(title_en.upper(), title_en)
                self.current_sh = title_en.upper()
                self.in_synopsis = (self.current_sh == "SYNOPSIS")
                self.blank()
                # GitBook 风格：## 中文标题
                self.emit(f"## {title_zh}")
                self.blank()
                i += 1
                continue
            if macro == "Ss":
                title = args_str.strip()
                self.blank()
                self.emit(f"### {title}")
                self.blank()
                i += 1
                continue
            if macro == "Nm":
                # NAME 章节下 .Nm 描述行；SYNOPSIS 下整行处理
                if self.current_sh == "NAME":
                    # NAME 章节通常是 .Nm name .Nd desc
                    # 已在 Nd 处理
                    pass
                elif self.in_synopsis:
                    # SYNOPSIS 行：整行渲染
                    line = self.render_synopsis_line(tok)
                    self.emit(line)
                else:
                    self.emit(self.render_one_macro("Nm", args))
                i += 1
                continue
            if macro == "Nd":
                # 名称描述：NAME 章节下，.Nm name .Nd desc → `name` — desc
                desc = self.render_inline(args_str)
                # 上一行应是 .Nm
                self.emit(f"`{self.page_name}` — {desc}")
                self.blank()
                i += 1
                continue
            if macro == "Pp":
                self.blank()
                i += 1
                continue
            if macro == "Bl":
                self.list_open(args)
                i += 1
                continue
            if macro == "El":
                self.list_close()
                i += 1
                continue
            if macro == "It":
                self.render_list_item(args_str, args)
                i += 1
                continue
            if macro == "Bd":
                # 显示块
                if "-literal" in args_str or "-literal" in " ".join(args):
                    self.display_stack.append("literal")
                    self.emit("```sh")
                elif "-filled" in args_str or "-ragged" in args_str:
                    self.display_stack.append("filled")
                else:
                    self.display_stack.append("block")
                i += 1
                continue
            if macro == "Ed":
                if self.display_stack:
                    kind = self.display_stack.pop()
                    if kind == "literal":
                        self.emit("```")
                        self.blank()
                i += 1
                continue
            if macro == "D1":
                # 单行缩进显示
                self.emit("```sh")
                self.emit(self.render_inline(args_str))
                self.emit("```")
                self.blank()
                i += 1
                continue
            if macro == "Dl":
                # 单行字面量
                self.emit("```sh")
                self.emit(args_str)
                self.emit("```")
                self.blank()
                i += 1
                continue
            if macro == "Ex":
                # .Ex -std [cmd ...] → 退出状态标准文本
                # 简化：输出 ".Ex -std" 的常见语义
                if args and args[0] == "-std":
                    cmd = args[1] if len(args) > 1 else self.page_name
                    self.emit(f"`{cmd}` 实用程序在成功时退出状态为 0，失败时退出状态为 1。")
                else:
                    self.emit(self.render_inline(args_str))
                self.blank()
                i += 1
                continue
            if macro == "Rs":
                # 参考文献开始
                i += 1
                continue
            if macro == "Re":
                self.blank()
                i += 1
                continue
            if macro == "%A" or macro == "%T" or macro == "%B" or macro == "%J" \
                    or macro == "%D" or macro == "%I" or macro == "%P" or macro == "%V" \
                    or macro == "%C" or macro == "%N" or macro == "%Q" or macro == "%R":
                # 参考文献字段：简化为文本
                self.emit(self.render_one_macro(macro, args))
                i += 1
                continue
            if macro == "Ta":
                # 表格列分隔：在 -column 列表项内
                if self.list_stack and self.list_stack[-1]["column"]:
                    # 当前行已收集，Ta 分隔
                    pass
                i += 1
                continue
            if macro == "Sm":
                # 空格模式：忽略
                i += 1
                continue
            # 未识别 macro：尝试内联渲染
            rendered = self.render_inline(f".{macro} {args_str}".strip())
            if rendered.strip():
                self.emit(rendered)
            i += 1
        # 清理多余空行
        out = "\n".join(self.out)
        out = re.sub(r'\n{3,}', '\n\n', out)
        return out.strip() + "\n"

    def render_synopsis_line(self, start_tok: dict) -> str:
        """渲染 SYNOPSIS 章节的一行（可能跨多个 macro）。"""
        # SYNOPSIS 中 .Nm 开头的一行，整行用单反引号包裹
        # 收集本行所有 token（同一原始行的后续 macro）
        # 简化：只处理当前 token，内联渲染后包裹
        rendered = self.render_inline(f".{start_tok['macro']} {start_tok['args']}".strip())
        return f"`{rendered}`"

    def render_list_item(self, args_str: str, args: List[str]) -> None:
        """处理 .It。"""
        if not self.list_stack:
            self.emit(self.render_inline(args_str))
            return
        entry = self.list_stack[-1]
        if entry["column"]:
            # -column 列表项：一行表格
            # .It Ta 分隔
            cells = re.split(r'\s+Ta\s+', args_str)
            cells = [self.render_inline(c).replace("|", "\\|").strip() for c in cells]
            entry["rows"].append(cells)
            return
        if entry["tag"]:
            # -tag 列表：.It Fl v → `v` 描述
            # .It 后第一个 token 是标签
            head = args_str.split(None, 1)
            if head:
                tag_macro = head[0].lstrip(".")
                tag_args = head[1] if len(head) > 1 else ""
                # 处理 .It Fl v / .It Cm x / .It Ev X / .It Pa path 等
                if tag_macro in ("Fl", "Cm", "Ic", "Li", "Va", "Vt", "Fa", "Ev", "Dv", "Er", "Cd", "Ar"):
                    tag = self.render_one_macro(tag_macro, self.split_args(tag_args))
                elif tag_macro == "Xr":
                    xa = self.split_args(tag_args)
                    tag = self.render_one_macro("Xr", xa)
                else:
                    tag = self.render_inline(args_str)
                self.emit(f"- **{tag}**")
            else:
                self.emit("- ")
            return
        if entry["enum"]:
            entry["counter"] += 1
            self.emit(f"{entry['counter']}. {self.render_inline(args_str)}")
            return
        # item / hang
        self.emit(f"- {self.render_inline(args_str)}")
        return


# ============================================================
# 转换：单文件
# ============================================================

def parse_header(text: str) -> Tuple[str, int, str]:
    """从 mdoc 文本解析 .Dt（标题 章节号）和 .Dd（日期）。
    返回 (name, section, date)。
    """
    name = ""
    section = 0
    date = ""
    for line in text.splitlines():
        if line.startswith(".Dt "):
            parts = line.split()
            if len(parts) >= 2:
                name = parts[1]
            if len(parts) >= 3:
                try:
                    section = int(re.match(r'\d+', parts[2]).group(0))
                except (AttributeError, ValueError):
                    pass
        elif line.startswith(".Dd "):
            date = line[4:].strip()
    return name, section, date


def convert_one(src_path: Path, out_dir: Path, xref: CrossRefDB,
                alias_name: Optional[str] = None) -> Path:
    """转换单个 mdoc 文件为 markdown。
    alias_name: 若为别名，用此名作为标题与输出文件名。
    返回输出文件路径。
    """
    text = src_path.read_text(encoding="utf-8", errors="replace")
    name, section, date = parse_header(text)
    if not name:
        # 从文件名推导
        name = src_path.name.split(".")[0]
    if not section:
        section = section_from_suffix(src_path.name) or 1

    display_name = alias_name or name
    # 输出文件名：小写，命令.N.md
    out_name = f"{safe_filename(display_name)}.{section}.md"
    out_path = out_dir / out_name

    tokens = MdocParser(text).parse()
    renderer = MarkdownRenderer(tokens, display_name, section, xref, section)
    body = renderer.render()

    # 标题：别名用别名，否则用 name(section)
    title = f"{display_name}({section})"
    content = f"# {title}\n\n{body}"

    # 提取日期/版本信息
    out_path.write_text(content, encoding="utf-8")
    return out_path


def collect_dates(src_path: Path) -> Tuple[str, int, str]:
    """返回 (name, section, date)。"""
    text = src_path.read_text(encoding="utf-8", errors="replace")
    return parse_header(text)


# ============================================================
# SUMMARY 生成
# ============================================================

def build_summary(entries: List[dict]) -> str:
    """生成 SUMMARY.md 内容。
    entries: [{section, name, rel_path, group?}]
    group: man2/man3 的子目录分组名（如 string, stdio）
    """
    lines = ["# Table of contents", "", "* [man 页](README.md)", "* [目录](mu-lu.md)", ""]
    # 按章节分组
    by_sec: Dict[int, List[dict]] = {}
    for e in entries:
        by_sec.setdefault(e["section"], []).append(e)
    for sec in sorted(by_sec.keys()):
        title = SECTION_TITLES.get(sec, f"man{sec}")
        lines.append(f"## {title}")
        lines.append("")
        items = sorted(by_sec[sec], key=lambda x: (x.get("group", ""), x["name"].lower()))
        # man2/man3 按 group 分组
        if sec in (2, 3):
            groups: Dict[str, List[dict]] = {}
            for e in items:
                g = e.get("group", "")
                groups.setdefault(g, []).append(e)
            for g in sorted(groups.keys()):
                if g:
                    lines.append(f"### {g}")
                    lines.append("")
                for e in groups[g]:
                    lines.append(f"* [{e['name']}({sec})]({e['rel_path']})")
                lines.append("")
        else:
            for e in items:
                lines.append(f"* [{e['name']}({sec})]({e['rel_path']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ============================================================
# 清理集成（AutoCorrect / md-padding）
# ============================================================

def run_cleaners_check(target_dir: Path) -> None:
    """运行 AutoCorrect 和 md-padding 的差异检查（不修改文件）。
    输出差异报告到 script/ 下供人工逐条复核。
    """
    report_dir = ROOT / "script"
    report_dir.mkdir(exist_ok=True)
    patterns = ['"**/*.md"', f'"{target_dir.relative_to(ROOT)}/**"']
    # AutoCorrect
    ac_report = report_dir / "autocorrect_report.txt"
    try:
        r = subprocess.run(
            ["autocorrect", "--lint", str(target_dir)],
            capture_output=True, text=True, cwd=ROOT
        )
        ac_report.write_text(r.stdout + r.stderr, encoding="utf-8")
        log(f"AutoCorrect 报告：{ac_report}")
    except FileNotFoundError:
        log("AutoCorrect 未安装，跳过（安装：cargo install autocorrect 或 npm i -g autocorrect）")
    # md-padding
    mdp_report = report_dir / "mdpadding_report.txt"
    try:
        r = subprocess.run(
            ["md-padding", "--check", str(target_dir)],
            capture_output=True, text=True, cwd=ROOT
        )
        mdp_report.write_text(r.stdout + r.stderr, encoding="utf-8")
        log(f"md-padding 报告：{mdp_report}")
    except FileNotFoundError:
        log("md-padding 未安装，跳过（安装：npm i -g md-padding@latest）")


# ============================================================
# 主入口
# ============================================================

def cmd_preview(name: str) -> None:
    """预览模式：仅转换指定名称的 man 页面（如 man, cat, ls）。"""
    extract_zip()
    files = scan_man_files()
    target = None
    for p in files:
        stem = p.name.split(".")[0]
        if stem.lower() == name.lower():
            target = p
            break
    if not target:
        log(f"未找到 {name} 的 man 页面")
        sys.exit(1)
    log(f"转换 {target} 预览...")
    xref = CrossRefDB()
    # 预览模式：xref 仅注册自身
    section = section_from_suffix(target.name) or 1
    out_dir = EN_DIR / f"man{section}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # README.md for man2/man3
    if section in (2, 3):
        readme = out_dir / "README.md"
        if not readme.exists():
            readme.write_text(f"# man{section}\n", encoding="utf-8")
    out_path = convert_one(target, out_dir, xref)
    log(f"已生成：{out_path}")
    log("预览内容前 60 行：")
    print("\n".join(out_path.read_text(encoding="utf-8").splitlines()[:60]))


def cmd_all() -> None:
    """全量转换。"""
    extract_zip()
    files = scan_man_files()
    log(f"发现 {len(files)} 个 man 页面源文件")
    aliases = parse_mlinks()
    log(f"发现 {len(aliases)} 个 MLINKS 别名")
    # 构建交叉引用库
    xref = CrossRefDB()
    summary_entries: List[dict] = []
    dates_data: Dict[int, List[Tuple[str, str]]] = {}  # section -> [(name, date)]
    # 先注册所有主文件
    for p in files:
        name, section, date = collect_dates(p)
        if not name:
            continue
        rel = f"man{section}/{safe_filename(name)}.{section}.md"
        xref.register(name, section, rel)
        # 注册别名
    for alias, main in aliases.items():
        m = re.match(r'^([^.\s]+)\.(\d+)', main)
        a = re.match(r'^([^.\s]+)\.(\d+)', alias)
        if m and a:
            xref.register_alias(a.group(1), m.group(1), int(m.group(2)))
    # 转换主文件
    for p in files:
        name, section, date = collect_dates(p)
        if not name:
            continue
        out_dir = EN_DIR / f"man{section}"
        out_dir.mkdir(parents=True, exist_ok=True)
        # man2/man3 子目录分组（基于源路径，如 lib/libc/string/strcpy.3 → group=string）
        group = ""
        if section in (2, 3):
            rel = p.relative_to(SRC_DIR).as_posix()
            # 提取 lib/libX/<group>/ 中的 group
            gm = re.match(r'lib/[^/]+/([^/]+)/', rel)
            if gm:
                group = gm.group(1)
        try:
            convert_one(p, out_dir, xref)
        except Exception as e:
            log(f"转换失败 {p}: {e}")
            continue
        rel_path = f"en/man{section}/{safe_filename(name)}.{section}.md"
        # SUMMARY 路径相对根目录
        summary_entries.append({
            "section": section, "name": name, "rel_path": rel_path, "group": group
        })
        dates_data.setdefault(section, []).append((name, date))
    # 转换别名（重复内容，标题用别名）
    alias_entries: List[Tuple[str, str, int]] = []  # (alias, main, section)
    for alias, main in aliases.items():
        m = re.match(r'^([^.\s]+)\.(\d+)', main)
        a = re.match(r'^([^.\s]+)\.(\d+)', alias)
        if not (m and a):
            continue
        main_name = m.group(1)
        sec = int(m.group(2))
        # 找主文件源路径
        main_src = None
        for p in files:
            if p.name.split(".")[0].lower() == main_name.lower() and \
               section_from_suffix(p.name) == sec:
                main_src = p
                break
        if not main_src:
            continue
        alias_name = a.group(1)
        out_dir = EN_DIR / f"man{sec}"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            convert_one(main_src, out_dir, xref, alias_name=alias_name)
        except Exception as e:
            log(f"别名转换失败 {alias_name}: {e}")
            continue
        rel_path = f"en/man{sec}/{safe_filename(alias_name)}.{sec}.md"
        summary_entries.append({
            "section": sec, "name": alias_name, "rel_path": rel_path, "group": ""
        })
        alias_entries.append((alias_name, main_name, sec))
    log(f"转换完成：{len(summary_entries)} 个条目")
    # 生成 SUMMARY.md
    SUMMARY_FILE.write_text(build_summary(summary_entries), encoding="utf-8")
    log(f"已生成 {SUMMARY_FILE}")
    # 生成 .github/aliases.txt
    GITHUB_DIR.mkdir(exist_ok=True)
    with open(ALIASES_FILE, "w", encoding="utf-8") as f:
        for alias, main, sec in sorted(alias_entries):
            f.write(f"{alias}|{main}|{sec}\n")
    log(f"已生成 {ALIASES_FILE}（{len(alias_entries)} 条）")
    # 生成 .github/dates/
    DATES_DIR.mkdir(parents=True, exist_ok=True)
    for sec, items in dates_data.items():
        with open(DATES_DIR / f"man{sec}.txt", "w", encoding="utf-8") as f:
            for name, date in sorted(items):
                f.write(f"{name}\t{date}\n")
    log(f"已生成 {DATES_DIR}")
    # man2/man3 README.md
    for sec in (2, 3):
        readme = EN_DIR / f"man{sec}" / "README.md"
        if not readme.exists():
            readme.write_text(f"# man{sec}\n", encoding="utf-8")


def cmd_summary() -> None:
    """仅重新生成 SUMMARY.md（基于已有 en/manN/ 目录）。"""
    entries: List[dict] = []
    for sec in range(1, 10):
        d = EN_DIR / f"man{sec}"
        if not d.exists():
            continue
        for p in sorted(d.glob(f"*.{sec}.md")):
            name = p.stem.rsplit(".", 1)[0]
            rel = f"en/man{sec}/{p.name}"
            entries.append({"section": sec, "name": name, "rel_path": rel, "group": ""})
    SUMMARY_FILE.write_text(build_summary(entries), encoding="utf-8")
    log(f"已重新生成 {SUMMARY_FILE}（{len(entries)} 条）")


def cmd_clean() -> None:
    """运行清理差异报告。"""
    run_cleaners_check(EN_DIR)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "preview":
        if len(sys.argv) < 3:
            log("用法：python man.py preview <name>  如：python man.py preview man")
            sys.exit(1)
        cmd_preview(sys.argv[2])
    elif cmd == "all":
        cmd_all()
    elif cmd == "summary":
        cmd_summary()
    elif cmd == "clean":
        cmd_clean()
    elif cmd == "dates":
        # 仅重新生成 dates
        extract_zip()
        files = scan_man_files()
        DATES_DIR.mkdir(parents=True, exist_ok=True)
        by_sec: Dict[int, List[Tuple[str, str]]] = {}
        for p in files:
            name, section, date = collect_dates(p)
            if name and section:
                by_sec.setdefault(section, []).append((name, date))
        for sec, items in by_sec.items():
            with open(DATES_DIR / f"man{sec}.txt", "w", encoding="utf-8") as f:
                for name, date in sorted(items):
                    f.write(f"{name}\t{date}\n")
        log(f"已生成 {DATES_DIR}")
    elif cmd == "aliases":
        extract_zip()
        aliases = parse_mlinks()
        GITHUB_DIR.mkdir(exist_ok=True)
        with open(ALIASES_FILE, "w", encoding="utf-8") as f:
            for alias in sorted(aliases.keys()):
                main = aliases[alias]
                m = re.match(r'^([^.\s]+)\.(\d+)', main)
                a = re.match(r'^([^.\s]+)\.(\d+)', alias)
                if m and a:
                    f.write(f"{a.group(1)}|{m.group(1)}|{m.group(2)}\n")
        log(f"已生成 {ALIASES_FILE}（{len(aliases)} 条）")
    else:
        print(f"未知命令：{cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
