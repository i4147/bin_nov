#!/data/data/com.termux/files/usr/bin/env python
"""Recursively scan the current directory and remove: * .gitignore files * .github directories ...but ONLY when the folder containing them is NOT a git repository (i.e.
it has no .git entry next to them).
The user's home directory is exempt for .gitignore (~/.gitignore is preserved, since it's commonly used as a global gitignore).
Prints the total space freed at the end.
Usage: python clean_gitignore_and_github.py # delete python clean_gitignore_and_github.py --dry-run # preview only"""

import argparse
from pathlib import Path
import shutil
import sys
from typing import Iterator


def is_git_repo(folder: Path) -> bool:
    """A folder is a git repo if it holds a .git entry (dir, or file for worktrees/submodules)."""
    return (folder / ".git").exists()


def get_size(path: Path) -> int:
    """Return the total size in bytes of a file or directory tree."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PiB"


def iter_targets(folder: Path, home: Path) -> Iterator[Path]:
    """
    Generator-style walker yielding removal candidates one at a time.

    - Skips .git directories entirely (fast, and never touches git metadata).
    - Does not descend into a .github directory once it's marked for removal.
    - Skips ~/.gitignore (the user's home-level .gitignore).
    """
    try:
        entries = list(folder.iterdir())
    except OSError:
        return

    for entry in entries:
        # Never walk into git metadata
        if entry.name == ".git":
            continue

        if entry.is_dir():
            if entry.name == ".github" and not is_git_repo(entry.parent):
                yield entry
                continue  # pruned — don't look inside what we'll delete
            yield from iter_targets(entry, home)

        elif entry.is_file() and entry.name == ".gitignore":
            # Preserve ~/.gitignore
            try:
                if entry.parent.resolve() == home:
                    continue
            except OSError:
                pass
            if not is_git_repo(entry.parent):
                yield entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be removed without deleting anything",
    )
    args = parser.parse_args()

    root = Path.cwd().resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = Path.home()

    print(f"Scanning: {root}")
    print(f"Home (exempt from .gitignore removal): {home}\n")

    total_freed = 0
    removed_count = 0

    for target in iter_targets(root, home):
        if not target.exists():
            continue

        size = get_size(target)

        if args.dry_run:
            print(f"[would remove] {target}  ({human_size(size)})")
        else:
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                print(f"[skip] {target}: {exc}", file=sys.stderr)
                continue
            print(f"[removed] {target}  ({human_size(size)})")

        total_freed += size
        removed_count += 1

    verb = "Would remove" if args.dry_run else "Removed"
    print(f"\n{verb} {removed_count} item(s).")
    print(f"Space freed: {human_size(total_freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
