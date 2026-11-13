#!/data/data/com.termux/files/usr/bin/env python
"""Remove every ``from __future__ import ...`` statement from Python files.

The statement is deleted in all of its syntactic forms::

    from __future__ import annotations
    from __future__ import annotations, division
    from __future__ import annotations
    from __future__ import (
        annotations,
        division,
    )
    from __future__ import annotations  # noqa: F401
    from __future__ import annotations as ann

The now-redundant blank line that a removal can leave at the top of a
file is dropped as well.

Directories are walked via ``rignore``, so ``.gitignore`` rules are
honoured; ``SKIP_DIRS`` (from :mod:`dh`) is applied as a second filter.
Files are read and written as UTF-8; a leading BOM, if present, is
stripped on the first rewrite.

Usage::

    clean_future                 # clean the current directory
    clean_future src/ tests/     # clean multiple paths
    clean_future src/main.py     # clean a single file

The module can also be imported.  Requires Python 3.10+.
"""

import ast
import sys
from pathlib import Path
from typing import Iterator, Sequence

from atomicio import atomic_write
from dh import SKIP_DIRS, gsz
import rignore


# ---------------------------------------------------------------------------
# file discovery
# ---------------------------------------------------------------------------


def _has_python_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            first = f.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    low = first.lower()
    return b"python" in low or b"pypy" in low


def is_python_file(path: str | Path) -> bool:
    """Return True if *path* looks like a Python source file."""
    p = Path(path)
    if p.suffix in {".py", ".pyw", ".pyi"}:
        return True
    if p.suffix == "":
        return _has_python_shebang(p)
    return False


def walk_python_files(root_dir: str | Path) -> Iterator[Path]:
    """Yield Python files under *root_dir* (or *root_dir* itself if it is a file)."""
    root = Path(root_dir)

    # Explicit file argument: process it even if it is a symlink, because
    # the user asked for it by name.
    if root.is_file():
        if is_python_file(root):
            yield root
        return

    root = root.resolve()
    for entry in rignore.walk(root):
        path = Path(entry)
        if path.is_symlink():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if not path.is_file():
            continue
        if is_python_file(path):
            yield path


# ---------------------------------------------------------------------------
# core logic
# ---------------------------------------------------------------------------


def _parse(text: str) -> ast.Module:
    """Parse *text*, tolerating a UTF-8 BOM (for direct library use)."""
    try:
        return ast.parse(text)
    except SyntaxError:
        if text.startswith("\ufeff"):
            return ast.parse(text[1:])
        raise


def _future_import_line_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """Return 1-based inclusive ``(start, end)`` line ranges of __future__ imports."""
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            end = getattr(node, "end_lineno", None) or node.lineno
            ranges.append((node.lineno, end))
    ranges.sort()
    return ranges


def strip_future_imports(text: str) -> str:
    """Remove every ``from __future__ import ...`` statement from *text*.

    Raises :class:`SyntaxError` if *text* is not valid Python.
    """
    tree = _parse(text)
    ranges = _future_import_line_ranges(tree)
    if not ranges:
        return text

    # Split on "\n" only (not str.splitlines) so that our line numbering
    # matches the AST's, which counts only "\n" / "\r\n" as line breaks and
    # treats e.g. "\x0c" as plain whitespace.
    lines = text.split("\n")

    remove: set[int] = set()
    for start, end in ranges:
        remove.update(range(start - 1, end))  # 1-based -> 0-based

    new_lines = [ln for i, ln in enumerate(lines) if i not in remove]

    # If the file used to start with non-blank content and now starts with
    # blank lines, those blanks are the residue of the removed import
    # (and the blank line that followed it).  Drop them.
    if lines and lines[0].strip() and new_lines and not new_lines[0].strip():
        while new_lines and not new_lines[0].strip():
            new_lines.pop(0)

    return "\n".join(new_lines)


def process_file(path: Path) -> bool:
    """Strip future imports from *path*.  Return True if the file changed."""
    try:
        original = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"skip   {path}: {exc}", file=sys.stderr)
        return False

    try:
        updated = strip_future_imports(original)
    except SyntaxError as exc:
        print(f"skip   {path}: syntax error: {exc}", file=sys.stderr)
        return False

    if updated == original:
        return False

    try:
        atomic_write(path, updated)
    except Exception as exc:  # noqa: BLE001 - report anything that goes wrong
        print(f"error  {path}: {exc}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str]) -> int:
    roots: list[str] = list(argv[1:]) or ["."]
    scanned = changed = 0
    for root in roots:
        for path in walk_python_files(root):
            scanned += 1
            before = gsz(path)
            if process_file(path):
                changed += 1
                after = gsz(path)
                print(f"{path}    \x1b[38;5;10m{before - after}\x1b[0m")
    print(f"scanned {scanned} python files, modified {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
