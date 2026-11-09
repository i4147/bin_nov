#!/data/data/com.termux/files/usr/bin/env python
"""Find the duplicate files and optionally delete them.
Sizes the files, then hashes a 64 KB prefix and the full content in a pool, groups the identical ones and reports the space that could be freed; ``-a`` deletes the duplicates (dry run by default), ``-m`` sets the minimum size.
Usage: dupeguru [-a] [-m MIN_BYTES] [paths ...] Positional arguments: paths ...
Files or directories to scan (default: current directory) Options: -a, --apply Delete the duplicates (default: dry run) -m, --min-size Ignore files smaller than this many bytes (default: 1) -q, --quiet Suppress progress messages Examples: $ dupeguru ~/Downloads $ dupeguru -a ~/Downloads Command-line script: run ``python dupeguru.py`` or the ``dupeguru`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import hashlib
import multiprocessing as mp
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Iterator, Sequence

from dh import MAX_WORKERS, SKIP_DIRS


CHUNK_SIZE: int = 1 << 20
PARTIAL_SIZE: int = 1 << 16
DIGEST_BYTES: int = 16


def human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if x < 1024 or unit == "PiB":
            return f"{int(x)} B" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024
    return f"{n} B"


def walk_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        try:
            if root.is_symlink():
                continue
            if root.is_file():
                yield root
                continue
            if not root.is_dir():
                continue
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not Path(Path(dirpath).joinpath(d)).is_symlink()
            ]
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.is_symlink():
                        continue
                    if p.is_file():
                        yield p
                except OSError:
                    continue


def _hash_worker(task: tuple[str, int | None]) -> tuple[str, str, int, str | None]:
    path, limit = task
    try:
        h = hashlib.blake2b(digest_size=DIGEST_BYTES)
        read = 0
        with open(path, "rb") as f:
            while True:
                if limit is not None and read >= limit:
                    break
                want = CHUNK_SIZE if limit is None else min(CHUNK_SIZE, limit - read)
                buf = f.read(want)
                if not buf:
                    break
                h.update(buf)
                read += len(buf)
        return path, h.hexdigest(), read, None
    except OSError as exc:
        return path, "", 0, str(exc)


def find_duplicates(
    roots: list[Path],
    workers: int,
    min_size: int,
    log: Any,
) -> list[list[Path]]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    seen_inodes: set[tuple[int, int]] = set()
    n_files = 0
    for p in walk_files(roots):
        try:
            st = p.stat()
        except OSError:
            continue
        n_files += 1
        if st.st_size < min_size:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen_inodes:
            continue
        seen_inodes.add(key)
        by_size[st.st_size].append(p)
    log(f"Scanned {n_files} file(s).")
    size_groups = {s: ps for s, ps in by_size.items() if len(ps) > 1}
    if not size_groups:
        return []
    size_of: dict[str, int] = {}
    partial_tasks: list[tuple[str, int | None]] = []
    for size, paths in size_groups.items():
        for p in paths:
            sp = str(p)
            size_of[sp] = size
            partial_tasks.append((sp, PARTIAL_SIZE))
    log(f"{len(partial_tasks)} candidate(s) share a size; partial-hashing...")
    partial_groups: dict[tuple[int, str], list[Path]] = defaultdict(list)
    with mp.Pool(processes=workers) as pool:
        for path, digest, _read, err in pool.imap_unordered(_hash_worker, partial_tasks, chunksize=32):
            if err:
                log(f"WARN: {path}: {err}")
                continue
            partial_groups[(size_of[path], digest)].append(Path(path))
    full_tasks: list[tuple[str, int | None]] = []
    for paths in partial_groups.values():
        if len(paths) > 1:
            full_tasks.extend((str(p), None) for p in paths)
    if not full_tasks:
        return []
    log(f"{len(full_tasks)} candidate(s) share a partial hash; full-hashing...")
    full_groups: dict[tuple[int, str], list[Path]] = defaultdict(list)
    with mp.Pool(processes=workers) as pool:
        for path, digest, size, err in pool.imap_unordered(_hash_worker, full_tasks, chunksize=1):
            if err:
                log(f"WARN: {path}: {err}")
                continue
            full_groups[(size, digest)].append(Path(path))
    return [paths for paths in full_groups.values() if len(paths) > 1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dupefinder",
        description="Find (and optionally delete) duplicate files. "
        "If no paths are given, scans the current directory recursively.",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path()],
        help="Files or directories to scan (default: current directory)",
    )
    ap.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Delete the duplicates (default: dry run)",
    )
    ap.add_argument(
        "-m",
        "--min-size",
        type=int,
        default=1,
        help="Ignore files smaller than this many bytes (default: 1)",
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages")
    args = ap.parse_args(argv)
    if not args.paths:
        args.paths = [Path()]
    for p in args.paths:
        if not p.exists():
            print(f"error: path does not exist: {p}", file=sys.stderr)
            return 2

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    log(f"Scanning {len(args.paths)} root(s) with {MAX_WORKERS} worker(s)...")
    groups = find_duplicates(
        roots=list(args.paths),
        workers=max(1, MAX_WORKERS),
        min_size=args.min_size,
        log=log,
    )
    if not groups:
        print("No duplicates found.")
        return 0
    prepared: list[tuple[list[Path], int]] = []
    total_redundant = 0
    total_bytes = 0
    for paths in groups:
        paths.sort()
        try:
            sz = paths[0].stat().st_size
        except OSError:
            sz = 0
        prepared.append((paths, sz))
        total_redundant += len(paths) - 1
        total_bytes += sz * (len(paths) - 1)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"=== {mode}: {len(prepared)} group(s), {total_redundant} redundant "
        f"file(s), {human_size(total_bytes)} reclaimable ===\n"
    )
    removed = 0
    reclaimed = 0
    for i, (paths, sz) in enumerate(prepared, 1):
        print(f"[Group {i}]  {human_size(sz)} each")
        print(f"  KEEP     {paths[0]}")
        for p in paths[1:]:
            if args.apply:
                try:
                    p.unlink()
                    print(f"  REMOVED  {p}")
                    removed += 1
                    reclaimed += sz
                except OSError as exc:
                    print(f"  FAILED   {p}  ({exc})", file=sys.stderr)
            else:
                print(f"  REMOVE   {p}")
        print()
    if args.apply:
        print(f"Removed {removed} file(s), reclaimed {human_size(reclaimed)}.")
    else:
        print(f"Dry run complete. Re-run with -a/--apply to delete {total_redundant} file(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
