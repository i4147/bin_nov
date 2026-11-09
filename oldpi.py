#!/data/data/com.termux/files/usr/bin/env python
"""Detect and fix the Python 2 ``print`` statements.
Finds the ``print x`` lines with a regex confirmed by tokenize (skipping the Rich ``print`` imports and the commented-out code), reports them and with ``-a`` rewrites them as ``print(x)`` calls; large files are read through an mmap.
Usage: oldpi [path] [-a] Positional arguments: path Path to file or directory to scan Options: -a, --autofix Automatically fix print statements Examples: $ oldpi legacy/ -a Command-line script: run ``python oldpi.py`` or the ``oldpi`` command.
The module is importable as well."""

from collections import deque
import mmap
from mmap import mmap
from pathlib import Path
import re
import tokenize

from dh import SKIP_DIRS, mpf


def get_files(path: str | Path, ext: list[str] | None = None) -> list[Path]:
    path = Path(path)
    queue = deque([path])
    files = []
    while queue:
        current = queue.popleft()
        try:
            entries = current.iterdir()
        except (PermissionError, OSError):
            continue
        for item in entries:
            if item.is_symlink():
                continue
            if item.is_dir() and item.name not in SKIP_DIRS:
                queue.append(item)
            elif item.is_file() and (ext is None or item.suffix in ext):
                files.append(item)
    return files


SIZE_THRESHOLD: int = 1 * 1024 * 1024
OLD_PRINT_RE = re.compile(r"(?m)^[ \t]*print[ \t]+[^(\n]")


def _open_source(path: str):
    size = Path(path).stat().st_size
    f = Path(path).open("rb")
    if size > SIZE_THRESHOLD:
        return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    return f


def _read_text(path: str) -> str | None:
    try:
        with Path(path).open(encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return None


def _has_rich_print_import(text: str) -> bool:
    return "from rich import print" in text


def _is_in_commented_code(text: str, line_start: int) -> bool:
    lines = text.splitlines(True)
    if line_start >= len(lines):
        return False
    in_multiline = False
    multiline_delimiter = None
    for i, line in enumerate(lines):
        if i == line_start:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                return True
            break
        for quote in ['"""', "'''"]:
            pos = line.find(quote)
            if pos != -1:
                if in_multiline and multiline_delimiter == quote:
                    in_multiline = False
                    multiline_delimiter = None
                elif not in_multiline:
                    in_multiline = True
                    multiline_delimiter = quote
                break
    return in_multiline


def regex_flag(path: str) -> bool:
    text = _read_text(path)
    if not text:
        return False
    if _has_rich_print_import(text):
        return False
    return bool(OLD_PRINT_RE.search(text))


def tokenizer_confirm(path: str) -> tuple[str, int] | None:
    try:
        src = _open_source(path)
        tokens = list(tokenize.tokenize(src.readline))
    except Exception:
        return None
    text = _read_text(path)
    if not text:
        return None
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.NAME and tok.string == "print":
            line = tok.line.rstrip()
            if line.strip() == "print":
                continue
            line_num = tok.start[0]
            if _is_in_commented_code(text, line_num - 1):
                continue
            j = i + 1
            while j < len(tokens) and tokens[j].type in {
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            }:
                j += 1
            if j < len(tokens) and tokens[j].string != "(":
                if line.lstrip().startswith("#"):
                    continue
                return (line, line_num)
    return None


def autofix_file(path: str) -> bool:
    try:
        with Path(path).open(encoding="utf-8") as f:
            lines = f.readlines()
        if any(l.strip() == "from rich import print" for l in lines):
            return False
        changed = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if '"""' in stripped or "'''" in stripped:
                continue
            if stripped.rstrip() == "print":
                continue
            if stripped.startswith("print ") and (not stripped.startswith("print(")):
                indent = line[: len(line) - len(stripped)]
                content = stripped[len("print ") :].rstrip()
                lines[i] = f"{indent}print({content})\n"
                changed = True
        if changed:
            with Path(path).open("w", encoding="utf-8") as f:
                f.writelines(lines)
        return changed
    except Exception:
        return False


def process_file(path: str, autofix: bool = False) -> str | None:
    if not regex_flag(path):
        return None
    confirmed = tokenizer_confirm(path)
    if not confirmed:
        return None
    line, line_num = confirmed
    if autofix:
        if autofix_file(path):
            return f"{path} (fixed)\n  Line {line_num}: {line}"
        else:
            return f"{path} (could not fix)\n  Line {line_num}: {line}"
    else:
        return f"{path}\n  Line {line_num}: {line}"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Detect and fix Python 2 print statements")
    parser.add_argument("path", nargs="?", default=".", help="Path to file or directory to scan")
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically fix print statements",
    )
    args = parser.parse_args()
    path = Path(args.path)
    if path.is_file() and path.suffix == ".py":
        files = [path]
    else:
        files = get_files(path, ext=[".py"])
    if not files:
        print("No Python files found.")
        return
    results = mpf(process_file, files)
    found_issues = False
    for result in results:
        if result:
            print(result)
            found_issues = True
    if not found_issues:
        print("No Python 2 print statements found.")
    elif args.autofix:
        print("\n✓ Files with issues have been automatically fixed.")
    else:
        print("\nRun with --autofix to automatically fix these issues.")


if __name__ == "__main__":
    raise SystemExit(main())
