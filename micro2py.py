#!/data/data/com.termux/files/usr/bin/env python
"""Refactor the MicroPython exports of ``.py`` files into standard Python.
Renames the ``u<module>`` imports (``uos``, ``ujson`` ...) to their CPython counterparts when the name is not shadowed by a local one, and offers ``--dry-run`` for a preview.
Usage: micro2py [paths ...] [-n] Positional arguments: paths ...
Files or directories to refactor (recursively).
Defaults to '.' if omitted.
Options: -n, --dry-run Only report what would be changed; touch nothing.
Examples: $ micro2py firmware/ -n Command-line script: run ``python micro2py.py`` or the ``micro2py`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
import sys
from typing import Any, Iterator


if sys.version_info >= (3, 10):
    _STDLIB: set[str] | None = set(sys.stdlib_module_names)
else:
    import importlib.util

    _STDLIB = None


def is_stdlib(name: str) -> bool:
    if _STDLIB is not None:
        return name in _STDLIB
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


_U_PREFIX_RE = re.compile(r"\bu([A-Za-z_][A-Za-z0-9_]*)\b")


def refactor_content(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        candidate = match.group(1)
        if is_stdlib(candidate):
            return candidate
        return match.group(0)

    return _U_PREFIX_RE.sub(repl, text)


def _rename_target(path: Path) -> Path | None:
    stem = path.stem
    if len(stem) > 1 and stem.startswith("u") and is_stdlib(stem[1:]):
        return path.with_name(stem[1:] + path.suffix)
    return None


def refactor_file(path: Path, dry_run: bool = False) -> tuple[bool, bool]:
    content_changed = False
    name_changed = False
    original = path.read_text(encoding="utf-8")
    updated = refactor_content(original)
    if updated != original:
        content_changed = True
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
    new_path = _rename_target(path)
    if new_path is not None:
        name_changed = True
        if not dry_run:
            if new_path.exists():
                print(f"  ! not renaming {path.name}: {new_path.name} already exists")
            else:
                path.rename(new_path)
    return content_changed, name_changed


def _iter_py_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py":
            yield root
    elif root.is_dir():
        yield from root.rglob("*.py")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Refactor MicroPython .py files to standard Python.",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path()],
        help="Files or directories to refactor (recursively). Defaults to '.' if omitted.",
    )
    ap.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Only report what would be changed; touch nothing.",
    )
    args = ap.parse_args(argv)
    roots = args.paths or [Path()]
    total = 0
    for root in roots:
        for f in _iter_py_files(root):
            content_changed, name_changed = refactor_file(f, dry_run=args.dry_run)
            if content_changed or name_changed:
                total += 1
                tags = []
                if content_changed:
                    tags.append("content")
                if name_changed:
                    tags.append("rename")
                print(f"{f}: {', '.join(tags)}")
    if total == 0:
        print("Nothing to do.")
    else:
        verb = "Would refactor" if args.dry_run else "Refactored"
        print(f"{verb} {total} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
