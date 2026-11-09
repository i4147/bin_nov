#!/data/data/com.termux/files/usr/bin/env python
"""Folderize a whole tree by file extension and report the per-extension totals.
Walks the current directory recursively, moves every file into a top-level folder named after its lower-case extension (``no_extension`` when absent), removes the emptied directories and prints a table of file counts and sizes per extension.
Usage: foldext Examples: $ foldext Runs as a script (``python foldext.py``) in the directory it should act on; the module can also be imported."""

import contextlib
from pathlib import Path
import shutil
from typing import Any

from dh import gsz


def folderize_by_extension(cwd: Path) -> tuple[set[Any], dict[str, Any]]:
    root_path = Path(cwd)
    extension_stats = {}
    for path in root_path.rglob("*"):
        if path.name.startswith("."):
            continue
        if ".git" in path.parts:
            continue
        if path.is_file():
            ext = path.suffix.lower()[1:] if path.suffix else "no_extension"
            size = path.stat().st_size
            if ext not in extension_stats:
                extension_stats[ext] = {"count": 0, "total_size": 0, "files": []}
            extension_stats[ext]["count"] += 1
            extension_stats[ext]["total_size"] += size
            extension_stats[ext]["files"].append(path)
    created_dirs = set()
    for ext, stats in extension_stats.items():
        target_dir = root_path / ext
        target_dir.mkdir(exist_ok=True)
        created_dirs.add(ext)
        for path in stats["files"]:
            if path.parent == target_dir:
                continue
            target_path = target_dir / path.name
            counter = 1
            while target_path.exists():
                target_path = target_dir / f"{path.stem}_{counter}{path.suffix}"
                counter += 1
            with contextlib.suppress(BaseException):
                shutil.move(str(path), str(target_path))
    for dir_path in sorted(root_path.glob("**/*"), key=lambda p: len(p.parts), reverse=True):
        if dir_path.is_dir() and dir_path != root_path:
            with contextlib.suppress(OSError):
                dir_path.rmdir()
    total_files = 0
    total_size = 0
    for ext in sorted(extension_stats.keys()):
        stats = extension_stats[ext]
        total_files += stats["count"]
        total_size += stats["total_size"]
        ext_display = ext or "no_extension"
        size_str = gsz(stats["total_size"])
        print(f"{ext_display:<15} : {stats['count']:4} file{'s' if stats['count'] != 1 else ' '}  {size_str:>8}")
    print("-" * 40)
    print(f"{'TOTAL':<15} : {total_files:4} files  {gsz(total_size):>8}")
    print("=" * 40)
    return created_dirs, extension_stats


if __name__ == "__main__":
    target_dir = Path.cwd()
    created_dirs, stats = folderize_by_extension(target_dir)
    print(f"\nCreated {len(created_dirs)} extension folders.")
