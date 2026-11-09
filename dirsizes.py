#!/data/data/com.termux/files/usr/bin/env python
"""Find the files and directories with the largest footprint.
Walks a directory with a bounded depth, keeps the top K entries by recursive size (or by the individual file size with ``--mode file``), honours the ``--exclude`` patterns and prints a rich table, optionally reversed.
Usage: dirsizes [--dir DIR] [--topk K] [--mode folder|file] [--exclude NAME] Options: --dir, -d The directory to traverse recursively.
--topk, -k Display top K largest items (default: 10).
--exclude, -e Directories or files to ignore.
--wordy, -w Output permission warnings and skip notices.
--recur_depth, -r Maximum recursion depth (-1 for unlimited).
--mode, -m Mode: 'folder' footprint (non-recursive sum) or individual 'file' size.
--reverse Sort results from smallest to largest instead of largest to smallest.
Examples: $ dirsizes -d ~ -k 20 Command-line script: run ``python dirsizes.py`` or the ``dirsizes`` command.
The module is importable as well."""

import argparse
import heapq
import os
from pathlib import Path
import sys
import time
from typing import Generator

from rich.console import Console
from rich.status import Status


console = Console()


def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    size = float(size_bytes)
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find files/directories with the largest file footprint.")
    parser.add_argument(
        "--dir",
        "-d",
        type=Path,
        required=True,
        help="The directory to traverse recursively.",
    )
    parser.add_argument(
        "--topk",
        "-k",
        type=int,
        default=10,
        help="Display top K largest items (default: 10).",
    )
    parser.add_argument(
        "--exclude",
        "-e",
        type=Path,
        nargs="*",
        default=[],
        help="Directories or files to ignore.",
    )
    parser.add_argument(
        "--wordy",
        "-w",
        action="store_true",
        help="Output permission warnings and skip notices.",
    )
    parser.add_argument(
        "--recur_depth",
        "-r",
        type=int,
        default=-1,
        help="Maximum recursion depth (-1 for unlimited).",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["folder", "file"],
        default="folder",
        help="Mode: 'folder' footprint (non-recursive sum) or individual 'file' size.",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Sort results from smallest to largest instead of largest to smallest.",
    )
    return parser.parse_args()


def scan_directory(
    target_dir: Path,
    exclude_paths: set[Path],
    wordy: bool = False,
    max_depth: int = -1,
    current_depth: int = 0,
) -> Generator[tuple[Path, list[Path], list[tuple[Path, int]]], None, None]:
    if target_dir in exclude_paths:
        return
    subdirs: list[Path] = []
    files_with_sizes: list[tuple[Path, int]] = []
    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                if entry_path in exclude_paths:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry_path)
                    elif entry.is_file(follow_symlinks=False):
                        files_with_sizes.append((entry_path, entry.stat().st_size))
                except (PermissionError, OSError) as err:
                    if wordy:
                        console.print(f"[yellow]Warning:[/yellow] Cannot stat {entry_path}: {err}")
    except (PermissionError, OSError) as err:
        if wordy:
            console.print(f"[red]Error:[/red] Cannot read directory {target_dir}: {err}")
        return
    yield target_dir, subdirs, files_with_sizes
    if max_depth == -1 or current_depth < max_depth:
        for subdir in subdirs:
            yield from scan_directory(subdir, exclude_paths, wordy, max_depth, current_depth + 1)


def print_results(
    items: list[tuple[int, Path]],
    total_bytes: int,
    elapsed_time: float,
    reverse: bool = False,
) -> None:
    term_width = console.width
    sorted_items = sorted(items, key=lambda x: x[0], reverse=not reverse)
    rank_width = len(str(len(sorted_items))) + 2
    size_width = 12
    path_width = max(20, term_width - rank_width - size_width - 6)
    console.print()
    console.print("[bold cyan]Largest Footprint Results[/bold cyan]")
    console.print("─" * term_width, style="dim")
    for rank, (size, path) in enumerate(sorted_items, 1):
        formatted_size = format_size(size)
        path_str = str(path)
        if len(path_str) > path_width:
            path_str = "…" + path_str[-(path_width - 1) :]
        rank_str = f"#{rank}".ljust(rank_width)
        console.print(
            f"[yellow]{rank_str}[/yellow] "
            f"[bold green]{formatted_size:>{size_width}}[/bold green]  "
            f"[dim]{path_str}[/dim]"
        )
    console.print("─" * term_width, style="dim")
    console.print(
        f"[bold]Total Tracked Size:[/bold] [bold green]{format_size(total_bytes)}[/bold green] | "
        f"[dim]Scan finished in {elapsed_time:.2f}s[/dim]\n"
    )


def main() -> None:
    args = build_args()
    root_dir: Path = args.dir.resolve()
    if not root_dir.exists():
        console.print(f"[red]Error:[/red] Path '[bold]{root_dir}[/bold]' does not exist.")
        sys.exit(1)
    exclude_paths: set[Path] = {p.resolve() for p in args.exclude}
    min_heap: list[tuple[int, Path]] = []
    total_scanned_bytes = 0
    start_time = time.perf_counter()
    with Status(f"[bold green]Scanning {root_dir}...[/bold green]", console=console) as status:
        for current_dir, _, files in scan_directory(
            root_dir, exclude_paths, wordy=args.wordy, max_depth=args.recur_depth
        ):
            status.update(f"[bold green]Scanning:[/bold green] [dim]{current_dir}[/dim]")
            if args.mode == "folder":
                folder_size = sum(size for _, size in files)
                total_scanned_bytes += folder_size
                if len(min_heap) < args.topk:
                    heapq.heappush(min_heap, (folder_size, current_dir))
                else:
                    heapq.heappushpop(min_heap, (folder_size, current_dir))
            elif args.mode == "file":
                for file_path, file_size in files:
                    total_scanned_bytes += file_size
                    if len(min_heap) < args.topk:
                        heapq.heappush(min_heap, (file_size, file_path))
                    else:
                        heapq.heappushpop(min_heap, (file_size, file_path))
    elapsed_time = time.perf_counter() - start_time
    print_results(min_heap, total_scanned_bytes, elapsed_time, reverse=args.reverse)


if __name__ == "__main__":
    main()
