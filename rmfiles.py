#!/data/data/com.termux/files/usr/bin/env python
"""
Remove files listed in a text file (one path per line), reporting space freed.

Usage:
    python remove_files.py # uses list.txt python remove_files.py paths.txt # uses paths.txt python remove_files.py -n # dry run: show what would happen python remove_files.py -y -w 8 -v # no prompt, 8 workers, per-file output Lines that are blank or start with '#' are ignored. Duplicate paths are removed once. Missing files are skipped. Directories and other non-regular entries are reported as errors.

"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from functools import partial
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


DEFAULT_LIST: Path = Path("list.txt")

Status = Literal["removed", "would_remove", "missing", "error"]


@dataclass(frozen=True, slots=True)
class Result:
    """Outcome of processing a single path."""

    path: str
    status: Status
    bytes_freed: int = 0
    error: str | None = None


def format_bytes(num_bytes: int) -> str:
    """Convert a byte count to a human-readable string (powers of 1000)."""
    size = float(num_bytes)
    units: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")
    for unit in units[:-1]:
        if size < 1000:
            return f"{size:.0f} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} TB"


def remove_file(path_str: str, dry_run: bool = False) -> Result:
    """Delete one regular file or symlink. Runs in a worker process."""
    path = Path(path_str)

    try:
        # lstat() does not follow symlinks, so a link reports its own size
        st = path.lstat()
    except FileNotFoundError:
        return Result(path_str, "missing")
    except OSError as exc:
        return Result(path_str, "error", error=str(exc))

    if stat.S_ISLNK(st.st_mode):
        freed = 0
    elif stat.S_ISREG(st.st_mode):
        freed = st.st_size
    else:
        return Result(path_str, "error", error="not a regular file or symlink")

    if dry_run:
        return Result(path_str, "would_remove", bytes_freed=freed)

    try:
        path.unlink()
    except FileNotFoundError:
        return Result(path_str, "missing")
    except OSError as exc:
        return Result(path_str, "error", error=str(exc))

    return Result(path_str, "removed", bytes_freed=freed)


def read_paths(list_file: Path) -> list[str]:
    """Read paths from a file, skipping blanks and comments, removing duplicates."""
    with list_file.open(encoding="utf-8") as fh:
        raw = (
            line.rstrip("\r\n")
            for line in fh
            if line.strip() and not line.startswith("#")
        )
        # dict.fromkeys keeps first-seen order while dropping duplicates
        return list(dict.fromkeys(raw))


def confirm(count: int) -> bool:
    """Ask the user to confirm permanent deletion."""
    answer = input(f"Permanently delete {count} path(s)? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def run(
    paths: list[str], workers: int, chunksize: int, dry_run: bool
) -> Iterator[Result]:
    """Process paths in a process pool, yielding results in input order."""
    worker = partial(remove_file, dry_run=dry_run)
    with Pool(processes=workers) as pool:
        yield from pool.imap(worker, paths, chunksize=chunksize)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Remove files listed in a text file and report space freed."
    )
    parser.add_argument(
        "list_file",
        nargs="?",
        type=Path,
        default=DEFAULT_LIST,
        help=f"file containing one path per line (default: {DEFAULT_LIST})",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report what would be removed without deleting anything",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="number of worker processes (default: CPU count)",
    )
    parser.add_argument(
        "-c",
        "--chunksize",
        type=int,
        default=64,
        help="paths handed to each worker at a time (default: 64)",
    )

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="print each file's result"
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the summary"
    )

    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.chunksize < 1:
        parser.error("--chunksize must be at least 1")
    return args


def print_verbose(result: Result) -> None:
    """Print a per-file line when verbose output is enabled."""
    labels = {
        "removed": "removed",
        "would_remove": "would remove",
        "missing": "missing, skipped",
        "error": "error",
    }
    detail = f" ({format_bytes(result.bytes_freed)})" if result.bytes_freed else ""
    print(f"[{labels[result.status]}] {result.path}{detail}")


def main(argv: Iterable[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)
    list_file: Path = args.list_file

    if not list_file.is_file():
        print(f"Error: list file not found: {list_file}", file=sys.stderr)
        return 1

    paths = read_paths(list_file)
    if not paths:
        if not args.quiet:
            print(f"Nothing to do: {list_file} contains no paths.")
        return 0

    if (
        not args.dry_run
        and not args.yes
        and sys.stdin.isatty()
        and not confirm(len(paths))
    ):
        print("Aborted.")
        return 1

    counts: Counter[Status] = Counter()
    total_freed = 0

    try:
        for result in run(paths, args.workers, args.chunksize, args.dry_run):
            counts[result.status] += 1
            if result.status in {"removed", "would_remove"}:
                total_freed += result.bytes_freed
            if result.status == "error":
                print(f"Failed: {result.path}: {result.error}", file=sys.stderr)
            elif args.verbose:
                print_verbose(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    if not args.quiet:
        removed_count = counts["would_remove"] if args.dry_run else counts["removed"]
        verb = "Would remove" if args.dry_run else "Removed"
        freed_label = "Space that would be freed" if args.dry_run else "Space freed"
        print(
            f"{verb}: {removed_count}, missing (skipped): {counts['missing']}, errors: {counts['error']}"
        )
        print(f"{freed_label}: {format_bytes(total_freed)} ({total_freed:,} bytes)")

    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
