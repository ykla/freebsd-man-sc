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
    # &#160; → 空格（不间断空格的数字实体）
    text = text.replace('&#160;', ' ')
    # &gt; → >, &lt; → <, &amp; → &, &quot; → "
    text = text.replace('&gt;', '>').replace('&lt;', '<')
    text = text.replace('&amp;', '&').replace('&quot;', '"')
    # \_ → _ （转义下划线还原）
    text = text.replace(r'\_', '_')
    # \* → * （转义星号还原）
    text = text.replace(r'\*', '*')
    # \` → ` （转义反引号还原）
    text = text.replace(r'\`', '`')
    # '`code`' → `code` （去除包裹反引号代码的单引号）
    text = re.sub(r"'`([^`]+)`'", r'`\1`', text)
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
    """处理 SYNOPSIS 章节，返回 (处理后的行列表, 下一个未处理行的索引)。"""
    out: List[str] = []
    cmd_lower = display_name.lower()
    # 收集 SYNOPSIS 内的所有非空行（直到下一个 ## 标题）
    synopsis_lines: List[str] = []
    i = start_idx
    while i < len(lines):
        line = lines[i]
        # 遇到下一个 ## 标题，结束
        if line.startswith('## ') or line.startswith('# '):
            break
        synopsis_lines.append(line)
        i += 1

    # 解析命令行：每个 **man** 开头为新命令行
    commands: List[List[str]] = []  # 每个元素是一个命令的所有行
    current_cmd: List[str] = []
    for ln in synopsis_lines:
        stripped = ln.strip()
        if not stripped:
            # 空行：如果当前有命令，结束当前命令
            if current_cmd:
                commands.append(current_cmd)
                current_cmd = []
            continue
        if is_synopsis_command_start(stripped, cmd_lower):
            # 新命令开始
            if current_cmd:
                commands.append(current_cmd)
            current_cmd = [stripped]
        else:
            if current_cmd:
                current_cmd.append(stripped)
            else:
                # SYNOPSIS 中命令名前的行（罕见），单独作为命令
                current_cmd = [stripped]
    if current_cmd:
        commands.append(current_cmd)

    # 每个命令合并为一行，用反引号包裹
    for cmd_lines in commands:
        merged = ' '.join(cmd_lines)
        # 清理 markdown 标记
        cleaned = strip_inline_markup(merged)
        # 清理转义
        cleaned = clean_mandoc_escapes(cleaned)
        # 清理多余空格
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned:
            out.append(f'`{cleaned}`')
            out.append('')

    return out, i


def post_process(md: str, display_name: str, section: int,
                 xref: CrossRefDB) -> str:
    """后处理 mandoc markdown 输出，使格式接近传统 man 渲染效果。

    处理步骤：
    1. 替换首行标题为 `# name(N)` 小写
    2. 降级标题层级（# → ##，## → ###）
    3. 清理转义字符（\\[, \\], &nbsp;, \\_, \\*）
    4. SYNOPSIS 章节合并为代码块（反引号包裹的命令行）
    5. 去除 > 引用块前缀（mandoc 用 > 包裹 .It 列表项内容）
    6. 合并被拆分的段落（mandoc 把每个内联宏单独成行）
    7. 路径斜体改为加粗（*/path* → **/path**）
    8. 交叉引用 name(N) 链接化
    9. 去除页脚行
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
        if not skipped_title and not in_code_block:
            if re.match(r'^[A-Z][A-Z0-9._-]*\(\d+\)\s*-\s*FreeBSD', line):
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

        # 页脚行：mandoc 输出 "W  - January 24, 2025 - MAN(1)" 或类似
        if not in_code_block and not skipped_footer:
            if re.match(r'^[A-Z]\s+-\s+\w+\s+\d+,?\s+\d+\s+-\s+[A-Z]', line):
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
            m = re.match(r'^#+\s+(.+)$', line)
            if m:
                current_section_header = m.group(1).upper()
            line = "#" + line

        # 非代码块内：清理转义和格式
        if not in_code_block:
            # 清理转义字符
            line = clean_mandoc_escapes(line)
            # 去除 > 引用块前缀（mandoc 用 > 包裹 .It 列表项内容）
            # 可能有多层嵌套引用（> > text），循环去除所有 > 前缀
            while re.match(r'^>\s?', line):
                line = re.sub(r'^>\s?', '', line)
            # 路径斜体改加粗：*/path* → **/path**
            # 匹配 *...* 其中包含 / 的（路径）
            line = re.sub(r'\*([^\*]*/[^\*]*)\*', r'**\1**', line)
            # 交叉引用链接化
            line = linkify_xref(line, section, xref)

        out.append(line)
        i += 1

    result = "\n".join(out)
    # 合并有序列表的断行续行：如 "1.\tFreeBSD\n\tGeneral Commands Manual"
    result = merge_list_continuations(result)
    # 合并被拆分的段落（mandoc 把每个内联宏单独成行）
    result = merge_broken_paragraphs(result)
    # 清理多余空行
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip() + "\n"


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
    lines/idx 用于判断标签列表项后是否跟空行。
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
    # 列表项
    if re.match(r'^(\d+\.|-|\*)\s', s):
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
    m = re.match(r'^\*\*([^*]+)\*\*(.*)$', s)
    if m:
        tag = m.group(1)
        rest = m.group(2).strip()
        # rest 全是标点（如 , . ; :)→ 段落内联标记，不是边界
        if rest and re.match(r'^[.,;:!?)]+$', rest):
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
# 转换：单文件
# ============================================================

def convert_one(src_path: Path, out_dir: Path, xref: CrossRefDB,
                alias_name: Optional[str] = None) -> Tuple[Path, str, int, str]:
    """转换单个 mdoc 文件为 markdown。
    alias_name: 若为别名，用此名作为标题与输出文件名。
    返回 (输出路径, 显示名, 章节, 日期)。
    """
    text = src_path.read_text(encoding="utf-8", errors="replace")
    name, section, date = parse_header(text)
    if not name:
        name = src_path.name.split(".")[0]
    if not section:
        section = section_from_suffix(src_path.name) or 1

    display_name = alias_name or name
    out_name = f"{safe_filename(display_name)}.{section}.md"
    out_path = out_dir / out_name
    tmp_path = out_dir / f".{out_name}.tmp"

    # 调用 mandoc 转换
    run_mandoc(src_path, tmp_path)
    md = tmp_path.read_text(encoding="utf-8", errors="replace")

    # 后处理
    processed = post_process(md, display_name, section, xref)
    out_path.write_text(processed, encoding="utf-8")

    # 清理临时文件
    tmp_path.unlink(missing_ok=True)
    return out_path, display_name, section, date


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
            convert_one(p, out_dir, xref)
            converted += 1
            if converted % 50 == 0:
                log(f"  已转换 {converted}/{len(files)}...")
        except Exception as e:
            log(f"转换失败 {p}: {e}")
            continue

        rel_path = f"en/man{section}/{safe_filename(name)}.{section}.md"
        summary_entries.append({
            "section": section, "name": name, "rel_path": rel_path, "group": group
        })
        dates_data.setdefault(section, []).append((name, date))

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
