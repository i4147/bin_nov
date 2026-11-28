#!/data/data/com.termux/files/usr/bin/env python
"""Rename the current project: replace every occurrence of its directory name.

Usage:
    python script.py NEW_NAME

Replaces the old name (the current directory name) in:
    - text file contents (binary files are skipped)
    - file and folder names
    - the project directory itself, renamed last

Matching is case-insensitive and preserves UPPER / lower / Title casing.
"""

from __future__ import annotations
import os
from pathlib import Path
import re
import sys
from typing import Callable, Iterator

from dh import SKIP_DIRS, atomic_write


def build_replacer(old: str, new: str) -> tuple[re.Pattern, Callable[[re.Match], str]]:
    pattern = re.compile(re.escape(old), re.IGNORECASE)

    def _repl(match: re.Match) -> str:
        token = match.group(0)
        if token.isupper():
            return new.upper()
        if token.islower():
            return new.lower()
        if token.istitle():
            return new.title()
        return new

    return pattern, _repl


def iter_paths(root: Path) -> Iterator[Path]:
    """Yield every file and folder under root, pruning SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for name in dirnames:
            yield base / name
        for name in filenames:
            yield base / name


def replace_in_file(path: Path, pattern: re.Pattern, repl: Callable[[re.Match], str]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    new_text = pattern.sub(repl, text)
    if new_text == text:
        return False
    try:
        atomic_write(path, new_text, encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not update '{path}': {exc}", file=sys.stderr)
        return False
    return True


def validate_new_name(new: str, old: str, target: Path) -> str | None:
    if not new or new in {".", ".."}:
        return "new name is empty or reserved"
    if "/" in new or "\\" in new:
        return "new name must not contain path separators"
    if new == old:
        return "old and new names are identical"
    if target.exists():
        return f"{target} already exists"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} NEW_NAME", file=sys.stderr)
        return 1

    root = Path.cwd().resolve()
    old = root.name
    new = argv[1].strip()
    target = root.with_name(new)

    error = validate_new_name(new, old, target)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if len(old) < 3:
        print(
            f"warning: old name '{old}' is very short; many unrelated matches are likely",
            file=sys.stderr,
        )

    pattern, repl = build_replacer(old, new)
    paths = list(iter_paths(root))

    files_updated = 0
    for path in paths:
        if path.is_file() and not path.is_symlink() and replace_in_file(path, pattern, repl):
            files_updated += 1

    renamed: list[tuple[str, str]] = []
    failed = 0
    # Deepest first, so parents are still at their original path when children are renamed.
    for src in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        if not pattern.search(src.name):
            continue
        new_name = pattern.sub(repl, src.name)
        dst = src.with_name(new_name)
        if dst.exists():
            print(f"skipped: '{dst}' already exists, rename it manually", file=sys.stderr)
            failed += 1
            continue
        try:
            src.rename(dst)
            renamed.append((src.name, new_name))
        except OSError as exc:
            print(f"warning: could not rename '{src}': {exc}", file=sys.stderr)
            failed += 1

    try:
        root.rename(target)
    except OSError as exc:
        print(f"error: could not rename project directory: {exc}", file=sys.stderr)
        return 1

    print(f"Renamed project '{old}' -> '{new}'")
    print(f"  new path:              {target}")
    print(f"  files updated:         {files_updated}")
    print(f"  files/folders renamed: {len(renamed)}")
    if failed:
        print(f"  skipped/failed:        {failed}")
    for before, after in renamed:
        print(f"    {before}  ->  {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
