#!/data/data/com.termux/files/usr/bin/env python
"""A small ``gdu``-style disk-usage tree view.
Scans the root directory, prints the sizes as a tree with human-readable units, and supports ``--max-depth``, ``--exclude`` globs and ``--follow-symlinks``.
Usage: gdu [PATH] [-d DEPTH] [-e GLOB] [-L] Positional arguments: path Root directory to scan (default: current folder).
Options: -d, --max-depth Limit how deep the tree is printed (e.g., -d 2).
-e, --exclude Glob pattern to ignore (can be used multiple times).
-L, --follow-symlinks Follow symbolic links (default: ignore).
Examples: $ gdu ~ -d 2 Command-line script: run ``python gdu.py`` or the ``gdu`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import fnmatch
import os
from pathlib import Path
import sys
from typing import Any, Iterable


def human_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:3.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}EB"


def scan_path(root: Path, exclude_patterns: Iterable[Any], follow_symlinks: Any) -> tuple[int,]:
    total_size = 0
    dir_sizes = defaultdict(int)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        cur_path = Path(dirpath)

        dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in exclude_patterns)]

        for fname in filenames:
            if any(fnmatch.fnmatch(fname, pat) for pat in exclude_patterns):
                continue
            fpath = cur_path / fname
            try:
                sz = fpath.stat().st_size
            except OSError:
                sz = 0
            total_size += sz
            ancestor = cur_path
            while True:
                dir_sizes[ancestor] += sz
                if ancestor == root:
                    break
                ancestor = ancestor.parent

    return dir_sizes, total_size


def print_tree(root: Path, dir_sizes: dict[str, Any], max_depth: int, prefix = "") -> None:
    if max_depth is not None and max_depth < 0:
        return

    size_str = human_size(dir_sizes.get(root, 0))
    print(f"{prefix}{root.name or root!s}  [{size_str}]")

    subdirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name.lower())

    for i, sub in enumerate(subdirs):
        is_last = i == len(subdirs) - 1
        child_prefix = prefix + ("└─ " if is_last else "├─ ")
        deeper_prefix = prefix + ("   " if is_last else "│  ")
        print_tree(sub, dir_sizes, None if max_depth is None else max_depth - 1, prefix=deeper_prefix)


def main() -> None:
    parser = argparse.ArgumentParser(description="A tiny, friendly 'gdu' clone written in Python.")
    parser.add_argument("path", nargs="?", default=".", help="Root directory to scan (default: current folder).")
    parser.add_argument(
        "-d", "--max-depth", type=int, default=None, help="Limit how deep the tree is printed (e.g., -d 2)."
    )
    parser.add_argument(
        "-e",
        "--exclude",
        action="append",
        default=[".git", "__pycache__"],
        metavar="PATTERN",
        help="Glob pattern to ignore (can be used multiple times).",
    )
    parser.add_argument("-L", "--follow-symlinks", action="store_true", help="Follow symbolic links (default: ignore).")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"❌ Oops! Path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    print(f"🔍 Scanning {root} …")
    dir_sizes, total = scan_path(root, args.exclude, args.follow_symlinks)
    print(f"✅ Scan complete! Total size: {human_size(total)}\n")

    print_tree(root, dir_sizes, args.max_depth)


if __name__ == "__main__":
    main()
