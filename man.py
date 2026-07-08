#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
man.py — FreeBSD man 手册英文 GitBook 项目生成器（单体脚本，调用 mandoc）

功能：
  1. 确保 mandoc_convert.exe 已编译（调用 script/build_mandoc.py）
  2. 从 en/freebsd-src-main.zip 提取源码树（或按需提取单文件预览）
  3. 扫描所有实际安装的 man 页面（man1-man9，含分散在 bin/sbin/usr.bin/lib/sys 等）
  4. 精准解析 MLINKS 别名（如 vi/edit），别名独立生成文件（重复内容，标题用别名）
  5. 调用 mandoc_convert.exe 将 mdoc→markdown，后处理：
     - H1 标题改为 `命令名(N)` 小写格式
     - 降级标题层级（mandoc 的 # → ##，## → ###），使页面 H1 为标题
     - 交叉引用 name(N) 链接化（同章节相对链接，跨章节 ../manN/）
     - 去除 mandoc 页脚行
  6. 生成 SUMMARY.md（根目录），man2/man3 按源码子目录二级标题分组
  7. 生成 .github/aliases.txt（别名清单）、.github/dates/（每页 .Dd 日期）
  8. man2/man3 建空 README.md
  9. 所有 md 文件名小写、Windows 兼容

用法：
  python man.py preview man      # 仅转换 man(1) 预览
  python man.py all              # 转换所有 man 页面
  python man.py summary          # 仅重新生成 SUMMARY.md（基于已有 en/manN/）
  python man.py dates            # 仅重新生成 .github/dates/
  python man.py aliases          # 仅重新生成 .github/aliases.txt
  python man.py clean            # 运行 AutoCorrect/md-padding 差异报告

依赖：
  - Python 3.9+（标准库 zipfile/re/subprocess/pathlib）
  - MinGW gcc（编译 mandoc；script/build_mandoc.py 自动调用）
  - 可选：autocorrect、md-padding（用于最终清理差异报告）

输出位置：
  - en/manN/命令.N.md    — 英文 markdown 页面
  - en/SUMMARY.md        — 英文项目 TOC（不覆盖根目录 SUMMARY.md）
  - SUMMARY.md           — 根目录 GitBook TOC（指向 en/manN/）
  - .github/aliases.txt  — MLINKS 别名清单
  - .github/dates/manN.txt — 每页日期
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ============================================================
# 路径配置
# ============================================================

ROOT = Path(__file__).resolve().parent
EN_DIR = ROOT / "en"
ZIP_PATH = EN_DIR / "freebsd-src-main.zip"
SRC_DIR = EN_DIR / "freebsd-src-main"  # 解压目录
BUILD_DIR = EN_DIR / "mandoc-build"
MANDOC_EXE = BUILD_DIR / "mandoc_convert.exe"
BUILD_SCRIPT = ROOT / "script" / "build_mandoc.py"

GITHUB_DIR = ROOT / ".github"
DATES_DIR = GITHUB_DIR / "dates"
ALIASES_FILE = GITHUB_DIR / "aliases.txt"
SUMMARY_FILE = ROOT / "SUMMARY.md"
EN_SUMMARY_FILE = EN_DIR / "SUMMARY.md"

# man 章节标题（SUMMARY.md 分组用）
SECTION_TITLES = {
    1: "man1", 2: "man2", 3: "man3", 4: "man4", 5: "man5",
    6: "man6", 7: "man7", 8: "man8", 9: "man9",
}


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


# ============================================================
# mandoc 编译保障
# ============================================================

def ensure_mandoc() -> None:
    """确保 mandoc_convert.exe 已编译，否则调用 build_mandoc.py。"""
    if MANDOC_EXE.exists():
        return
    log(f"mandoc_convert.exe 不存在，开始编译...")
    if not BUILD_SCRIPT.exists():
        raise FileNotFoundError(f"未找到构建脚本：{BUILD_SCRIPT}")
    r = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, text=True
    )
    if r.returncode != 0:
        log(r.stdout[-2000:])
        log(r.stderr[-2000:])
        raise RuntimeError("mandoc 编译失败，详见上方输出")
    if not MANDOC_EXE.exists():
        raise RuntimeError(f"编译完成但 {MANDOC_EXE} 仍不存在")
    log("mandoc_convert.exe 编译完成")


# ============================================================
# 数据源：zip 解压与 man 页面扫描
# ============================================================

def extract_zip(force: bool = False) -> None:
    """解压 freebsd-src-main.zip 到 en/freebsd-src-main/。"""
    if SRC_DIR.exists() and not force:
        if (SRC_DIR / "share").exists() or (SRC_DIR / "README.md").exists():
            log(f"已解压到 {SRC_DIR}，跳过（force=True 强制重解压）")
            return
    if not ZIP_PATH.exists():
        raise FileNotFoundError(f"未找到 {ZIP_PATH}")
    log(f"解压 {ZIP_PATH.name} 到 {SRC_DIR}（可能需要数分钟）...")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(EN_DIR)
    log("解压完成")


def find_man_in_zip(name: str, section: int) -> Optional[str]:
    """在 zip 中查找指定 name.N 的源文件路径，返回 zip 内路径。"""
    if not ZIP_PATH.exists():
        return None
    target_suffix = f"/{name}.{section}"
    with zipfile.ZipFile(ZIP_PATH) as zf:
        candidates = []
        for n in zf.namelist():
            if n.endswith(target_suffix):
                candidates.append(n)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            # 优先选 share/man/manN/ 下的
            for c in candidates:
                if f"share/man/man{section}/" in c:
                    return c
            # 否则选最短路径（通常是最直接的）
            return sorted(candidates, key=len)[0]
    return None


def scan_man_files() -> List[Path]:
    """扫描所有实际安装的 man 页面源文件（.1-.9），返回路径列表。"""
    if not SRC_DIR.exists():
        raise FileNotFoundError(f"源码树不存在：{SRC_DIR}，请先解压")
    results: List[Path] = []
    seen: Set[Path] = set()

    def matches_man(name: str, n: int) -> bool:
        return bool(re.match(rf'^[^/]+\.{n}(\.[a-z0-9]+)?$', name))

    # 1. share/man/manN/ 下所有文件
    for n in range(1, 10):
        d = SRC_DIR / "share" / "man" / f"man{n}"
        if d.exists():
            for p in d.iterdir():
                if p.is_file() and matches_man(p.name, n):
                    if p not in seen:
                        seen.add(p)
                        results.append(p)

    # 2. 分散在 bin/sbin/usr.bin/ 等的命令 man 页面
    skip_dirs = {"contrib", "tests", "tools", "release", "packages"}
    top_dirs = ["bin", "sbin", "usr.bin", "usr.sbin", "libexec", "stand",
                "gnu/usr.bin", "gnu/usr.sbin", "cddl/usr.bin", "cddl/usr.sbin",
                "secure/usr.bin", "secure/usr.sbin", "kerberos5/usr.bin",
                "kerberos5/usr.sbin", "kerberos5/lib", "crypto/openssh",
                "libexec/rtld-elf"]
    for top in top_dirs:
        d = SRC_DIR / top
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC_DIR).as_posix()
            if any(rel.startswith(s + "/") for s in skip_dirs):
                continue
            n = section_from_suffix(p.name)
            if n is None or n < 1 or n > 9:
                continue
            if not matches_man(p.name, n):
                continue
            if p not in seen:
                seen.add(p)
                results.append(p)

    # 3. lib/ 下的库函数 man2/man3/man9
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
            if not matches_man(p.name, n):
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
            if not matches_man(p.name, n):
                continue
            if p not in seen:
                seen.add(p)
                results.append(p)

    return sorted(results)


# ============================================================
# MLINKS 别名解析
# ============================================================

def parse_mlinks() -> Dict[str, str]:
    """从所有 Makefile 解析 MLINKS，返回 {别名文件名: 主文件名}。

    MLINKS 格式：
      MLINKS = cat.1 catcat.1 \\
               dog.1 dogdog.1
    每对 (主, 别名)。
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
        text = re.sub(r'\\\s*\n\s*', ' ', text)  # 合并续行
        for m in pattern.finditer(text):
            chunk = m.group(1)
            chunk = re.split(r'\s+\w+\s*[+:]?=', chunk)[0]
            tokens = chunk.split()
            for i in range(0, len(tokens) - 1, 2):
                main = tokens[i]
                alias = tokens[i + 1]
                if main == alias:
                    continue
                if not re.match(r'^[^.\s]+\.\d+(\.[a-z0-9]+)?$', main):
                    continue
                if not re.match(r'^[^.\s]+\.\d+(\.[a-z0-9]+)?$', alias):
                    continue
                if alias not in aliases:
                    aliases[alias] = main
    return aliases


# ============================================================
# mdoc 头部解析（.Dt / .Dd）
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
                m = re.match(r'\d+', parts[2])
                if m:
                    section = int(m.group(0))
        elif line.startswith(".Dd "):
            date = line[4:].strip()
    return name, section, date


def collect_headers(files: List[Path]) -> Dict[Tuple[str, int], Tuple[int, str, str, Path]]:
    """收集所有文件的头部信息。
    返回 {(name_lower, section): (section, date, orig_name, src_path)}
    """
    db: Dict[Tuple[str, int], Tuple[int, str, str, Path]] = {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        name, section, date = parse_header(text)
        if not name:
            name = p.name.split(".")[0]
        if not section:
            section = section_from_suffix(p.name) or 1
        db[(name.lower(), section)] = (section, date, name, p)
    return db


# ============================================================
# 交叉引用数据库
# ============================================================

class CrossRefDB:
    """交叉引用数据库：(name_lower, section) → 输出文件名（如 man.1.md）。"""

    def __init__(self):
        # key: (name_lower, section), value: 输出文件名（不含路径，如 man.1.md）
        self.entries: Dict[Tuple[str, int], str] = {}

    def register(self, name: str, section: int, out_filename: str) -> None:
        self.entries[(name.lower(), section)] = out_filename

    def resolve(self, name: str, section: int, current_section: int) -> Optional[str]:
        """返回相对链接路径，或 None（不存在）。"""
        key = (name.lower(), section)
        if key not in self.entries:
            return None
        filename = self.entries[key]
        if section == current_section:
            return filename
        return f"../man{section}/{filename}"


# ============================================================
# mandoc 转换 + 后处理
# ============================================================

def run_mandoc(src_path: Path, out_path: Path) -> str:
    """调用 mandoc_convert.exe 转换单个文件，返回 markdown 文本。"""
    cmd = [str(MANDOC_EXE), str(src_path), str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=BUILD_DIR)
    if r.returncode != 0:
        log(f"  mandoc 警告/错误 ({src_path.name}): {r.stderr[:300]}")
    if not out_path.exists():
        raise RuntimeError(f"mandoc 未生成输出：{out_path}")
    return out_path.read_text(encoding="utf-8", errors="replace")


def clean_mandoc_escapes(text: str) -> str:
    """清理 mandoc -Tmarkdown 的转义字符和 HTML 实体。"""
    # \[ → [, \] → ]
    text = text.replace(r'\[', '[').replace(r'\]', ']')
    # &nbsp; → 空格
    text = text.replace('&nbsp;', ' ')
    # HTML 数字实体清理
    text = text.replace('&#160;', ' ')   # 不间断空格
    text = text.replace('&#45;', '-')    # 连字符
    text = text.replace('&#46;', '.')    # 句点
    text = text.replace('&#92;', '\\')   # 反斜杠
    text = text.replace('&#39;', "'")    # 撇号
    text = text.replace('&#96;', '`')    # 反引号
    text = text.replace('&#8220;', '"')  # 左双引号
    text = text.replace('&#8221;', '"')  # 右双引号
    text = text.replace('&#8216;', "'")  # 左单引号
    text = text.replace('&#8217;', "'")  # 右单引号
    text = text.replace('&#8203;', '')   # 零宽空格
    text = text.replace('&#8204;', '')   # 零宽不连字 (zwnj)
    text = text.replace('&zwnj;', '')    # 零宽不连字命名实体
    # 数学符号实体
    text = text.replace('&#8804;', '≤')  # 小于等于
    text = text.replace('&#8805;', '≥')  # 大于等于
    text = text.replace('&#8800;', '≠')  # 不等于
    text = text.replace('&#215;', '×')   # 乘号
    text = text.replace('&#247;', '÷')   # 除号
    text = text.replace('&#8594;', '→')  # 右箭头
    text = text.replace('&#8592;', '←')  # 左箭头
    text = text.replace('&#8593;', '↑')  # 上箭头
    text = text.replace('&#8595;', '↓')  # 下箭头
    # &gt; → >, &lt; → <, &amp; → &, &quot; → "
    text = text.replace('&gt;', '>').replace('&lt;', '<')
    text = text.replace('&amp;', '&').replace('&quot;', '"')
    # \_ → _ （转义下划线还原）
    text = text.replace(r'\_', '_')
    # \* → * （转义星号还原）
    text = text.replace(r'\*', '*')
    # \` → ` （转义反引号还原）
    text = text.replace(r'\`', '`')
    # 转义点号还原：4\.4BSD → 4.4BSD
    text = text.replace(r'\.', '.')
    # \-- → -- （转义连字符还原）
    text = text.replace(r'\--', '--')
    # \`code\` → `code` （去除包裹反引号代码的单引号）
    text = re.sub(r"'`([^`]+)`'", r'`\1`', text)
    # 反引号代码内的斜体标记去除：`sysctl *hw.machine_arch*` → `sysctl hw.machine_arch`
    def deitalicize_code(m: re.Match) -> str:
        inner = m.group(1)
        inner = re.sub(r'\*([^*]+)\*', r'\1', inner)
        return f'`{inner}`'
    text = re.sub(r'`([^`]+)`', deitalicize_code, text)
    # \\ → \ （转义反斜杠还原，但保留代码块内的）
    # 注意：不要在代码块内做这个替换
    return text


def strip_inline_markup(text: str) -> str:
    """去除内联 markdown 标记（用于 SYNOPSIS 代码块）。
    **bold** → bold, *italic* → italic, `code` → code
    """
    # `code` → code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # **bold** → bold
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    # *italic* → italic
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    # 去除行尾两个空格（markdown 硬换行）
    text = text.rstrip(' \t')
    return text


def is_synopsis_command_start(line: str, cmd_lower: str) -> bool:
    """判断一行是否是 SYNOPSIS 中的命令名开头（如 **man**）。"""
    # mandoc 输出 **man** 或 *man*
    stripped = line.strip()
    if stripped.startswith('**'):
        # 提取 **xxx** 的 xxx
        m = re.match(r'\*\*([^*]+)\*\*', stripped)
        if m and m.group(1).lower() == cmd_lower:
            return True
    if stripped.startswith('*'):
        m = re.match(r'\*([^*]+)\*', stripped)
        if m and m.group(1).lower() == cmd_lower:
            return True
    return False


def process_synopsis(lines: List[str], start_idx: int, display_name: str,
                     xref: CrossRefDB, section: int) -> Tuple[List[str], int]:
    """处理 SYNOPSIS 章节，返回 (处理后的行列表, 下一个未处理行的索引)。

    mandoc 原始输出特征：
    - .Bd -literal 块 → tab 缩进的纯文本行
    - .Bd -ragged + .Cd 块 → > 前缀的加粗行（如 > **device iflib**）
    - C 函数声明 → **#include** / *int* / **func**() 等加粗/斜体标记
    - 命令行 → **cmd** [-options] [args] 等加粗标记
    - 描述文字 → 纯文本行（如 "To compile this driver..."）
    """
    out: List[str] = []
    cmd_lower = display_name.lower()
    # 收集 SYNOPSIS 内的所有行（直到下一个 ## 标题）
    synopsis_lines: List[str] = []
    i = start_idx
    while i < len(lines):
        line = lines[i]
        if line.startswith('## ') or line.startswith('# '):
            break
        synopsis_lines.append(line)
        i += 1

    # 判断 SYNOPSIS 类型：
    # - C 函数声明型（man2/man3/man9）：含 #include 或函数声明（int/void/char * 等）
    # - 混合型（man4 驱动）：含 tab 缩进的 .Bd -literal 块 或 > 前缀的 .Cd 块
    # - 命令行型（man1/man5/man8）：以 **cmd** 开头
    is_c_synopsis = False
    has_literal_block = False  # 含 .Bd -literal 的 tab 缩进块
    has_cd_block = False       # 含 .Cd 的 > 前缀块
    for ln in synopsis_lines:
        s = ln.strip()
        plain = strip_inline_markup(s)
        plain = clean_mandoc_escapes(plain)
        plain = plain.replace('`', '').strip()
        if plain.startswith('#include'):
            is_c_synopsis = True
            break
        if re.match(r'^(int|void|char\s*\*?|size_t|ssize_t|long|unsigned|struct|enum|const|uint\w+|int\w+|u_\w+|u_int\w*|__\w+)\s', plain):
            is_c_synopsis = True
            break
        if plain.startswith('#') and not plain.startswith('#!'):
            is_c_synopsis = True
            break
        # 检测 tab 缩进的 .Bd -literal 块
        if ln.startswith('\t') or ln.startswith('        '):
            has_literal_block = True
        # 检测 > 前缀的 .Cd 块
        if ln.startswith('>'):
            has_cd_block = True

    if is_c_synopsis:
        # C 函数声明：用三反引号代码块，保持多行结构
        code_lines: List[str] = []
        for ln in synopsis_lines:
            stripped = ln.strip()
            if not stripped:
                continue
            cleaned = stripped
            cleaned = cleaned.replace(r'\*', '\x00STAR\x00')
            cleaned = cleaned.replace(r'\_', '_')
            cleaned = re.sub(r'\*\*([^*]+)\*\*', r'\1', cleaned)
            cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
            cleaned = re.sub(r'`([^`]+)`', r'\1', cleaned)
            cleaned = clean_mandoc_escapes(cleaned)
            cleaned = cleaned.replace('\x00STAR\x00', '*')
            cleaned = re.sub(r' {2,}', ' ', cleaned).strip()
            if cleaned:
                code_lines.append(cleaned)
        if code_lines:
            out.append('```c')
            out.extend(code_lines)
            out.append('```')
            out.append('')
    elif has_literal_block or has_cd_block:
        # 混合型（man4 驱动等）：保留结构，逐段处理
        # - 纯文本行 → 保留
        # - tab 缩进块（.Bd -literal）→ ```sh 代码围栏
        # - > 前缀块（.Cd in .Bd -ragged）→ 去除 > 和 **，转为 `device xxx`
        # - 命令行（**cmd** 开头）→ 单行反引号包裹
        out.extend(_process_mixed_synopsis(synopsis_lines, cmd_lower))
    else:
        # 命令行型：每个 **cmd** 开头为新命令行，合并为单行反引号
        commands: List[List[str]] = []
        current_cmd: List[str] = []
        for ln in synopsis_lines:
            stripped = ln.strip()
            if not stripped:
                if current_cmd:
                    commands.append(current_cmd)
                    current_cmd = []
                continue
            if is_synopsis_command_start(stripped, cmd_lower):
                if current_cmd:
                    commands.append(current_cmd)
                current_cmd = [stripped]
            else:
                if current_cmd:
                    current_cmd.append(stripped)
                else:
                    current_cmd = [stripped]
        if current_cmd:
            commands.append(current_cmd)

        for cmd_lines in commands:
            merged = ' '.join(cmd_lines)
            cleaned = strip_inline_markup(merged)
            cleaned = clean_mandoc_escapes(cleaned)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if cleaned:
                out.append(f'`{cleaned}`')
                out.append('')

    return out, i


def _process_mixed_synopsis(synopsis_lines: List[str], cmd_lower: str) -> List[str]:
    """处理混合型 SYNOPSIS（含 .Bd -literal / .Bd -ragged / .Cd 的 man4 驱动等）。

    返回处理后的行列表。逐行分析：
    - tab/8空格缩进连续行 → ```sh 代码围栏
    - > 前缀的 **xxx** 行（.Cd 宏）→ 去除 > 和 **，转为 `xxx`
    - 命令行（**cmd** 开头）→ 单行反引号包裹
    - 纯文本描述 → 保留原样
    """
    out: List[str] = []
    i = 0
    n = len(synopsis_lines)
    while i < n:
        line = synopsis_lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # tab 缩进的 .Bd -literal 块
        if line.startswith('\t') or line.startswith('        '):
            code_lines: List[str] = []
            while i < n:
                ln = synopsis_lines[i]
                if ln.startswith('\t') or ln.startswith('        '):
                    # 去除前导 tab/空格
                    content = ln.lstrip(' \t')
                    content = clean_mandoc_escapes(content)
                    code_lines.append(content)
                    i += 1
                elif ln.strip() == '':
                    # 空行可能是块内或块结束，看下一行
                    if i + 1 < n and (synopsis_lines[i + 1].startswith('\t') or
                                      synopsis_lines[i + 1].startswith('        ')):
                        code_lines.append('')
                        i += 1
                    else:
                        break
                else:
                    break
            if code_lines:
                out.append('```sh')
                out.extend(code_lines)
                out.append('```')
                out.append('')
            continue

        # > 前缀的 .Cd 块（.Bd -ragged）
        if stripped.startswith('>'):
            cd_lines: List[str] = []
            while i < n:
                ln = synopsis_lines[i]
                s = ln.strip()
                if s.startswith('>'):
                    # 去除 > 前缀
                    content = re.sub(r'^>\s?', '', s)
                    # 去除 ** 加粗标记
                    content = re.sub(r'\*\*([^*]+)\*\*', r'`\1`', content)
                    content = clean_mandoc_escapes(content)
                    if content:
                        cd_lines.append(content)
                    i += 1
                elif s == '':
                    # 空行可能是块结束
                    break
                else:
                    break
            if cd_lines:
                for cl in cd_lines:
                    out.append(cl)
                out.append('')
            continue

        # 命令行（**cmd** 开头）
        if is_synopsis_command_start(stripped, cmd_lower):
            cmd_parts: List[str] = [stripped]
            i += 1
            # 合并后续非空非特殊行
            while i < n:
                nl = synopsis_lines[i].strip()
                if not nl:
                    break
                if nl.startswith('>'):
                    break
                if synopsis_lines[i].startswith('\t') or synopsis_lines[i].startswith('        '):
                    break
                if is_synopsis_command_start(nl, cmd_lower):
                    break
                cmd_parts.append(nl)
                i += 1
            merged = ' '.join(cmd_parts)
            cleaned = strip_inline_markup(merged)
            cleaned = clean_mandoc_escapes(cleaned)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if cleaned:
                out.append(f'`{cleaned}`')
                out.append('')
            continue

        # 纯文本描述行：保留原样（合并连续文本行）
        text_parts: List[str] = [stripped]
        i += 1
        while i < n:
            nl = synopsis_lines[i].strip()
            if not nl:
                break
            if nl.startswith('>'):
                break
            if synopsis_lines[i].startswith('\t') or synopsis_lines[i].startswith('        '):
                break
            if is_synopsis_command_start(nl, cmd_lower):
                break
            text_parts.append(nl)
            i += 1
        merged_text = ' '.join(text_parts)
        merged_text = clean_mandoc_escapes(merged_text)
        merged_text = re.sub(r'\s+', ' ', merged_text).strip()
        if merged_text:
            out.append(merged_text)
            out.append('')

    return out


def post_process(md: str, display_name: str, section: int,
                 xref: CrossRefDB) -> str:
    """后处理 mandoc markdown 输出，使格式接近传统 man 渲染效果。

    处理步骤：
    1. 替换首行标题为 `# name(N)` 小写
    2. 降级标题层级（# → ##，## → ###）
    3. 清理转义字符（\\[, \\], &nbsp;, \\_, \\*）
    4. SYNOPSIS 章节合并为代码块（反引号包裹的命令行）
    5. 去除 > 引用块前缀（mandoc 用 > 包裹 .It 列表项内容）
    6. .Bd -literal 块（tab 缩进）转为 ```sh 代码围栏
    7. 合并被拆分的段落（mandoc 把每个内联宏单独成行）
    8. 路径斜体改为加粗（*/path* → **/path**）
    9. 交叉引用 name(N) 链接化
    10. 去除页脚行和残留页眉行
    11. 章节标题去除反引号包裹
    """
    lines = md.splitlines()
    out: List[str] = []
    in_code_block = False
    skipped_title = False
    skipped_footer = False
    current_section_header = ""  # 当前 ## 章节标题（大写）

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 代码块状态跟踪
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            out.append(line)
            i += 1
            continue

        # 首行标题：mandoc 输出 "MAN(1) - FreeBSD General Commands Manual"
        # 注意：mandoc 可能输出转义下划线如 "NG\_BTSOCKET(4)"
        if not skipped_title and not in_code_block:
            if re.match(r'^[A-Z][A-Z0-9._\\-]*\(\d+\)\s*-\s*FreeBSD', line):
                out.append(f"# {display_name.lower()}({section})")
                out.append("")
                skipped_title = True
                i += 1
                continue
            if i == 0:
                out.append(f"# {display_name.lower()}({section})")
                out.append("")
                skipped_title = True
                # 不 continue，继续处理这行

        # 残留页眉行：H1 后紧跟的 "XXX(N) - FreeBSD XXX Manual" 行
        if not in_code_block and skipped_title:
            if re.match(r'^[A-Z][A-Z0-9._\\-]*\(\d+\)\s*-\s*FreeBSD\s+\w+', line):
                i += 1
                continue

        # 页脚行：mandoc 输出 "W  - January 24, 2025 - MAN(1)" 或类似
        if not in_code_block and not skipped_footer:
            if re.match(r'^[A-Z]\s+-\s+\w+\s+\d+,?\s+\d+\s+-\s+[A-Z]', line):
                skipped_footer = True
                i += 1
                continue
            # .TH 格式页脚："\- - UNTITLED"
            if re.match(r'^\\?-\s+-\s+\w+', line):
                skipped_footer = True
                i += 1
                continue

        # 降级标题层级（仅在非代码块内）
        if not in_code_block and line.startswith("#"):
            # 检测 SYNOPSIS 章节（mandoc 输出 # SYNOPSIS，降级后为 ## SYNOPSIS）
            if re.match(r'^#\s+SYNOPSIS\s*$', line):
                current_section_header = "SYNOPSIS"
                out.append("## SYNOPSIS")
                out.append("")
                # 处理 SYNOPSIS 内容
                syn_out, next_i = process_synopsis(
                    lines, i + 1, display_name, xref, section)
                out.extend(syn_out)
                i = next_i
                continue
            # 其他标题：记录当前章节
            m = re.match(r'^(#+)\s+(.+)$', line)
            if m:
                hash_count = len(m.group(1))
                title = m.group(2)
                # 去除标题中的反引号包裹（mandoc 有时输出 # `BLUETOOTH_PROTO_HCI protocol`）
                title = re.sub(r'^`([^`]+)`$', r'\1', title)
                # 去除标题中的 ** 加粗标记
                title = re.sub(r'\*\*([^*]+)\*\*', r'\1', title)
                # 去除标题中的 * 斜体标记
                title = re.sub(r'\*([^*]+)\*', r'\1', title)
                current_section_header = title.upper()
                # 降级：增加一个 #
                line = "#" * (hash_count + 1) + " " + title
            else:
                line = "#" + line

        # 非代码块内：清理转义和格式
        if not in_code_block:
            # tab/8空格缩进行将转为代码块，跳过内联格式处理（避免损坏 C 代码指针/注释）
            if line.startswith('\t') or re.match(r'^ {8,}', line):
                out.append(line)
                i += 1
                continue
            # 清理转义字符
            line = clean_mandoc_escapes(line)
            # 去除 > 引用块前缀（mandoc 用 > 包裹 .It 列表项内容）
            # 可能有多层嵌套引用（> > text），循环去除所有 > 前缀
            while re.match(r'^>\s?', line):
                line = re.sub(r'^>\s?', '', line)
            # .Cd 宏输出 **device xxx** → `device xxx`（内核配置声明用反引号）
            line = re.sub(r'\*\*(device\s+\S+)\*\*', r'`\1`', line)
            # 路径斜体改加粗：*/path* → **/path**
            # 匹配 *...* 其中包含 / 的（路径），要求内容不含空格（避免误匹配运算符列表如 * / % ^*）
            line = re.sub(r'\*([^\*\s]*/[^\*\s]*)\*', r'**\1**', line)
            # 交叉引用链接化
            line = linkify_xref(line, section, xref)

        out.append(line)
        i += 1

    result = "\n".join(out)
    # 将 .Bd -literal 的 tab 缩进块转为 ```sh 代码围栏
    result = convert_literal_blocks(result)
    # 合并有序列表的断行续行：如 "1.\tFreeBSD\n\tGeneral Commands Manual"
    result = merge_list_continuations(result)
    # 合并被拆分的段落（mandoc 把每个内联宏单独成行）
    result = merge_broken_paragraphs(result)
    # 将连续的 **标签**\n\n描述 模式转换为无序列表（嵌套子选项）
    result = convert_tag_desc_to_list(result)
    # 修复 .Ns 宏产生的损坏输出：*** → ** ** 之间的分隔
    result = fix_ns_macro_damage(result)
    # 清理多余空行
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip() + "\n"


def convert_literal_blocks(text: str) -> str:
    """将 .Bd -literal 的 tab/8空格缩进块转为 ```sh 代码围栏。

    mandoc 把 .Bd -literal 块输出为 tab 缩进的纯文本行。
    本函数检测连续的 tab/8空格缩进行，包裹在 ```sh ... ``` 中。
    """
    lines = text.split('\n')
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 检测 tab 或 8+ 空格缩进的行（非代码围栏内）
        if (line.startswith('\t') or re.match(r'^ {8,}', line)) and line.strip():
            block: List[str] = []
            while i < n:
                ln = lines[i]
                if ln.startswith('\t') or re.match(r'^ {8,}', ln):
                    content = ln.lstrip(' \t')
                    # 清理 HTML 实体和转义字符（代码块内的 C 代码注释等）
                    content = clean_mandoc_escapes(content)
                    block.append(content)
                    i += 1
                elif ln.strip() == '':
                    # 空行：看下一行是否仍是缩进行
                    if i + 1 < n and (lines[i + 1].startswith('\t') or
                                      re.match(r'^ {8,}', lines[i + 1])):
                        block.append('')
                        i += 1
                    else:
                        break
                else:
                    break
            if block:
                out.append('```sh')
                out.extend(block)
                out.append('```')
                out.append('')
            continue
        out.append(line)
        i += 1
    return '\n'.join(out)


def fix_ns_macro_damage(text: str) -> str:
    """修复 .Ns 宏产生的损坏输出。

    mandoc 在处理 .Ns（无空格连接）时会产生 *** 系列损坏：
    - *rhost*****:*path* → *rhost*:**:** *path*（应为 rhost:path 或 **rhost**:**:** **path**）
    - **+ - ** / % ^*** → 损坏的运算符列表
    """
    # 修复 ***** 模式（多个连续星号）
    text = re.sub(r'\*{4,}', '**', text)
    # 修复 ** ** 之间的空连接：**word** **:** → **word****:**
    # 这种损坏较难完美修复，仅做最小修复
    return text


def merge_list_continuations(text: str) -> str:
    """合并有序/无序列表的断行续行。

    mandoc 输出：
        1.\tFreeBSD
        \tGeneral Commands Manual
    合并为：
        1. FreeBSD General Commands Manual
    """
    lines = text.split('\n')
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测有序列表项：N.\t 或 N.  开头
        m = re.match(r'^(\d+)\.\t(.+)$', line)
        if m:
            # 收集续行（以 \t 开头的行）
            parts = [m.group(2)]
            j = i + 1
            while j < len(lines) and lines[j].startswith('\t'):
                parts.append(lines[j].strip('\t'))
                j += 1
            out.append(f"{m.group(1)}. {' '.join(parts)}")
            i = j
            continue
        # 检测无序列表项续行（- 或 * 开头后的 \t 续行）
        m2 = re.match(r'^(-|\*)\t(.+)$', line)
        if m2:
            parts = [m2.group(2)]
            j = i + 1
            while j < len(lines) and lines[j].startswith('\t'):
                parts.append(lines[j].strip('\t'))
                j += 1
            out.append(f"{m2.group(1)} {' '.join(parts)}")
            i = j
            continue
        out.append(line)
        i += 1
    return '\n'.join(out)


def is_block_boundary(line: str, lines: Optional[List[str]] = None, idx: int = -1) -> bool:
    """判断一行是否是段落边界（不应合并的行）。
    lines/idx 用于判断标签列表项后是否跟空行，以及列表项上下文。
    """
    s = line.strip()
    if not s:
        return True
    # 标题
    if s.startswith('#'):
        return True
    # 代码块围栏
    if s.startswith('```'):
        return True
    # 列表项：检测 - 和 * 开头
    # 但 - 后跟数字（如 "- 1, inclusive."）可能是数学表达式，不是列表项
    # 仅当前一行为空或也是列表项时才视为边界
    list_match = re.match(r'^(-|\*)\s(.+)$', s)
    if list_match:
        content = list_match.group(2)
        # - 后跟数字和标点（如 "- 1, inclusive."）→ 可能是数学表达式
        if re.match(r'^\d+[,.]?\s', content) or re.match(r'^\d+,\s\w', content):
            # 检查前一行是否为空（段落开始）
            if lines is not None and idx > 0:
                prev_s = lines[idx - 1].strip()
                if prev_s:
                    # 前一行非空，不是段落开始 → 合并到前一段落
                    return False
            return True
        return True
    # 有序列表项：N. 开头
    if re.match(r'^\d+\.\s', s):
        return True
    # 缩进代码块（4+ 空格或 tab）
    if line.startswith('    ') or line.startswith('\t'):
        return True
    # 表格行
    if s.startswith('|'):
        return True
    # HTML 标签
    if s.startswith('<'):
        return True
    # 水平分隔线
    if re.match(r'^(-{3,}|\*{3,})$', s):
        return True
    # 标签列表项：**-K** 或 **--opt** 这种以 ** 开头且紧跟非字母数字
    # 也匹配 **word**() 这种函数名加粗（后跟括号）
    m = re.match(r'^\*\*([^*]+)\*\*(.*)$', s)
    if m:
        tag = m.group(1)
        rest = m.group(2).strip()
        # rest 全是标点（如 , . ; : () ()）→ 段落内联标记，不是边界
        if rest and re.match(r'^[.,;:!?()]+$', rest):
            return False
        # 标签后跟参数（如 **file** *path*）→ 边界
        if rest:
            return True
        # 单独的 **-X** 或 **word** 行：检查后是否跟空行
        # 后跟空行 → 独立标签列表项（边界）
        # 后跟非空行 → 段落中的内联标记（不边界，合并）
        if lines is not None and idx >= 0 and idx + 1 < len(lines):
            next_s = lines[idx + 1].strip()
            if next_s == '':
                return True
            return False
        # 无上下文信息，保守视为边界
        return True
    # 环境变量标签：`VAR` 单独成行
    # 只有当后跟空行时才是标签列表项（边界）
    # 段落中间的 `VAR` 单独成行（后无空行）不是边界
    m2 = re.match(r'^`([^`]+)`(.*)$', s)
    if m2:
        rest = m2.group(2).strip()
        if not rest:
            # 单独成行：检查下一行是否为空
            if lines is not None and idx >= 0 and idx + 1 < len(lines):
                next_s = lines[idx + 1].strip()
                if next_s == '':
                    return True
                return False
            # 无上下文信息，保守视为边界
            return True
    return False


def should_join_with_prev(line: str, prev_line: str) -> bool:
    """判断当前行是否应与前一行合并为同一段落。"""
    s = line.strip()
    p = prev_line.strip()
    if not s or not p:
        return False
    # 边界行不合并
    if is_block_boundary(line) or is_block_boundary(prev_line):
        return False
    return True


def merge_broken_paragraphs(text: str) -> str:
    """合并被 mandoc 拆分的段落。

    mandoc 把每个内联宏（.Nm, .Ar, .Fl 等）单独成行，
    需要将它们合并回完整段落。所有同一非空块内的行用空格连接。
    """
    lines = text.split('\n')
    out: List[str] = []
    para: List[str] = []  # 当前段落的行

    def flush_para():
        if not para:
            return
        # 所有行用空格连接
        parts = [ln.strip() for ln in para if ln.strip()]
        merged = ' '.join(parts)
        # 压缩多空格
        merged = re.sub(r' {2,}', ' ', merged)
        out.append(merged)
        para.clear()

    in_code = False
    for i, line in enumerate(lines):
        s = line.strip()
        # 代码块状态
        if s.startswith('```'):
            flush_para()
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        # 空行：段落结束
        if not s:
            flush_para()
            out.append('')
            continue
        # 边界行：段落结束，边界行单独输出
        if is_block_boundary(line, lines, i):
            flush_para()
            out.append(line)
            continue
        # 普通行：加入当前段落
        para.append(line)

    flush_para()
    return '\n'.join(out)


def convert_tag_desc_to_list(text: str) -> str:
    """将连续的 **标签**\n\n描述 模式转换为无序列表。

    mandoc 嵌套标签列表输出为：
        **e**

        eqn(1) (description)

        **p**

        pic(1) (description)

    转换为：
        - **e** eqn(1) (description)
        - **p** pic(1) (description)

    仅当连续出现 2 个以上 **标签**+描述 时才转换（避免误判段落中的加粗词）。
    """
    lines = text.split('\n')
    out: List[str] = []
    i = 0
    n = len(lines)

    # 匹配纯标签行：**xxx** 或 **-x** 或 **--xxx**，后无其他内容
    tag_re = re.compile(r'^\*\*([^*]+)\*\*$')

    while i < n:
        # 尝试检测连续的 标签+空行+描述+空行 模式
        # 仅转换不以 - 开头的短标签（子选项），保留 **-X** 顶级选项不变
        first_m = tag_re.match(lines[i].strip())
        if not first_m or first_m.group(1).startswith('-'):
            out.append(lines[i])
            i += 1
            continue

        # 先扫描连续的标签块（所有标签都不以 - 开头）
        tag_blocks: List[Tuple[str, str, int]] = []  # (tag, desc, start_line)
        j = i
        while j < n:
            m = tag_re.match(lines[j].strip())
            if not m:
                break
            tag = m.group(1)
            # 带 - 的选项标签不参与子选项列表
            if tag.startswith('-'):
                break
            # 查找描述：跳过空行，找下一个非空行（直到下一个标签或空行）
            k = j + 1
            # 跳过一个空行
            if k < n and lines[k].strip() == '':
                k += 1
            # 收集描述行（直到空行或标签）
            desc_parts: List[str] = []
            while k < n:
                ls = lines[k].strip()
                if ls == '':
                    break
                if tag_re.match(ls):
                    break
                desc_parts.append(ls)
                k += 1
            desc = ' '.join(desc_parts)
            if not desc:
                break
            tag_blocks.append((tag, desc, j))
            # 移动到描述结束位置
            j = k
            # 跳过空行到下一个标签
            while j < n and lines[j].strip() == '':
                j += 1

        # 只有连续 2 个以上标签+描述才转换为列表
        if len(tag_blocks) >= 2:
            for tag, desc, _ in tag_blocks:
                out.append(f"- **{tag}** {desc}")
            out.append("")
            i = j
            continue

        # 单个标签+描述：保持原样
        if tag_blocks:
            for tag, desc, _ in tag_blocks:
                out.append(f"**{tag}**")
                out.append("")
                out.append(desc)
                out.append("")
            i = j
            continue

        out.append(lines[i])
        i += 1

    return '\n'.join(out)


def linkify_xref(line: str, current_section: int, xref: CrossRefDB) -> str:
    """将行内的 name(N) 交叉引用转换为 markdown 链接。

    跳过已在链接 [](...  内的、在代码 ` 内的引用。
    """
    # name(N) 模式：name 为字母数字+._-，N 为数字+可选字母（如 3lua）
    pattern = re.compile(r'(?<![\w\[\(])(([A-Za-z][\w.-]*)\((\d[a-z]*)\))')

    def replace(m: re.Match) -> str:
        full = m.group(1)   # name(N)
        name = m.group(2)   # name
        sec_str = m.group(3)  # N
        # 解析章节号
        sm = re.match(r'(\d+)', sec_str)
        if not sm:
            return full
        sec = int(sm.group(1))
        link = xref.resolve(name, sec, current_section)
        if link:
            return f"[{full}]({link})"
        return full

    # 分段处理：跳过 `code` 和 [link](url) 区域
    result: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        # 跳过行内代码 `...`
        if line[i] == '`':
            j = line.find('`', i + 1)
            if j != -1:
                result.append(line[i:j + 1])
                i = j + 1
                continue
        # 跳过链接 [text](url)
        if line[i] == '[':
            j = line.find(']', i + 1)
            if j != -1 and j + 1 < n and line[j + 1] == '(':
                k = line.find(')', j + 2)
                if k != -1:
                    result.append(line[i:k + 1])
                    i = k + 1
                    continue
        # 找下一个 ` 或 [（从 i+1 开始，避免停在当前位置死循环）
        next_special = min(
            (p for p in [line.find('`', i + 1), line.find('[', i + 1)] if p != -1),
            default=n
        )
        chunk = line[i:next_special]
        result.append(pattern.sub(replace, chunk))
        i = next_special
    return "".join(result)


# ============================================================
# .TH 格式转换（man 格式，非 mdoc）
# ============================================================

def is_th_format(text: str) -> bool:
    """检测文件是否为 .TH 格式（man 格式，非 mdoc）。

    mdoc 文件首行非注释行以 .Dt 或 .Dd 开头；
    .TH 文件首行非注释行以 .TH 开头。
    """
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('.\\"') or s.startswith("'\\\""):
            continue
        if s.startswith('.TH'):
            return True
        if s.startswith('.Dt') or s.startswith('.Dd'):
            return False
        # 其他行：可能是 .TH 格式的其他宏
        if s.startswith('.'):
            return True  # 默认视为 .TH 格式
        return False
    return False


def parse_th_line(line: str) -> Tuple[str, int, str]:
    """解析 .TH 行，返回 (title, section, date)。

    格式：.TH title section date source manual
    带引号：.TH "CLANG" "1" "2023-05-24" "16" "Clang"
    """
    # 去除 .TH 前缀
    rest = line[3:].strip()
    # 分割带引号的 token
    parts: List[str] = []
    i = 0
    n = len(rest)
    while i < n:
        # 跳过空格
        while i < n and rest[i] in ' \t':
            i += 1
        if i >= n:
            break
        if rest[i] == '"':
            # 引号字符串
            j = i + 1
            while j < n and rest[j] != '"':
                j += 1
            parts.append(rest[i + 1:j])
            i = j + 1
        else:
            # 非引号 token
            j = i
            while j < n and rest[j] not in ' \t':
                j += 1
            parts.append(rest[i:j])
            i = j
    title = parts[0] if parts else ""
    section = 0
    if len(parts) >= 2:
        m = re.match(r'(\d+)', parts[1])
        if m:
            section = int(m.group(1))
    date = parts[2] if len(parts) >= 3 else ""
    return title, section, date


def th_clean_escapes(text: str) -> str:
    """清理 .TH 格式的转义字符。"""
    # \- → -
    text = text.replace(r'\-', '-')
    # \\ → \
    text = text.replace(r'\\', '\\')
    # \% → 空字符串（可选连字符，输出中不可见）
    text = text.replace(r'\%', '')
    # \  → 空格（转义空格）
    text = text.replace('\\ ', ' ')
    # \| → 空字符串（细空格）
    text = text.replace(r'\|', '')
    # \^ → 空字符串（1/6 em 空格）
    text = text.replace(r'\^', '')
    # \0 → 空格（非断行空格）
    text = text.replace(r'\0', ' ')
    # \& → 空字符串（零宽占位符）
    text = text.replace(r'\&', '')
    # \(aq → '
    text = text.replace(r'\(aq', "'")
    # \(lq → "（左引号）
    text = text.replace(r'\(lq', '"')
    # \(rq → "（右引号）
    text = text.replace(r'\(rq', '"')
    # \(dq → "（双引号）
    text = text.replace(r'\(dq', '"')
    # \(em → —（破折号）
    text = text.replace(r'\(em', '—')
    # \(en → –（短破折号）
    text = text.replace(r'\(en', '–')
    # \(bu → •（项目符号）
    text = text.replace(r'\(bu', '•')
    # \(aa → ´（锐音符）
    text = text.replace(r'\(aa', '´')
    # \(ga → `（重音符）
    text = text.replace(r'\(ga', '`')
    # \*(Aq → '
    text = text.replace(r'\*(Aq', "'")
    # \*(C` → ''（Pod::Man 左引号，\f(CW 已提供反引号，此处冗余）
    text = text.replace(r'\*(C`', '')
    # \*(C' → ''（Pod::Man 右引号，\f(CW 已提供反引号，此处冗余）
    text = text.replace(r"\*(C'", '')
    # \*(XX — 其他通用字符串引用（跳过）
    text = re.sub(r'\\\*\([A-Z][A-Za-z]', '', text)
    # \m[blue] \m[] — 颜色标记（跳过）
    text = re.sub(r'\\m\[[^\]]*\]', '', text)
    # \s-2 \s+2 — 字号（跳过）
    text = re.sub(r'\\s[+-]?\d+', '', text)
    # \u \d — 上标/下标（跳过）
    text = text.replace(r'\u', '').replace(r'\d', '')
    # \w'...' — 宽度指令（跳过）
    text = re.sub(r"\\w'[^']*'", '', text)
    # \[charNN] — Unicode 字符引用（跳过）
    text = re.sub(r'\\\[[^\]]*\]', '', text)
    return text


def th_process_font_markup(text: str, state: Optional[Dict[str, str]] = None) -> str:
    """处理 .TH 格式的内联字体标记 \\fB \\fI \\fR \\fP \\f(CW。

    man 格式的字体切换是顺序的（非嵌套）：
    \\fB 设置当前字体为加粗
    \\fI 设置当前字体为斜体
    \\fR 设置当前字体为 roman（常规）
    \\fP 返回上一字体
    \\f(CW 设置当前字体为等宽（Constant Width）

    使用 ** 表示 bold，_ 表示 italic（避免 ** 与 * 混合产生 *** 歧义），
    ` 表示等宽字体。

    state: 可选的状态字典 {'current': 'R', 'prev': 'R'}，用于跨行状态传递。
           若传入则函数会更新状态，调用方负责在段落边界重置。
    """
    if state is None:
        current_font = 'R'
        prev_font = 'R'
    else:
        current_font = state.get('current', 'R')
        prev_font = state.get('prev', 'R')
    result: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '\\' and i + 1 < n and text[i + 1] == 'f' and i + 2 < n:
            # 单字符字体名：\\fB \\fI \\fR \\fP
            font = text[i + 2]
            if font in ('B', 'I', 'R', 'P'):
                new_font = font
                if font == 'P':
                    new_font = prev_font
                # 无操作：新字体与当前字体相同（如 \\fB 后紧跟 \\fB）
                if new_font == current_font:
                    i += 3
                    continue
                # 字体真正改变时更新 prev_font
                if font != 'P':
                    prev_font = current_font
                # 关闭当前字体标记
                if current_font == 'B':
                    result.append('**')
                elif current_font == 'I':
                    result.append('_')
                elif current_font == 'CW':
                    result.append('`')
                current_font = new_font
                # 开启新字体标记
                if current_font == 'B':
                    result.append('**')
                elif current_font == 'I':
                    result.append('_')
                i += 3
                continue
            # 双字符字体名：\\f(CW \\f(CR 等
            if i + 4 < n and text[i + 2] == '(' and text[i + 3:i + 5].isalpha():
                font_name = text[i + 3:i + 5]
                new_font = font_name
                if new_font == current_font:
                    i += 5
                    continue
                prev_font = current_font
                # 关闭当前字体标记
                if current_font == 'B':
                    result.append('**')
                elif current_font == 'I':
                    result.append('_')
                elif current_font == 'CW':
                    result.append('`')
                current_font = new_font
                # 开启新字体标记
                if current_font == 'CW':
                    result.append('`')
                elif current_font == 'B':
                    result.append('**')
                elif current_font == 'I':
                    result.append('_')
                i += 5
                continue
        result.append(text[i])
        i += 1
    # 更新状态（不关闭未闭合的标记，由调用方在段落边界处理）
    if state is not None:
        state['current'] = current_font
        state['prev'] = prev_font
    else:
        # 无状态模式（独立行处理）：关闭未闭合的标记
        if current_font == 'B':
            result.append('**')
        elif current_font == 'I':
            result.append('_')
        elif current_font == 'CW':
            result.append('`')
    return ''.join(result)


def th_strip_font_markup(text: str) -> str:
    """移除文本中的 \\fB \\fI \\fR \\fP \\f(CW 等字体标记，仅保留内容（用于代码块内）。"""
    text = re.sub(r'\\f[BIRP]', '', text)
    text = re.sub(r'\\f\([A-Z]{2}', '', text)
    return text


def th_split_macro_args(line: str) -> Tuple[str, List[str]]:
    """分割 .TH 宏行为 (宏名, [参数])。

    如 .BR \\-F <number> → ('BR', ['-F', '<number>'])
    如 .B ipf → ('B', ['ipf'])
    """
    # 去除前导 .
    rest = line[1:] if line.startswith('.') else line
    parts: List[str] = []
    i = 0
    n = len(rest)
    while i < n:
        while i < n and rest[i] in ' \t':
            i += 1
        if i >= n:
            break
        # 引号字符串
        if rest[i] == '"':
            j = i + 1
            while j < n and rest[j] != '"':
                j += 1
            parts.append(rest[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and rest[j] not in ' \t':
                j += 1
            parts.append(rest[i:j])
            i = j
    if not parts:
        return '', []
    return parts[0], parts[1:]


def th_format_alternating(macro: str, args: List[str]) -> str:
    """处理交替字体宏 .BR .BI .IR .RB .RI .IB 等。

    .BR a b c → **a** b **c**（B/R 交替）
    .BI a b → **a** _b_
    .IR a b → _a_ b
    """
    if not macro or len(macro) < 2:
        return ' '.join(args)
    fonts = list(macro)  # 如 'BR' → ['B', 'R']
    result: List[str] = []
    for idx, arg in enumerate(args):
        font = fonts[idx % len(fonts)]
        cleaned = th_clean_escapes(arg)
        if font == 'B':
            result.append(f'**{cleaned}**')
        elif font == 'I':
            result.append(f'_{cleaned}_')
        else:  # R
            result.append(cleaned)
    return ''.join(result)


# .TH 章节标题英文→中文映射（与 mdoc 相同）
TH_SECTION_MAP = {
    'NAME': '名称',
    'SYNOPSIS': '概要',
    'DESCRIPTION': '描述',
    'OPTIONS': '选项',
    'EXIT STATUS': '退出状态',
    'EXAMPLES': '实例',
    'SEE ALSO': '参见',
    'STANDARDS': '标准',
    'HISTORY': '历史',
    'AUTHORS': '作者',
    'BUGS': '缺陷',
    'CAVEATS': '注意事项',
    'DIAGNOSTICS': '诊断',
    'ERRORS': '错误',
    'ENVIRONMENT': '环境变量',
    'FILES': '文件',
    'LEGAL': '法律条款',
    'WARNING': '警告',
    'COMPILATION': '编译',
    'OVERVIEW': '概述',
    'LIBRARY': '库',
    'NOTES': '注意',
    'RETURN VALUE': '返回值',
    'COPYRIGHT': '版权',
}


def _strip_podman_preamble(lines: List[str]) -> List[str]:
    """去除 Pod::Man 自动生成的前导码（roff 宏定义块）。

    Pod::Man 生成的文件以 .de Sp/Vb/Ve/IX 等宏定义开头，
    这些宏定义在 markdown 转换中无意义，需要去除。
    返回去除前导码后的行列表。
    """
    # 找到第一个 .TH 行，之前的所有内容都是前导码
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('.TH'):
            return lines[i:]
    return lines


def convert_th_to_markdown(text: str, display_name: str, section: int,
                           xref: CrossRefDB) -> str:
    """将 .TH 格式 man 页面转换为 markdown。

    处理 .TH .SH .SS .B .I .BR .BI .IR .TP .PP .nf .fi 等宏，
    以及 \\fB \\fI \\fR \\fP 内联字体标记。

    使用段落缓冲区收集普通文本行，遇到边界时合并并统一处理字体标记，
    解决跨行 \\fB...\\fI...\\fB 标记的状态丢失问题。
    """
    raw_lines = text.splitlines()
    lines = _strip_podman_preamble(raw_lines)
    out: List[str] = []
    out.append(f"# {display_name.lower()}({section})")
    out.append("")

    # 解析 .TH 行获取日期
    date = ""
    in_code_block = False  # .nf/.fi 代码块
    in_synopsis = False    # SYNOPSIS 章节
    synopsis_lines: List[str] = []
    skipping_macro_def = False  # .de/.de1 宏定义块

    # .TP 标签处理
    pending_tp = False  # 下一行是 .TP 的标签
    pending_ip_desc = False  # .IP 标签已输出，等待描述

    # 当前章节
    current_section = ""

    # 段落缓冲区：收集普通文本行（仅 clean_escapes，未处理字体标记）
    # 遇到边界时 flush：合并为一行，统一处理字体标记
    para_buffer: List[str] = []

    def flush_para():
        """合并段落缓冲区，处理字体标记，输出。"""
        if not para_buffer:
            return
        merged = ' '.join(p.strip() for p in para_buffer if p.strip())
        merged = re.sub(r' {2,}', ' ', merged)
        if not merged:
            para_buffer.clear()
            return
        # 统一处理字体标记（跨行状态在此解决）
        content = th_process_font_markup(merged)
        # 交叉引用链接化
        content = linkify_xref(content, section, xref)
        # 如果前一行是列表项，合并为描述
        if out and out[-1].startswith('- ') and not out[-1].endswith('```'):
            out[-1] = out[-1] + ' ' + content
        else:
            out.append(content)
        para_buffer.clear()

    for line in lines:
        # 跳过注释行
        if line.startswith('.\\"') or line.startswith("'\\\"") or line.startswith("'\""):
            continue

        # 宏定义块跳过
        if re.match(r'^\.de1?\s', line):
            skipping_macro_def = True
            continue
        if skipping_macro_def:
            if line.strip() == '..':
                skipping_macro_def = False
            continue

        stripped = line.strip()

        # 空行
        if not stripped:
            if in_code_block:
                out.append("")
            elif in_synopsis:
                pass  # synopsis 内空行忽略
            else:
                flush_para()
                out.append("")
            continue

        # 跳过孤立的 . 行（rst2man 等生成的空宏行）
        if stripped == '.':
            continue

        # .TH 行
        if stripped.startswith('.TH'):
            _, _, date = parse_th_line(stripped)
            continue

        # 跳过的 roff 控制宏
        if re.match(r'^\.(nr|ds|ie|el|if|nh|ad|ft|in|ti|ta|ll|po|pl|ne|hy|IX|rs|re|HP|cw|ps|cs|rr|tm)\b', stripped) or \
           re.match(r'^\.(nr|ds|ie|el|if|nh|ad|ft|in|ti|ta|ll|po|pl|ne|hy|IX|rs|re|HP|cw|ps|cs|rr|tm)$', stripped):
            continue
        if stripped == '.DT':
            continue
        if stripped.startswith('.INDENT') or stripped.startswith('.UNINDENT'):
            continue
        # Pod::Man 宏：.Sp（垂直间距）、.PD（段落间距）
        if stripped == '.Sp' or stripped.startswith('.Sp '):
            if not in_code_block:
                flush_para()
                out.append("")
            continue
        if stripped == '.PD' or stripped.startswith('.PD '):
            continue
        if stripped == '.sp' or stripped == '.sp 1' or stripped.startswith('.sp '):
            if in_synopsis:
                synopsis_lines.append(line)
            elif not in_code_block:
                flush_para()
                out.append("")
            continue

        # .nf / .Vb — 开始代码块
        if stripped == '.nf' or stripped.startswith('.nf ') or stripped == '.Vb' or stripped.startswith('.Vb '):
            if in_synopsis:
                synopsis_lines.append(line)
            else:
                flush_para()
                in_code_block = True
                out.append("```sh")
            continue

        # .fi / .Ve — 结束代码块
        if stripped == '.fi' or stripped.startswith('.fi ') or stripped == '.Ve' or stripped.startswith('.Ve '):
            if in_code_block:
                out.append("```")
                out.append("")
                in_code_block = False
            if in_synopsis:
                synopsis_lines.append(line)
            continue

        # 代码块内：直接输出（清理转义 + 移除字体标记）
        if in_code_block:
            content = th_clean_escapes(line)
            content = th_strip_font_markup(content)
            out.append(content)
            continue

        # .PP / .LP — 段落分隔
        if stripped in ('.PP', '.LP', '.P', '.PP ') or stripped.startswith('.PP '):
            if in_synopsis:
                synopsis_lines.append(line)
            else:
                flush_para()
                out.append("")
            continue

        # .br — 行分隔
        if stripped == '.br':
            if in_synopsis:
                synopsis_lines.append(line)
            else:
                flush_para()
                out.append("")
            continue

        # .RS / .RE — 缩进（跳过，输出空行分隔）
        if stripped.startswith('.RS') or stripped.startswith('.RE'):
            if in_synopsis:
                synopsis_lines.append(line)
            else:
                flush_para()
                out.append("")
            continue

        # .SH — 章节标题
        if stripped.startswith('.SH'):
            # 如果在 SYNOPSIS 中，先输出收集的 synopsis
            if in_synopsis:
                in_synopsis = False
                if synopsis_lines:
                    syn_out = _th_format_synopsis(synopsis_lines, display_name, section, xref)
                    out.extend(syn_out)
                    synopsis_lines = []

            flush_para()
            # 提取章节名
            sec_name = stripped[3:].strip()
            # 去除引号
            sec_name = sec_name.strip('"').strip("'")
            # 清理转义
            sec_name = th_clean_escapes(sec_name)
            # 去除多余空格
            sec_name = re.sub(r'\s+', ' ', sec_name).strip()
            current_section = sec_name.upper()

            # SYNOPSIS 章节特殊处理
            if current_section == 'SYNOPSIS':
                in_synopsis = True
                synopsis_lines = []
                out.append("## SYNOPSIS")
                out.append("")
                continue

            # 翻译章节标题
            cn_name = TH_SECTION_MAP.get(current_section, sec_name)
            out.append(f"## {cn_name}")
            out.append("")
            continue

        # .SS — 子章节标题
        if stripped.startswith('.SS'):
            # 如果在 SYNOPSIS 中，不加 flush，将 .SS 加入 synopsis_lines
            # （jemalloc.3 等文件 SYNOPSIS 内含 .SS 子章节）
            if in_synopsis:
                synopsis_lines.append(line)
                continue

            flush_para()
            sub_name = stripped[3:].strip().strip('"').strip("'")
            sub_name = th_clean_escapes(sub_name)
            sub_name = re.sub(r'\s+', ' ', sub_name).strip()
            out.append(f"### {sub_name}")
            out.append("")
            continue

        # .TP — 标签段落（下一行是标签）
        if stripped == '.TP' or stripped.startswith('.TP '):
            # 如果在 SYNOPSIS 中，加入 synopsis_lines
            if in_synopsis:
                synopsis_lines.append(line)
                continue
            flush_para()
            pending_tp = True
            continue

        # .IP — 缩进段落
        if stripped.startswith('.IP'):
            # 如果在 SYNOPSIS 中，加入 synopsis_lines
            if in_synopsis:
                synopsis_lines.append(line)
                continue
            flush_para()
            # .IP 类似 .TP，但标签在同行的参数中
            _, args = th_split_macro_args(stripped)
            if args:
                tag = th_clean_escapes(args[0])
                tag = th_process_font_markup(tag)
                out.append(f"- {tag}")
                pending_ip_desc = True
            continue

        # SYNOPSIS 章节：收集所有行
        if in_synopsis:
            synopsis_lines.append(line)
            continue

        # .B / .I / .BR / .BI / .IR 等 — 字体宏
        m = re.match(r'^\.(B|I|BR|BI|IR|RB|RI|IB|SB|SM)\b\s*(.*)$', stripped)
        if m:
            macro = m.group(1)
            rest = m.group(2)
            flush_para()
            # 分割参数
            if macro in ('B', 'I', 'SB', 'SM'):
                # .B text → **text** 或 _text_
                content = th_clean_escapes(rest)
                content = th_process_font_markup(content)
                if macro in ('B', 'SB'):
                    formatted = f'**{content}**' if not content.startswith('**') else content
                else:  # I, SM
                    formatted = f'_{content}_' if not content.startswith('_') else content
            else:
                # .BR .BI .IR .RB .RI .IB — 交替字体
                _, args = th_split_macro_args(stripped)
                if args:
                    formatted = th_format_alternating(macro, args)
                else:
                    formatted = ''
            if pending_tp:
                # 这是 .TP 后的标签行
                out.append(f"- {formatted}")
                pending_tp = False
            else:
                out.append(formatted)
            continue

        # 普通文本行（含 \fB \fI 等内联标记）
        # 只做 clean_escapes，加入段落缓冲区，统一处理字体标记
        content = th_clean_escapes(line)

        if pending_tp:
            # 这是 .TP 后的标签行（普通文本）
            content_processed = th_process_font_markup(content)
            out.append(f"- {content_processed}")
            pending_tp = False
        else:
            # 加入段落缓冲区
            para_buffer.append(content)

    # flush 残留段落
    flush_para()

    # 处理 SYNOPSIS 章节末尾
    if in_synopsis and synopsis_lines:
        syn_out = _th_format_synopsis(synopsis_lines, display_name, section, xref)
        out.extend(syn_out)

    result = '\n'.join(out)
    # 合并被拆分的段落（连续非空非特殊行用空格连接）— 段落缓冲区已处理，但保留以防遗漏
    result = _th_merge_paragraphs(result)
    # 清理多余空行
    result = re.sub(r'\n{3,}', '\n\n', result)
    # 清理空 bold：** ** → 空（要求中间有空白，不匹配 **text**）
    result = re.sub(r'\*\*\s+\*\*', '', result)
    # 清理空 italic：_ _ → 空（要求中间有空白，不匹配 _text_）
    result = re.sub(r'(?<!_)_\s+_(?!_)', '', result)
    # 清理 **** 系列（重复 bold 开关）→ 空
    result = re.sub(r'\*{4,}', '', result)
    return result.strip() + '\n'


def _th_merge_paragraphs(text: str) -> str:
    """合并 .TH 转换输出中被拆分的段落。

    连续的普通文本行（非标题、非列表项、非代码块）合并为一个段落。
    """
    lines = text.split('\n')
    out: List[str] = []
    para: List[str] = []

    def flush():
        if para:
            merged = ' '.join(p.strip() for p in para if p.strip())
            merged = re.sub(r' {2,}', ' ', merged)
            if merged:
                out.append(merged)
            para.clear()

    in_code = False
    for line in lines:
        s = line.strip()
        if s.startswith('```'):
            flush()
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        if not s:
            flush()
            out.append('')
            continue
        # 边界行：不合并
        if s.startswith('#'):  # 标题
            flush()
            out.append(line)
            continue
        if s.startswith('- ') or s.startswith('* '):  # 列表项
            flush()
            out.append(line)
            continue
        if s.startswith('```'):  # 代码块
            flush()
            out.append(line)
            continue
        if s.startswith('|'):  # 表格
            flush()
            out.append(line)
            continue
        # 普通文本行：加入段落
        para.append(line)

    flush()
    return '\n'.join(out)


def _th_format_synopsis(lines: List[str], display_name: str, section: int,
                        xref: CrossRefDB) -> List[str]:
    """格式化 .TH 格式的 SYNOPSIS 章节。

    支持两种 SYNOPSIS 风格：
    1. 简单命令行（如 ipf.8）：.B cmd [.B -opt] [<arg>] → 合并为单行反引号
    2. 复杂函数签名（如 jemalloc.3）：含 .nf/.fi 代码块、.SS 子章节、.HP+.BI 函数签名
    """

    def _strip_markers(text: str) -> str:
        """移除 bold/italic 标记，保留纯文本。"""
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)
        return text
    out: List[str] = []
    # 先检测是否为复杂 SYNOPSIS（含 .nf/.fi/.Vb/.Ve 或 .HP 或 .SS）
    has_nf = any(l.strip().startswith('.nf') or l.strip().startswith('.Vb') for l in lines)
    has_hp = any(l.strip().startswith('.HP') for l in lines)
    has_ss = any(l.strip().startswith('.SS') for l in lines)
    is_complex = has_nf or has_hp or has_ss

    if not is_complex:
        # 简单命令行 SYNOPSIS：合并为单行反引号
        parts: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 处理宏行
            m = re.match(r'^\.(B|I|BR|BI|IR|RB|RI|IB|SB|SM)\b\s*(.*)$', stripped)
            if m:
                macro = m.group(1)
                rest = m.group(2)
                if macro in ('B', 'SB'):
                    content = th_clean_escapes(rest)
                    content = th_process_font_markup(content)
                    content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
                    content = re.sub(r'\*([^*]+)\*', r'\1', content)
                    parts.append(content)
                elif macro in ('I', 'SM'):
                    content = th_clean_escapes(rest)
                    content = th_process_font_markup(content)
                    content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
                    content = re.sub(r'\*([^*]+)\*', r'\1', content)
                    parts.append(content)
                else:
                    _, args = th_split_macro_args(stripped)
                    if args:
                        formatted = th_format_alternating(macro, args)
                        formatted = re.sub(r'\*\*([^*]+)\*\*', r'\1', formatted)
                        formatted = re.sub(r'\*([^*]+)\*', r'\1', formatted)
                        parts.append(formatted)
                continue
            # 跳过的宏
            if stripped.startswith('.nf') or stripped.startswith('.fi') or stripped.startswith('.Vb') or stripped.startswith('.Ve'):
                continue
            if stripped.startswith('.PP') or stripped == '.br' or stripped == '.sp':
                continue
            if stripped.startswith('.ft'):
                continue
            # 普通文本行
            content = th_clean_escapes(stripped)
            content = th_process_font_markup(content)
            content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
            content = re.sub(r'\*([^*]+)\*', r'\1', content)
            if content:
                parts.append(content)

        merged = ' '.join(parts)
        merged = re.sub(r'\s+', ' ', merged).strip()
        merged = re.sub(r'\[\s+', '[', merged)
        merged = re.sub(r'\s+\]', ']', merged)
        merged = re.sub(r'<\s+', '<', merged)
        merged = re.sub(r'\s+>', '>', merged)
        if merged:
            out.append(f'`{merged}`')
            out.append('')
        return out

    # 复杂 SYNOPSIS：逐行处理，保留结构
    # 收集函数签名到代码块，按 .SS 子章节分组
    in_nf = False
    nf_lines: List[str] = []
    sig_lines: List[str] = []  # 当前子章节的函数签名
    in_sig_block = False  # 是否正在收集函数签名

    def _flush_sig_block():
        """输出当前收集的函数签名代码块。"""
        nonlocal sig_lines, in_sig_block
        if not sig_lines:
            return
        out.append('```c')
        for sl in sig_lines:
            out.append(sl)
        out.append('```')
        out.append('')
        sig_lines.clear()
        in_sig_block = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # .nf / .Vb — 开始代码块
        if stripped == '.nf' or stripped.startswith('.nf ') or stripped == '.Vb' or stripped.startswith('.Vb '):
            _flush_sig_block()
            in_nf = True
            continue
        # .fi / .Ve — 结束代码块
        if stripped == '.fi' or stripped.startswith('.fi ') or stripped == '.Ve' or stripped.startswith('.Ve '):
            if in_nf and nf_lines:
                out.append('```c')
                for nl in nf_lines:
                    out.append(nl)
                out.append('```')
                out.append('')
                nf_lines.clear()
            in_nf = False
            continue
        # 代码块内
        if in_nf:
            content = th_clean_escapes(stripped)
            content = th_strip_font_markup(content)
            if content:
                nf_lines.append(content)
            continue

        # .SS — 子章节（先输出前面的代码块，再输出标题）
        if stripped.startswith('.SS'):
            _flush_sig_block()
            sub_name = stripped[3:].strip().strip('"').strip("'")
            sub_name = th_clean_escapes(sub_name)
            sub_name = re.sub(r'\s+', ' ', sub_name).strip()
            out.append(f'### {sub_name}')
            out.append('')
            continue

        # .HP — 悬挂缩进（函数签名前），跳过
        if stripped.startswith('.HP'):
            continue

        # .ft — 字体变化，跳过
        if stripped.startswith('.ft'):
            continue

        # .sp / .PP — 段落分隔，先输出代码块
        if stripped.startswith('.sp') or stripped.startswith('.PP'):
            _flush_sig_block()
            continue

        # .B / .I / .BR / .BI 等 — 字体宏（函数签名）
        m = re.match(r'^\.(B|I|BR|BI|IR|RB|RI|IB|SB|SM)\b\s*(.*)$', stripped)
        if m:
            macro = m.group(1)
            rest = m.group(2)
            in_sig_block = True
            if macro in ('B', 'SB', 'I', 'SM'):
                content = th_clean_escapes(rest)
            else:
                _, args = th_split_macro_args(stripped)
                if args:
                    # 代码块中不需要字体标记，直接清理转义连接
                    content = ''.join(th_clean_escapes(a) for a in args)
                else:
                    content = ''
            if content:
                sig_lines.append(content)
            continue

        # 普通文本行（如 const char *malloc_conf;）
        content = th_clean_escapes(stripped)
        if content:
            in_sig_block = True
            sig_lines.append(content)

    _flush_sig_block()
    return out


# ============================================================
# tbl 表格预处理器（.TS/.TE → markdown 表格）
# ============================================================

def _parse_tbl_column_spec(spec: str) -> str:
    """解析单个 tbl 列说明符，返回 markdown 对齐方式。

    l/la/n → :--- (左对齐)
    r     → ---: (右对齐)
    c     → :---: (居中)
    s     → :--- (span，等同左对齐)
    ^     → :--- (垂直展开，等同左对齐)
    b/B   → :--- (加粗，仅用于数据格式，不影响对齐)
    """
    # 提取基础对齐字符（忽略修饰符 b, B, w(), z, |, ||）
    spec = spec.strip()
    # 移除列修饰符
    spec = re.sub(r'[bB]', '', spec)  # 加粗
    spec = re.sub(r'w\(\d+[a-zA-Z]*\)', '', spec)  # 宽度
    spec = re.sub(r'[z|]', '', spec)  # 零宽、竖线
    base = spec.strip()
    if not base:
        return ':---'
    if base.startswith('r'):
        return '---:'
    if base.startswith('c'):
        return ':---:'
    # l, a, n, s, ^ 等
    return ':---'


def _preprocess_tbl_tables(text: str) -> Tuple[str, Dict[str, str]]:
    """预处理 mdoc 源文件中的 tbl 表格，转换为 markdown 表格。

    将 .TS/.TE 表格块替换为占位符标记，避免 mandoc markdown 后端崩溃。
    返回 (修改后的文本, {占位符: markdown表格})。

    支持的 tbl 特性：
    - 基本对齐：l, r, c, n, a
    - 修饰符：b, B（加粗）, w(N)（宽度）, z（零宽）
    - 选项：box, center, expand, allbox, tab()
    - 数据：T{...T} 多行文本块, _ 水平线, = 双水平线
    - 表格续行：.T&
    - 单元格内 roff 转义：\\fB, \\fI, \\&
    """
    lines = text.split('\n')
    result_lines: List[str] = []
    tables: Dict[str, str] = {}
    table_idx = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == '.TS' or stripped.startswith('.TS '):
            # 开始收集表格
            tbl_lines: List[str] = []
            i += 1
            while i < len(lines):
                l = lines[i]
                if l.strip() == '.TE' or l.strip().startswith('.TE '):
                    i += 1
                    break
                tbl_lines.append(l)
                i += 1

            # 转换表格
            md_table = _tbl_to_markdown(tbl_lines)
            placeholder = f'__TBL_{table_idx}__'
            tables[placeholder] = md_table
            result_lines.append(placeholder)
            table_idx += 1
        else:
            result_lines.append(line)
            i += 1

    return '\n'.join(result_lines), tables


def _merge_text_block(lines: List[str]) -> str:
    """合并 T{...T} 多行文本块为单行。

    将多行 T{...T} 文本块扁平化为单行 tab 分隔的单元格。
    T{ 开始一个单元格，T} 结束一个单元格，
    之间的所有文本（可跨多行）拼接为该单元格的内容。
    """
    if not lines:
        return ''
    result_cells: List[str] = []
    current_cell: List[str] = []
    in_cell = False

    for line in lines:
        cells = line.split('\t')
        for cell in cells:
            cell = cell.strip()
            if cell == 'T{':
                in_cell = True
                current_cell = []
            elif cell == 'T}':
                if in_cell:
                    result_cells.append(' '.join(current_cell).strip())
                    current_cell = []
                    in_cell = False
                else:
                    result_cells.append('')
            elif in_cell:
                if cell:
                    current_cell.append(cell)
            else:
                result_cells.append(cell)

    if in_cell:
        result_cells.append(' '.join(current_cell).strip())

    return '\t'.join(result_cells)


def _tbl_to_markdown(tbl_lines: List[str]) -> str:
    """将 tbl 表格行列表转换为 markdown 表格。"""
    # 解析选项和格式说明
    options: List[str] = []
    format_lines: List[str] = []
    data_lines: List[str] = []
    state = 'options'

    for line in tbl_lines:
        stripped = line.rstrip('\n')
        if state == 'options':
            if stripped.endswith(';'):
                options.append(stripped)
            else:
                state = 'format'
                format_lines.append(stripped)
        elif state == 'format':
            if stripped.strip().endswith('.'):
                # 格式结束
                format_lines.append(stripped)
                state = 'data'
            else:
                format_lines.append(stripped)
        elif state == 'data':
            data_lines.append(stripped)

    # 解析选项
    option_str = ' '.join(options)
    # 解析 tab() 分隔符
    tab_sep = '\t'
    tab_match = re.search(r'tab\(([^)]*)\)', option_str)
    if tab_match:
        tab_sep = tab_match.group(1)

    # 解析格式说明
    # tbl 支持多行格式：第一行是字体修饰符（b, B, i），第二行是对齐（l, r, c, n）
    # 使用最后一行（对齐行）来确定列数和对齐方式
    format_str = ' '.join(format_lines)
    # 去除末尾的 .
    if format_str.endswith('.'):
        format_str = format_str[:-1]

    # 如果有多个格式行（用空格连接后），取最后一行作为对齐行
    # 格式行之间可能有换行，已被 join 变为空格，但列说明符不含空格
    # 我们直接按空格分割所有格式说明符
    all_specs = format_str.split()
    # 如果有多行格式说明，最后一行是对齐行
    # 找出对齐行：从后往前找，直到找到第一个非 b/B/i/I 修饰符的说明符
    # 简化处理：取 all_specs 作为列说明符列表，但只取对齐相关的
    # 对于两行格式（如 "lB lB lB lB r l l l l"），后半部分是对齐
    # 计算列数：如果有 b/B/i/I 修饰符行，列数 = 后一半；否则全部
    # 简单启发式：如果说明符数量能被二整除且前半有 b/B/i/I 修饰符，则取后半
    if len(all_specs) % 2 == 0:
        half = len(all_specs) // 2
        first_half = all_specs[:half]
        second_half = all_specs[half:]
        has_modifiers = any('B' in s or 'b' in s or 'I' in s or 'i' in s for s in first_half)
        has_align = any(s.startswith(('l', 'r', 'c', 'n', 'a', 's', '^')) for s in second_half)
        if has_modifiers and has_align:
            col_specs = second_half
        else:
            col_specs = all_specs
    else:
        col_specs = all_specs
    # 计算实际列数（排除纯修饰符列如 |, ||, _, =）
    alignments: List[str] = []
    for cs in col_specs:
        if cs in ('|', '||', '_', '='):
            continue
        # 处理 s（span）和 ^（垂直展开）——不增加列数但需要对齐
        if cs.startswith('s') or cs.startswith('^'):
            continue
        alignments.append(_parse_tbl_column_spec(cs))

    if not alignments:
        return ''

    # 处理数据行（含 T{...T} 多行文本块）
    # T{...T} 是 per-cell 的文本块，不是 per-row
    # 将多行 T{...T} 文本块合并为单行单元格
    merged_lines: List[str] = []
    in_text = False
    text_parts: List[str] = []
    for line in data_lines:
        stripped = line.strip()
        if stripped == '.T&':
            continue
        if not in_text:
            # 检查是否包含 T{（文本块开始）
            if 'T{' in line.split(tab_sep):
                in_text = True
                text_parts = [line]
            elif stripped in ('_', '='):
                merged_lines.append(stripped)  # 保留水平线
            else:
                merged_lines.append(line)
        else:
            # 文本块内遇到 _ 或 =：结束文本块，保留水平线
            if stripped in ('_', '='):
                in_text = False
                merged_lines.append(_merge_text_block(text_parts))
                merged_lines.append(stripped)
                text_parts = []
                continue
            text_parts.append(line)
            # 检查是否包含 T}（文本块结束）
            # 只有当行中有 T} 且没有 T{ 时才结束（避免跨 cell 的 T{...T}）
            cells_in_line = line.split(tab_sep)
            has_t_end = 'T}' in cells_in_line
            has_t_start = 'T{' in cells_in_line
            if has_t_end and not has_t_start:
                in_text = False
                merged_lines.append(_merge_text_block(text_parts))
                text_parts = []

    if in_text:
        # 未闭合的文本块
        merged_lines.append(_merge_text_block(text_parts))

    data_lines = merged_lines

    # 处理合并后的数据行，提取单元格和水平线
    rows: List[List[str]] = []
    raw_rows: List[str] = []
    for line in data_lines:
        stripped = line.strip()
        if stripped in ('_', '='):
            if rows:
                raw_rows.append('__SEP__')
            continue

        # 分割单元格
        cells = line.split(tab_sep)
        # 去除首尾空单元格（tbl 数据行可能以 tab 开头/结尾）
        while cells and cells[0] == '':
            cells.pop(0)
        while cells and cells[-1] == '':
            cells.pop()
        rows.append(cells)

    if not rows:
        return ''

    # 判断第一行是否为表头（如果第一行之后有 __SEP__）
    # 如果没有 __SEP__，将第一行作为表头（tbl 中第一行通常就是表头）
    has_header = False
    for sep_idx, raw in enumerate(raw_rows):
        if raw == '__SEP__':
            has_header = True
            break

    # 构建 markdown 表格
    md_lines: List[str] = []

    # 确定表头行和数据行
    if has_header and len(rows) >= 2:
        header_row = rows[0]
        data_rows = rows[1:]
    elif len(rows) >= 1:
        # 没有显式分隔符：将第一行作为表头
        header_row = rows[0]
        data_rows = rows[1:]
    else:
        header_row = None
        data_rows = rows

    # 清理单元格内容
    def clean_cell(cell: str) -> str:
        """清理单元格内的 roff 转义。"""
        # 移除 \fB, \fI, \fR, \fP
        cell = re.sub(r'\\f[BIRP]', '', cell)
        # 移除 \f(XX
        cell = re.sub(r'\\f\([A-Z]{2}', '', cell)
        # 移除 \&
        cell = cell.replace('\\&', '')
        # 移除 \*(XX
        cell = re.sub(r'\\\*\([A-Z][A-Za-z]', '', cell)
        # 移除 \s+/-N
        cell = re.sub(r'\\s[+-]?\d', '', cell)
        # 移除 \sN
        cell = re.sub(r'\\s\d+', '', cell)
        # 移除 \h'...'
        cell = re.sub(r"\\h'[^']*'", '', cell)
        # 移除 \w'...'
        cell = re.sub(r"\\w'[^']*'", '', cell)
        # 移除 \|  细空格
        cell = cell.replace('\\|', '')
        # 移除 \^  1/6 em 空格
        cell = cell.replace('\\^', '')
        # 移除 \%  可选连字符
        cell = cell.replace('\\%', '')
        # 移除 \-  连字符
        cell = cell.replace('\\-', '-')
        # 移除 \(xx 特殊字符
        cell = re.sub(r'\\\([a-z]{2}', '', cell)
        # 移除 \e  转义反斜杠
        cell = cell.replace('\\e', '\\')
        # 移除 \~  不间断空格
        cell = cell.replace('\\~', ' ')
        # 移除 \0  数字宽度空格
        cell = cell.replace('\\0', ' ')
        # 清理多余空格
        cell = cell.strip()
        return cell

    # 构建表头
    if header_row:
        header_cells = [clean_cell(c) for c in header_row]
        # 确保列数匹配
        while len(header_cells) < len(alignments):
            header_cells.append('')
        header_cells = header_cells[:len(alignments)]
        md_lines.append('| ' + ' | '.join(header_cells) + ' |')
    else:
        # 无表头，生成空表头
        md_lines.append('| ' + ' | '.join([''] * len(alignments)) + ' |')

    # 分隔行
    md_lines.append('|' + '|'.join(alignments) + '|')

    # 数据行
    for row in data_rows:
        cells = [clean_cell(c) for c in row]
        while len(cells) < len(alignments):
            cells.append('')
        cells = cells[:len(alignments)]
        md_lines.append('| ' + ' | '.join(cells) + ' |')

    return '\n'.join(md_lines)


# ============================================================
# 转换：单文件
# ============================================================

def convert_one(src_path: Path, out_dir: Path, xref: CrossRefDB,
                alias_name: Optional[str] = None) -> Tuple[Path, str, int, str]:
    """转换单个 man 页面为 markdown（自动检测 mdoc/.TH 格式）。
    alias_name: 若为别名，用此名作为标题与输出文件名。
    返回 (输出路径, 显示名, 章节, 日期)。
    """
    text = src_path.read_text(encoding="utf-8", errors="replace")
    is_th = is_th_format(text)

    if is_th:
        # .TH 格式：用 Python 转换器
        _, sec_from_th, date = _th_parse_header(text)
        name, section, _ = parse_header(text)  # mdoc 解析器对 .TH 返回空
        if not name:
            # 从 .TH 行解析
            th_name = _th_parse_header(text)[0]
            name = th_name or src_path.name.split(".")[0]
        if not section:
            section = sec_from_th or section_from_suffix(src_path.name) or 1
        if not date:
            date = _th_parse_header(text)[2]
    else:
        # mdoc 格式
        name, section, date = parse_header(text)
        if not name:
            name = src_path.name.split(".")[0]
        if not section:
            section = section_from_suffix(src_path.name) or 1

    display_name = alias_name or name
    out_name = f"{safe_filename(display_name)}.{section}.md"
    out_path = out_dir / out_name

    if is_th:
        # .TH 格式转换
        processed = convert_th_to_markdown(text, display_name, section, xref)
        out_path.write_text(processed, encoding="utf-8")
    else:
        # mdoc 格式：mandoc + 后处理
        # 先预处理 tbl 表格（.TS/.TE → markdown 占位符）
        processed_text, tbl_tables = _preprocess_tbl_tables(text)
        if tbl_tables:
            # 有 tbl 表格，写预处理后的临时源文件
            tmp_src = out_dir / f".{out_name}.src.tmp"
            tmp_src.write_text(processed_text, encoding="utf-8")
            tmp_path = out_dir / f".{out_name}.tmp"
            run_mandoc(tmp_src, tmp_path)
            tmp_src.unlink(missing_ok=True)
        else:
            tmp_path = out_dir / f".{out_name}.tmp"
            run_mandoc(src_path, tmp_path)
        md = tmp_path.read_text(encoding="utf-8", errors="replace")
        processed = post_process(md, display_name, section, xref)
        # 替换 tbl 占位符
        for placeholder, md_table in tbl_tables.items():
            processed = processed.replace(placeholder, md_table)
        out_path.write_text(processed, encoding="utf-8")
        tmp_path.unlink(missing_ok=True)

    return out_path, display_name, section, date


def _th_parse_header(text: str) -> Tuple[str, int, str]:
    """从 .TH 格式文本解析标题、章节、日期。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('.TH'):
            return parse_th_line(s)
    return "", 0, ""


# ============================================================
# SUMMARY 生成
# ============================================================

def build_summary(entries: List[dict]) -> str:
    """生成 SUMMARY.md 内容。
    entries: [{section, name, rel_path, group?}]
    group: man2/man3 的子目录分组名
    """
    lines = ["# Table of contents", "", "* [man 页](README.md)", "* [目录](mu-lu.md)", ""]
    by_sec: Dict[int, List[dict]] = {}
    for e in entries:
        by_sec.setdefault(e["section"], []).append(e)

    for sec in sorted(by_sec.keys()):
        title = SECTION_TITLES.get(sec, f"man{sec}")
        lines.append(f"## {title}")
        lines.append("")
        items = sorted(by_sec[sec], key=lambda x: (x.get("group", ""), x["name"].lower()))
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

def run_cleaners_check() -> None:
    """运行 AutoCorrect 和 md-padding 的差异检查（不修改文件）。
    输出差异报告到 script/ 下供人工逐条复核。
    """
    report_dir = ROOT / "script"
    target_dirs = [EN_DIR / f"man{n}" for n in range(1, 10)
                   if (EN_DIR / f"man{n}").exists()]
    targets = " ".join(f'"{d}"' for d in target_dirs)
    # AutoCorrect
    ac_report = report_dir / "autocorrect_report.txt"
    try:
        r = subprocess.run(
            f'autocorrect --lint {targets}',
            shell=True, capture_output=True, text=True, cwd=ROOT
        )
        ac_report.write_text(r.stdout + r.stderr, encoding="utf-8")
        log(f"AutoCorrect 报告：{ac_report}")
    except FileNotFoundError:
        log("AutoCorrect 未安装，跳过（安装：cargo install autocorrect）")
    # md-padding
    mdp_report = report_dir / "mdpadding_report.txt"
    try:
        r = subprocess.run(
            f'md-padding --check {targets}',
            shell=True, capture_output=True, text=True, cwd=ROOT
        )
        mdp_report.write_text(r.stdout + r.stderr, encoding="utf-8")
        log(f"md-padding 报告：{mdp_report}")
    except FileNotFoundError:
        log("md-padding 未安装，跳过（安装：npm i -g md-padding@latest）")


# ============================================================
# 主入口：preview
# ============================================================

def cmd_preview(name: str) -> None:
    """预览模式：仅转换指定名称的 man 页面（如 man, cat, ls）。"""
    ensure_mandoc()

    # 尝试在 zip 中直接查找（避免全量解压）
    section = 1
    zip_path = None
    for sec in range(1, 10):
        p = find_man_in_zip(name, sec)
        if p:
            section = sec
            zip_path = p
            break

    if zip_path:
        log(f"在 zip 中找到：{zip_path}")
        # 仅解压该文件
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extract(zip_path, EN_DIR)
        src_path = EN_DIR / zip_path
    else:
        # 全量解压后扫描
        extract_zip()
        files = scan_man_files()
        src_path = None
        for p in files:
            stem = p.name.split(".")[0]
            if stem.lower() == name.lower():
                src_path = p
                section = section_from_suffix(p.name) or 1
                break
        if not src_path:
            log(f"未找到 {name} 的 man 页面")
            sys.exit(1)

    log(f"转换 {src_path.name} 预览...")

    # 构建 xref（预览模式：仅注册自身 + 常见页面）
    xref = CrossRefDB()
    out_filename = f"{safe_filename(name)}.{section}.md"
    xref.register(name, section, out_filename)

    out_dir = EN_DIR / f"man{section}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if section in (2, 3):
        readme = out_dir / "README.md"
        if not readme.exists():
            readme.write_text(f"# man{section}\n", encoding="utf-8")

    out_path, display_name, sec, date = convert_one(src_path, out_dir, xref)
    log(f"已生成：{out_path}")
    log(f"标题：{display_name}({sec})，日期：{date}")
    log("预览内容前 80 行：")
    print("\n".join(out_path.read_text(encoding="utf-8").splitlines()[:80]))


# ============================================================
# 主入口：all
# ============================================================

def cmd_all() -> None:
    """全量转换。"""
    ensure_mandoc()
    extract_zip()
    files = scan_man_files()
    log(f"发现 {len(files)} 个 man 页面源文件")
    aliases = parse_mlinks()
    log(f"发现 {len(aliases)} 个 MLINKS 别名")

    # 收集所有头部信息，构建交叉引用库
    headers = collect_headers(files)
    xref = CrossRefDB()
    for (nl, sec), (_, _, orig_name, _) in headers.items():
        xref.register(orig_name, sec, f"{safe_filename(orig_name)}.{sec}.md")
    # 注册别名
    for alias_file, main_file in aliases.items():
        am = re.match(r'^([^.\s]+)\.(\d+)', alias_file)
        mm = re.match(r'^([^.\s]+)\.(\d+)', main_file)
        if am and mm:
            alias_name = am.group(1)
            main_name = mm.group(1)
            sec = int(mm.group(2))
            xref.register(alias_name, sec, f"{safe_filename(alias_name)}.{sec}.md")

    summary_entries: List[dict] = []
    dates_data: Dict[int, List[Tuple[str, str]]] = {}
    alias_entries: List[Tuple[str, str, int]] = []

    # 转换主文件
    converted = 0
    for p in files:
        name, section, date = parse_header(
            p.read_text(encoding="utf-8", errors="replace"))
        if not name:
            name = p.name.split(".")[0]
        if not section:
            section = section_from_suffix(p.name) or 1
        out_dir = EN_DIR / f"man{section}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # man2/man3 子目录分组
        group = ""
        if section in (2, 3):
            rel = p.relative_to(SRC_DIR).as_posix()
            gm = re.match(r'lib/[^/]+/([^/]+)/', rel)
            if gm:
                group = gm.group(1)

        try:
            out_path, display_name, section, date = convert_one(p, out_dir, xref)
            converted += 1
            if converted % 50 == 0:
                log(f"  已转换 {converted}/{len(files)}...")
        except Exception as e:
            log(f"转换失败 {p}: {e}")
            continue

        rel_path = f"en/man{section}/{safe_filename(display_name)}.{section}.md"
        summary_entries.append({
            "section": section, "name": display_name, "rel_path": rel_path, "group": group
        })
        dates_data.setdefault(section, []).append((display_name, date))

    log(f"主文件转换完成：{converted}/{len(files)}")

    # 转换别名（重复内容，标题用别名）
    for alias_file, main_file in aliases.items():
        mm = re.match(r'^([^.\s]+)\.(\d+)', main_file)
        am = re.match(r'^([^.\s]+)\.(\d+)', alias_file)
        if not (mm and am):
            continue
        main_name = mm.group(1)
        sec = int(mm.group(2))
        # 找主文件源路径
        main_src = None
        key = (main_name.lower(), sec)
        if key in headers:
            main_src = headers[key][3]
        if not main_src:
            continue
        alias_name = am.group(1)
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

    log(f"别名转换完成：{len(alias_entries)} 个")

    # 生成 SUMMARY.md（根目录，指向 en/manN/）
    SUMMARY_FILE.write_text(build_summary(summary_entries), encoding="utf-8")
    log(f"已生成 {SUMMARY_FILE}（{len(summary_entries)} 条）")

    # 生成 en/SUMMARY.md（英文项目内部 TOC，路径不带 en/ 前缀）
    en_entries = []
    for e in summary_entries:
        en_entries.append({
            "section": e["section"], "name": e["name"],
            "rel_path": e["rel_path"].removeprefix("en/"), "group": e.get("group", "")
        })
    EN_SUMMARY_FILE.write_text(build_summary(en_entries), encoding="utf-8")
    log(f"已生成 {EN_SUMMARY_FILE}")

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
            for nm, dt in sorted(items):
                f.write(f"{nm}\t{dt}\n")
    log(f"已生成 {DATES_DIR}")

    # man2/man3 README.md
    for sec in (2, 3):
        readme = EN_DIR / f"man{sec}" / "README.md"
        if not readme.exists():
            readme.write_text(f"# man{sec}\n", encoding="utf-8")

    log("全部完成！")


# ============================================================
# 主入口：summary（仅重新生成 SUMMARY.md）
# ============================================================

def cmd_summary() -> None:
    """仅重新生成 SUMMARY.md（基于已有 en/manN/ 目录）。"""
    entries: List[dict] = []
    for sec in range(1, 10):
        d = EN_DIR / f"man{sec}"
        if not d.exists():
            continue
        for p in sorted(d.glob(f"*.{sec}.md")):
            if p.name == "README.md":
                continue
            name = p.stem.rsplit(".", 1)[0]
            rel = f"en/man{sec}/{p.name}"
            entries.append({"section": sec, "name": name, "rel_path": rel, "group": ""})
    SUMMARY_FILE.write_text(build_summary(entries), encoding="utf-8")
    log(f"已重新生成 {SUMMARY_FILE}（{len(entries)} 条）")


# ============================================================
# 主入口：clean
# ============================================================

def cmd_clean() -> None:
    """运行清理差异报告。"""
    run_cleaners_check()


# ============================================================
# main
# ============================================================

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
        extract_zip()
        files = scan_man_files()
        DATES_DIR.mkdir(parents=True, exist_ok=True)
        by_sec: Dict[int, List[Tuple[str, str]]] = {}
        for p in files:
            name, section, date = parse_header(
                p.read_text(encoding="utf-8", errors="replace"))
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
                mm = re.match(r'^([^.\s]+)\.(\d+)', main)
                am = re.match(r'^([^.\s]+)\.(\d+)', alias)
                if mm and am:
                    f.write(f"{am.group(1)}|{mm.group(1)}|{mm.group(2)}\n")
        log(f"已生成 {ALIASES_FILE}（{len(aliases)} 条）")
    else:
        print(f"未知命令：{cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
