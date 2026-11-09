#!/data/data/com.termux/files/usr/bin/env python
"""List the top-level directories of the current directory, optionally with their sizes.
Prints the subdirectories one per line and, with the size flag, the recursive total of each of them in a sorted, human-readable listing.
Usage: dirz [-s] Options: -s, --size Show total size per directory Examples: $ dirz -s Command-line script: run ``python dirz.py`` or the ``dirz`` command.
The module is importable as well."""

import argparse
import os
from pathlib import Path
from typing import Iterator, Optional


def format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return None


def walk_files(root: Path) -> Iterator[tuple[os.DirEntry, Optional[str]]]:
    stack: list[tuple[str, Optional[str]]] = [(str(root), None)]
    while stack:
        current_path, top_level = stack.pop()
        try:
            with os.scandir(current_path) as entries:
                for entry in entries:
                    if entry.name == ".git":
                        continue
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        yield entry, top_level
                    elif entry.is_dir(follow_symlinks=False):
                        child_top_level = top_level if top_level is not None else entry.name
                        stack.append((entry.path, child_top_level))
        except OSError:
            continue


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List top-level directories of the CWD, optionally with total sizes.",
    )
    parser.add_argument(
        "-s",
        "--size",
        action="store_true",
        help="show total size per directory",
    )
    return parser.parse_args(argv)


def print_directory_listing(
    dirs: list[str],
    sizes: dict[str, int],
    show_size: bool,
) -> None:
    size_strings: dict[str, str] = {}
    size_width = 0
    if show_size and dirs:
        size_strings = {name: format_size(sizes.get(name, 0)) for name in dirs}
        size_width = max(len(s) for s in size_strings.values())
    for dir_name in dirs:
        line = f"-{dir_name}"
        if show_size:
            line += f"  {size_strings[dir_name]:>{size_width}}"
        print(line)
    total_line = f"total:{len(dirs)} dirs"
    if show_size:
        total_line += f"  {format_size(sum(sizes.values()))}"


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    root = Path.cwd()
    dir_names: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name == ".git":
                    continue
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dir_names.append(entry.name)
    except OSError:
        pass
    dir_names.sort()
    dir_sizes: dict[str, int] = {}
    if args.size:
        for entry, top_level in walk_files(root):
            if top_level is None:
                continue
            try:
                file_size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            dir_sizes[top_level] = dir_sizes.get(top_level, 0) + file_size
    print_directory_listing(dir_names, dir_sizes, args.size)


if __name__ == "__main__":
    main()
