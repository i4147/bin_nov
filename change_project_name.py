#!/data/data/com.termux/files/usr/bin/env python
"""Rename a project by replacing one string with another in file names and contents.
Walks the current directory, replaces the old text in every readable file and then renames the matching files and directories (deepest paths first, so parents can be renamed too).
Existing destinations are reported instead of overwritten.
Usage: change_project_name OLD_TEXT NEW_TEXT Examples: $ change_project_name myproj newproj Runs as a script (``python change_project_name.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import shutil
import sys


def replace_in_file(path: Path, old: str, new: str) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (UnicodeDecodeError, PermissionError):
        return
    if old not in text:
        return
    new_text = text.replace(old, new)
    path.write_text(new_text, encoding="utf-8")


def rename_path(path: Path, old: str, new: str) -> Path:
    if old not in path.name:
        return path
    new_name = path.name.replace(old, new)
    new_path = path.parent / new_name
    if new_path.exists():
        print(f"path by name {new_path} already exists\n rename it manually")
        return path
    try:
        shutil.move(str(path), str(new_path))
        return new_path
    except Exception:
        return path


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <text_to_change> <replacement_text>")
        sys.exit(1)
    old = sys.argv[1]
    new = sys.argv[2]
    root = Path()
    for path in list(root.rglob("*")):
        if path.is_file():
            replace_in_file(path, old, new)
    paths = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        rename_path(path, old, new)


if __name__ == "__main__":
    raise SystemExit(main())
