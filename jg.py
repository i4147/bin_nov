#!/data/data/com.termux/files/usr/bin/env python
"""Empty every repository below the current directory, keeping only its ``.git`` folder.
For each ``.git`` directory found it removes all sibling files and folders, leaving a clean checkout directory with the history in place.
This is destructive: run it from the tree you want to strip and keep a backup.
Usage: jg Examples: $ jg Irreversible — it deletes the working tree of every repository below the current directory."""

from pathlib import Path
import shutil


def process_dir(pardir: Path) -> bool:
    dotgit = pardir / ".git"
    if not dotgit.exists():
        return False
    for path in pardir.glob("*"):
        if path.is_dir() and path.name != ".git":
            shutil.rmtree(str(path))
        if path.is_file():
            path.unlink()
    return True


def find_targets(cwd: Path) -> None:
    for dpath in cwd.rglob("*"):
        if dpath.is_dir() and dpath.name == ".git":
            parent_of_dotgit = dpath.parent
            process_dir(parent_of_dotgit)


if __name__ == "__main__":
    cwd = Path.cwd()
    find_targets(cwd)
    for p in cwd.glob("*"):
        print(p.relative_to(cwd))
