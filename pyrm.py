#!/data/data/com.termux/files/usr/bin/env python
"""Remove entries in the current directory tree (dry-run by default).
Usage:
    python script.py                 dry run: list readme.md files and directories
    python script.py -f              dry run: only readme.md files
    python script.py -d              dry run: only readme.md directories
    python script.py -e EXT          dry run: all files with extension EXT
    add -a / --apply to actually delete
Name matching is case-insensitive and exact.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass
import multiprocessing as mp
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence

from loguru import logger
from rich.console import Console
from rich.text import Text


TARGET_NAME: str = "readme.md"
WORKERS: int = 4
SIZE_WIDTH: int = 10
console = Console(highlight=False)


@dataclass(frozen=True)
class Result:
    path: Path
    size: int
    ok: bool
    error: str = ""


def is_directory_entry(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def entry_size(path: Path) -> int:
    """Size of a file, or the total size of all files under a directory."""
    if is_directory_entry(path):
        return sum(
            p.lstat().st_size for p in path.rglob("*") if not is_directory_entry(p)
        )
    return path.lstat().st_size


def remove_entry(path: Path) -> Result:
    """Measure and delete one entry. Runs in a worker process."""
    try:
        size = entry_size(path)
        if is_directory_entry(path):
            shutil.rmtree(path)
        else:
            path.unlink()
        return Result(path, size, True)
    except OSError as exc:
        return Result(path, 0, False, str(exc))


def find_by_name(root: Path, want_files: bool, want_dirs: bool) -> list[Path]:
    """Collect exact-name matches first so deletion does not alter the walk."""
    targets: list[Path] = []
    for path in root.rglob("*"):
        if path.name.lower() != TARGET_NAME:
            continue
        if is_directory_entry(path):
            if want_dirs:
                targets.append(path)
        elif want_files:
            targets.append(path)
    return targets


def find_by_extension(root: Path, ext: str) -> list[Path]:
    """Collect files whose suffix matches ext, case-insensitively."""
    suffix = "." + ext.lower().lstrip(".")
    return [
        path
        for path in root.rglob("*")
        if not is_directory_entry(path)
        and path.is_file()
        and path.suffix.lower() == suffix
    ]


def format_size(num_bytes: int) -> str:
    """Return a size in KB or MB (base 1024)."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / 1024:.1f} KB"


def print_line(index: int, total: int, rel: str, size: int) -> None:
    """Print '[i/N]  relpath   size' fitted to the terminal width."""
    width = console.size.width
    prefix = f"[{index}/{total}]"
    avail = max(width - len(prefix) - SIZE_WIDTH - 4, 10)
    if len(rel) > avail:
        rel = rel[: avail - 1] + "…"
    line = Text()
    line.append(f"{prefix} ")
    line.append(f"{rel:<{avail}}")
    line.append(" ")
    line.append(f"{format_size(size):>{SIZE_WIDTH}}", style="green")
    console.print(line)


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, format="<level>{level: <8}</level> {message}")
    import argparse

    parser = argparse.ArgumentParser(
        description="Remove readme.md entries or files by extension (dry-run by default)."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "-f", "--files", action="store_true", help="target readme.md files"
    )
    scope.add_argument(
        "-d", "--dirs", action="store_true", help="target readme.md directories"
    )
    scope.add_argument(
        "-e", "--ext", metavar="EXT", help="target files with this extension"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="only list matches (default)"
    )
    mode.add_argument("-a", "--apply", action="store_true", help="delete the matches")
    args = parser.parse_args()
    root = Path.cwd()
    if args.ext:
        if not args.ext.strip("."):
            parser.error("extension must not be empty")
        targets = find_by_extension(root, args.ext)
        label = f"*.{args.ext.lower().lstrip('.')} files"
    else:
        any_flag = args.files or args.dirs
        want_files = args.files or not any_flag
        want_dirs = args.dirs or not any_flag
        targets = find_by_name(root, want_files, want_dirs)
        label = f"'{TARGET_NAME}' entries"
    if not targets:
        logger.info(f"no {label} found under '{root}'")
        return 0
    total = len(targets)
    if not args.apply:
        logger.info(
            f"dry run: {total} {label} matched under '{root}' (use -a/--apply to delete)"
        )
        would_free = 0
        for index, path in enumerate(sorted(targets), start=1):
            rel = str(path.relative_to(root))
            try:
                size = entry_size(path)
            except OSError as exc:
                logger.warning(f"[{index}/{total}] cannot measure '{rel}': {exc}")
                size = 0
            would_free += size
            print_line(index, total, rel, size)
        console.print(f"\nWould free: [green]{format_size(would_free)}[/green]")
        return 0
    logger.info(f"removing {total} {label} under '{root}'")
    freed = 0
    failed = 0
    with mp.Pool(WORKERS) as pool:
        for index, result in enumerate(
            pool.imap_unordered(remove_entry, targets), start=1
        ):
            rel = str(result.path.relative_to(root))
            if result.ok:
                freed += result.size
                print_line(index, total, rel, result.size)
            else:
                failed += 1
                logger.error(
                    f"[{index}/{total}] failed to remove '{rel}': {result.error}"
                )
    console.print(f"\nSpace freed: [green]{format_size(freed)}[/green]")
    if failed:
        logger.warning(f"{failed} of {total} entries could not be removed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
