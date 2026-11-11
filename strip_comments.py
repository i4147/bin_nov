#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments from source files recursively (Python, shell, JS, TOML, Rust, Lua)."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import io
from multiprocessing import Pool
from pathlib import Path
import re
import tokenize
from typing import TYPE_CHECKING, Final

from dh import fsz
from loguru import logger


@dataclass(slots=True)
class FileResult:
    path: Path
    rel: str
    original_lines: int
    stripped_lines: int
    original_bytes: int
    stripped_bytes: int
    changed: bool
    error: str = ""

    @property
    def lines_removed(self) -> int:
        return self.original_lines - self.stripped_lines

    @property
    def bytes_saved(self) -> int:
        return self.original_bytes - self.stripped_bytes


RESET: Final[str] = "\x1b[0m"
BOLD: Final[str] = "\x1b[1m"
DIM: Final[str] = "\x1b[2m"
GREEN: Final[str] = "\x1b[32m"
YELLOW: Final[str] = "\x1b[33m"
CYAN: Final[str] = "\x1b[36m"
RED: Final[str] = "\x1b[31m"
BLUE: Final[str] = "\x1b[34m"
MAGENTA: Final[str] = "\x1b[35m"
WHITE: Final[str] = "\x1b[97m"


def _c(text: object, *codes: str) -> str:
    return "".join(codes) + str(text) + RESET


def strip_rust(source: str) -> str:
    result: list[str] = []
    i: int = 0
    n: int = len(source)
    in_string: bool = False
    in_raw_string: bool = False
    raw_hashes: int = 0
    in_char: bool = False
    while i < n:
        if not in_string and (not in_raw_string) and (not in_char):
            raw_m = re.match('r(#*)"', source[i:])
            if raw_m:
                raw_hashes = len(raw_m.group(1))
                end_pat = '"' + "#" * raw_hashes
                end_idx = source.find(end_pat, i + len(raw_m.group(0)))
                if end_idx == -1:
                    result.append(source[i:])
                    break
                end_idx += len(end_pat)
                result.append(source[i:end_idx])
                i = end_idx
                continue
        if not in_raw_string and (not in_char) and (source[i] == '"') and (not in_string):
            in_string = True
            result.append(source[i])
            i += 1
            continue
        if in_string:
            if source[i] == "\\" and i + 1 < n:
                result.append(source[i : i + 2])
                i += 2
                continue
            if source[i] == '"':
                in_string = False
            result.append(source[i])
            i += 1
            continue
        if not in_raw_string and source[i] == "'" and (not in_char):
            in_char = True
            result.append(source[i])
            i += 1
            continue
        if in_char:
            if source[i] == "\\" and i + 1 < n:
                result.append(source[i : i + 2])
                i += 2
                continue
            if source[i] == "'":
                in_char = False
            result.append(source[i])
            i += 1
            continue
        if source[i : i + 2] == "/*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if source[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif source[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            newlines = source[i:j].count("\n")
            result.append("\n" * newlines)
            i = j
            continue
        if source[i : i + 2] == "//":
            end = source.find("\n", i)
            if end == -1:
                i = n
            else:
                result.append("\n")
                i = end + 1
            continue
        result.append(source[i])
        i += 1
    return _collapse_blank_lines("".join(result))


def strip_toml(source: str) -> str:
    output: list[str] = []
    for raw_line in source.splitlines(keepends=True):
        output.append(_strip_hash_comment_from_line(raw_line))
    return _collapse_blank_lines("".join(output))


def _strip_hash_comment_from_line(line: str) -> str:
    result: list[str] = []
    in_sq: bool = False
    in_dq: bool = False
    i: int = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and (not in_dq):
            in_sq = not in_sq
            result.append(ch)
        elif ch == '"' and (not in_sq):
            in_dq = not in_dq
            result.append(ch)
        elif ch == "#" and (not in_sq) and (not in_dq):
            nl = "\n" if line.endswith("\n") else ""
            result.append(nl)
            return "".join(result)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def strip_js(source: str) -> str:
    result: list[str] = []
    i: int = 0
    n: int = len(source)
    while i < n:
        if source[i] == "`":
            end = i + 1
            while end < n:
                if source[end] == "\\":
                    end += 2
                elif source[end] == "`":
                    end += 1
                    break
                else:
                    end += 1
            result.append(source[i:end])
            i = end
            continue
        if source[i] in ('"', "'"):
            q = source[i]
            result.append(q)
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    result.append(source[i : i + 2])
                    i += 2
                elif source[i] == q:
                    result.append(q)
                    i += 1
                    break
                else:
                    result.append(source[i])
                    i += 1
            continue
        if source[i : i + 2] == "/*":
            j = source.find("*/", i + 2)
            if j == -1:
                newlines = source[i:].count("\n")
                result.append("\n" * newlines)
                break
            newlines = source[i : j + 2].count("\n")
            result.append("\n" * newlines)
            i = j + 2
            continue
        if source[i : i + 2] == "//":
            end = source.find("\n", i)
            if end == -1:
                break
            result.append("\n")
            i = end + 1
            continue
        result.append(source[i])
        i += 1
    return _collapse_blank_lines("".join(result))


_PRESERVE_COMMENT: Final[re.Pattern[str]] = re.compile(r"^\s*#\s*(type|fmt|noqa|pyright|pylint|mypy|ruff)\s*[:\s]")


def strip_python(source: str) -> str:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source
    result: list[str] = []
    prev_end: tuple[int, int] = (1, 0)
    module_docstring_done: bool = False
    first_string_seen: bool = False
    lines = source.splitlines(keepends=True)

    def text_between(start: tuple[int, int], end: tuple[int, int]) -> str:
        sl, sc = start
        el, ec = end
        if sl == el:
            return lines[sl - 1][sc:ec] if sl <= len(lines) else ""
        out: list[str] = [lines[sl - 1][sc:]]
        for ln in range(sl, el - 1):
            out.append(lines[ln] if ln < len(lines) else "")
        if el <= len(lines):
            out.append(lines[el - 1][:ec])
        return "".join(out)

    import token as token_mod

    for tok in tokens:
        ttype, ttext, tstart, tend, tline = tok
        gap = text_between(prev_end, tstart)
        result.append(gap)
        if ttype == tokenize.COMMENT:
            if ttext.startswith("#!") or _PRESERVE_COMMENT.match(ttext):
                result.append(ttext)
            prev_end = tend
            continue
        if ttype == token_mod.STRING:
            if not first_string_seen:
                first_string_seen = True
                if ttext.startswith(('"""', "'''", 'r"""', "r'''")):
                    module_docstring_done = True
                    result.append(ttext)
                    prev_end = tend
                    continue
            if module_docstring_done:
                stripped_line = tline.strip()
                is_docstring = stripped_line.startswith(ttext[:3]) if len(ttext) >= 3 else False
                if is_docstring and ttext.startswith(('"""', "'''", 'r"""', "r'''")):
                    prev_end = tend
                    continue
            result.append(ttext)
            prev_end = tend
            continue
        if ttype in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
            token_mod.ENDMARKER,
        ):
            result.append(ttext)
            prev_end = tend
            continue
        result.append(ttext)
        prev_end = tend
    stripped = "".join(result)
    return _collapse_blank_lines(stripped)


def strip_shell(source: str) -> str:
    output: list[str] = []
    for idx, raw_line in enumerate(source.splitlines(keepends=True)):
        if idx == 0 and raw_line.startswith("#!"):
            output.append(raw_line)
            continue
        output.append(_strip_shell_comment(raw_line))
    return _collapse_blank_lines("".join(output))


def _strip_shell_comment(line: str) -> str:
    result: list[str] = []
    in_sq: bool = False
    in_dq: bool = False
    i: int = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and (not in_dq):
            in_sq = not in_sq
            result.append(ch)
        elif ch == '"' and (not in_sq):
            in_dq = not in_dq
            result.append(ch)
        elif ch == "\\" and (not in_sq):
            result.append(line[i : i + 2])
            i += 2
            continue
        elif ch == "#" and (not in_sq) and (not in_dq):
            nl = "\n" if line.endswith("\n") else ""
            result.append(nl)
            return "".join(result)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def strip_lua(source: str) -> str:
    result: list[str] = []
    i: int = 0
    n: int = len(source)
    while i < n:
        ls_m = re.match(r"\[(?P<eq>=*)\[", source[i:])
        if ls_m and source[i] == "[":
            eq = ls_m.group("eq")
            close = "]" + eq + "]"
            end = source.find(close, i + len(ls_m.group(0)))
            if end == -1:
                result.append(source[i:])
                break
            end += len(close)
            result.append(source[i:end])
            i = end
            continue
        if source[i] in ('"', "'"):
            q = source[i]
            result.append(q)
            i += 1
            while i < n:
                if source[i] == "\\":
                    result.append(source[i : i + 2])
                    i += 2
                elif source[i] == q:
                    result.append(q)
                    i += 1
                    break
                else:
                    result.append(source[i])
                    i += 1
            continue
        if source[i : i + 2] == "--":
            lc_m = re.match(r"--\[(?P<eq>=*)\[", source[i:])
            if lc_m:
                eq = lc_m.group("eq")
                close = "]" + eq + "]"
                end = source.find(close, i + len(lc_m.group(0)))
                if end == -1:
                    newlines = source[i:].count("\n")
                    result.append("\n" * newlines)
                    break
                newlines = source[i : end + len(close)].count("\n")
                result.append("\n" * newlines)
                i = end + len(close)
                continue
            end = source.find("\n", i)
            if end == -1:
                break
            result.append("\n")
            i = end + 1
            continue
        result.append(source[i])
        i += 1
    return _collapse_blank_lines("".join(result))


def _collapse_blank_lines(text: str, max_consecutive: int = 1) -> str:
    pattern = re.compile("\\n{" + str(max_consecutive + 2) + ",}")
    return pattern.sub("\n" * (max_consecutive + 1), text)


_STRIPPER_MAP: Final[dict[str, Callable[[str], str]]] = {
    ".rs": strip_rust,
    ".toml": strip_toml,
    ".js": strip_js,
    ".ts": strip_js,
    ".jsx": strip_js,
    ".tsx": strip_js,
    ".mjs": strip_js,
    ".h": strip_js,
    ".hh": strip_js,
    ".hpp": strip_js,
    ".hxx": strip_js,
    ".cc": strip_js,
    ".cxx": strip_js,
    ".cpp": strip_js,
    ".c": strip_js,
    ".cjs": strip_js,
    ".py": strip_python,
    ".sh": strip_shell,
    ".bash": strip_shell,
    ".lua": strip_lua,
}


def process_file(args: tuple[Path, Path, set[str]]) -> FileResult:
    path, cwd, _active_exts = args
    rel = str(path.relative_to(cwd))
    ext = path.suffix.lower()
    try:
        original_bytes = path.stat().st_size
        source = path.read_text(encoding="utf-8", errors="replace")
        original_lines = source.count("\n")
        stripper = _STRIPPER_MAP[ext]
        stripped = stripper(source)
        stripped_lines = stripped.count("\n")
        stripped_bytes = len(stripped.encode("utf-8"))
        changed = stripped != source
        if changed:
            path.write_text(stripped, encoding="utf-8")
        return FileResult(
            path=path,
            rel=rel,
            original_lines=original_lines,
            stripped_lines=stripped_lines,
            original_bytes=original_bytes,
            stripped_bytes=stripped_bytes,
            changed=changed,
        )
    except Exception as exc:
        return FileResult(
            path=path,
            rel=rel,
            original_lines=0,
            stripped_lines=0,
            original_bytes=0,
            stripped_bytes=0,
            changed=False,
            error=str(exc),
        )


_EXT_ICON: Final[dict[str, str]] = {
    ".rs": "🦀",
    ".toml": "⚙️ ",
    ".js": "🟨",
    ".ts": "🔷",
    ".jsx": "⚛️ ",
    ".tsx": "⚛️ ",
    ".mjs": "🟨",
    ".cjs": "🟨",
    ".py": "🐍",
    ".sh": "🐚",
    ".bash": "🐚",
    ".lua": "🌙",
}


def print_header(active_exts: set[str]) -> None:
    exts_str = "  ".join(sorted(active_exts))
    logger.opt(colors=True).info("")
    logger.opt(colors=True).info(_c("  strip_comments ", BOLD, CYAN) + _c(f"targeting: {exts_str}", DIM))
    logger.opt(colors=True).info(_c("  " + "─" * 40, DIM))


def print_file_result(r: FileResult) -> None:
    icon = _EXT_ICON.get(Path(r.rel).suffix.lower(), "📄")
    if r.error:
        status = _c(" ERROR ", BOLD, RED)
        detail = _c(r.error, RED)
        logger.opt(colors=True).info(f"  {icon}  {_c(r.rel, BOLD)}  {status}  {detail}")
        return
    if r.changed:
        status = _c(" stripped ", BOLD, GREEN)
        lines_badge = (
            _c(f"-{r.lines_removed}", YELLOW) + _c(" lines", DIM) if r.lines_removed > 0 else _c("±0 lines", DIM)
        )
        bytes_badge = _c(f"-{fsz(r.bytes_saved)}", MAGENTA)
        logger.opt(colors=True).info(f"  {icon}  {_c(r.rel, WHITE)}  {status}  {lines_badge}  {bytes_badge}")
    else:
        status = _c(" clean  ", DIM)
        logger.opt(colors=True).info(f"  {icon}  {_c(r.rel, DIM)}  {status}")


def print_summary(results: list[FileResult], elapsed: float) -> None:
    total = len(results)
    changed = sum(1 for r in results if r.changed)
    clean = sum(1 for r in results if not r.changed and (not r.error))
    errors = sum(1 for r in results if r.error)
    lines_saved = sum(r.lines_removed for r in results)
    bytes_saved = sum(r.bytes_saved for r in results)
    logger.opt(colors=True).info("")
    logger.opt(colors=True).info(_c("  " + "─" * 40, DIM))
    msg = (
        f"  {_c('Summary', BOLD, CYAN)}  {_c(total, BOLD)} files  "
        f"{_c(changed, BOLD, GREEN)} stripped  {_c(clean, DIM)} clean  "
    )
    if errors:
        msg += f"{_c(errors, BOLD, RED)} errors  "
    msg += f"{_c(f'-{lines_saved} lines', YELLOW)}  {_c(f'-{fsz(bytes_saved)}', MAGENTA)}  {_c(f'{elapsed:.2f}s', DIM)}"
    logger.opt(colors=True).info(msg)
    logger.opt(colors=True).info("")


_FLAG_EXTS: Final[dict[str, list[str]]] = {
    "rs": [".rs"],
    "toml": [".toml"],
    "js": [".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"],
    "py": [".py"],
    "sh": [".sh", ".bash"],
    "lua": [".lua"],
}
_POOL_SIZE: Final[int] = 8


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Strip comments from source files recursively.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "dirs",
        nargs="*",
        default=["."],
        help="Directories to scan (default: current directory)",
    )
    for flag, exts in _FLAG_EXTS.items():
        p.add_argument(
            f"--{flag}",
            action="store_true",
            help=f"Strip comments from {', '.join(exts)} files",
        )
    p.add_argument("--all", action="store_true", help="Enable all language strippers")
    return p


def collect_files(dirs: list[str], active_exts: set[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        root = Path(d).resolve()
        if not root.is_dir():
            logger.opt(colors=True).warning(_c(f"  ⚠  Not a directory: {d}", YELLOW))
            continue
        for ext in active_exts:
            for fp in root.rglob(f"*{ext}"):
                if fp.is_file() and fp not in seen:
                    seen.add(fp)
                    files.append(fp)
    return sorted(files)


def main() -> int:
    import time

    parser = build_parser()
    args = parser.parse_args()
    active_exts: set[str] = set()
    if args.all:
        for exts in _FLAG_EXTS.values():
            active_exts.update(exts)
    else:
        for flag, exts in _FLAG_EXTS.items():
            if getattr(args, flag):
                active_exts.update(exts)
    if not active_exts:
        parser.error("No language flag specified. Use --rs, --toml, --js, --py, --sh, --lua, or --all.")
    cwd = Path.cwd()
    files = collect_files(args.dirs, active_exts)
    if not files:
        logger.opt(colors=True).info(_c("\n  No matching files found.\n", YELLOW))
        return 0
    print_header(active_exts)
    work: list[tuple[Path, Path, set[str]]] = [(fp, cwd, active_exts) for fp in files]
    results: list[FileResult] = []
    t0 = time.perf_counter()
    with Pool(processes=_POOL_SIZE) as pool:
        async_results = [pool.apply_async(process_file, (w,)) for w in work]
        for ar in async_results:
            result: FileResult = ar.get()
            print_file_result(result)
            results.append(result)
    elapsed = time.perf_counter() - t0
    results.sort(key=lambda r: r.rel)
    print_summary(results, elapsed)
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
