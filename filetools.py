#!/data/data/com.termux/files/usr/bin/env python
"""Query and move files by age, size and extension with one subcommand CLI.
``scan-ctime`` reports files created in the last N minutes, ``filter``/``filter-recursive`` list files by extension, size and age, ``recent-modified`` and ``same-day`` select by mtime, ``move`` relocates matches into a destination directory, and ``newest``/``oldest``/``btime`` show the extremes (birth time is read through ``statx`` where available).
Usage: filetools {scan-ctime,filter,filter-recursive,recent-modified,same-day,move,newest,oldest,btime} [options] Positional arguments: minutes filename path Subcommands: scan-ctime filter filter-recursive recent-modified same-day move newest oldest btime Options: --dir --minutes --ext --min-size --non-recursive --old --dest -r, --recursive -n, --count Examples: $ filetools recent-modified --minutes 30 $ filetools move --ext .py --dest ../src Command-line script: run ``python filetools.py`` or the ``filetools`` command.
The module is importable as well."""

import argparse
import ctypes
import ctypes.util
from datetime import UTC, datetime
from functools import partial
import multiprocessing as mp
import operator
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Final, Sequence

from dh import MAX_WORKERS
from tqdm import tqdm

SECONDS_PER_MINUTE: Final[float] = 60.0
SECONDS_24H: Final[float] = 24 * 60 * 60
EXCLUDE_DIRS: Final[frozenset[str]] = frozenset({".git"})
EXCLUDED_DIRS: Final[frozenset[str]] = frozenset({".git", "__pycache__"})
AT_FDCWD: Final[int] = -100
STATX_BTIME: Final[int] = 2048
_ANSI: Final[dict[str, str]] = {
    "yellow": "\033[93m",
    "cyan": "\033[96m",
    "green": "\033[92m",
    "red": "\033[91m",
    "reset": "\033[0m",
}


def _cprint(text: str, color: str = "yellow") -> None:
    print(f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}")


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in root.walk(follow_symlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        files.extend(dirpath / fname for fname in filenames)
    return files


def _ctime_if_recent(path: Path, now: float, seconds: float) -> tuple[float, Path] | None:
    try:
        ctime = path.stat().st_ctime
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if now - ctime <= seconds:
        return ctime, path
    return None


def cmd_scan_ctime(args: argparse.Namespace) -> int:
    root = Path.cwd()
    files = _iter_files(root)
    if not files:
        return 0
    now = time.time()
    recent: list[tuple[float, Path]] = []
    with mp.Pool(processes=MAX_WORKERS) as pool:
        async_results = [pool.apply_async(_ctime_if_recent, (p, now, SECONDS_24H)) for p in files]
        for ar in tqdm(async_results, total=len(async_results), desc="Scanning", unit="file"):
            result = ar.get()
            if result is not None:
                recent.append(result)
    recent.sort(key=operator.itemgetter(0))
    for _, path in recent:
        print(path.relative_to(root))
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    minutes: float = args.minutes
    cutoff = time.time() - minutes * SECONDS_PER_MINUTE
    cwd = Path.cwd()
    ctm: dict[Path, float] = {}
    max_path_string = 20
    for path in cwd.glob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            continue
        stats = path.stat()
        created = stats.st_ctime
        modified = stats.st_mtime
        pathstr = str(path.name)
        max_path_string = max(len(pathstr), 20)
        if created <= cutoff or modified >= cutoff:
            ctm[path] = created
    ctmsorted = dict(sorted(ctm.items(), key=operator.itemgetter(1)))
    for pth, ct in ctmsorted.items():
        ctime_str = datetime.fromtimestamp(ct).strftime("%Y/%m/%d-%H:%M:%S")
        print(f"\033[05;96m{Path(pth).name[:19]:<{max_path_string}} \033[05;93m{ctime_str}\033[0m")
    return 0


def cmd_filter_recursive(args: argparse.Namespace) -> int:
    minutes: float = args.minutes
    cutoff = time.time() - minutes * SECONDS_PER_MINUTE
    cwd = Path.cwd()
    ctm: dict[Path, float] = {}
    for path in cwd.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            continue
        stats = path.stat()
        created = stats.st_ctime
        if created <= cutoff:
            ctm[path] = created
    ctmsorted = dict(sorted(ctm.items(), key=operator.itemgetter(1)))
    for pth, ct in ctmsorted.items():
        ctime_str = datetime.fromtimestamp(ct).strftime("%Y/%m/%d-%H:%M:%S")
        path_str = str(pth.relative_to(cwd))
        max_path_len = max(len(path_str), 20)
        print(f"{path_str:<{max_path_len}}", end=" ")
        _cprint(ctime_str, "yellow")
    return 0


def _check_file_age(path: Path, n_minutes: float) -> tuple[str, datetime] | None:
    try:
        mod_time = path.stat().st_mtime
        current_time = time.time()
        age_minutes = (current_time - mod_time) / SECONDS_PER_MINUTE
        if age_minutes <= n_minutes:
            return str(path), datetime.fromtimestamp(mod_time)
    except (OSError, PermissionError):
        pass
    return None


def cmd_recent_modified(args: argparse.Namespace) -> int:
    n_minutes: int = args.minutes
    if n_minutes < 0:
        print("Error: minutes must be a non-negative number")
        return 1
    cwd = Path.cwd()
    all_files = [p for p in cwd.rglob("*") if p.is_file() and not p.is_symlink() and ".git" not in p.parts]
    if not all_files:
        print("No files found in current directory")
        return 0
    print(f"Checking {len(all_files)} files for modifications in last {n_minutes} minute(s)...")
    print()
    with mp.Pool(MAX_WORKERS) as pool:
        results = pool.map(partial(_check_file_age, n_minutes=float(n_minutes)), all_files)
    recent_files = [r for r in results if r is not None]
    recent_files.sort(key=lambda x: x[1], reverse=True)
    if recent_files:
        print(f"Found {len(recent_files)} file(s) modified in the last {n_minutes} minute(s):\n")
        for path, mod_time in recent_files:
            print(f"{mod_time.strftime('%Y-%m-%d %H:%M:%S')} - {Path(path).relative_to(cwd)}")
    else:
        print(f"No files modified in the last {n_minutes} minute(s)")
    return 0


def _get_file_creation_time(path: Path) -> datetime | None:
    try:
        stat = path.stat()
        if sys.platform == "win32":
            return datetime.fromtimestamp(stat.st_ctime)
        return datetime.fromtimestamp(stat.st_mtime)
    except Exception as e:
        print(f"Error: {e}")
        return None


def cmd_same_day(args: argparse.Namespace) -> int:
    filename: str = args.filename
    target = Path(filename)
    if not target.exists():
        print(f"Error: File '{filename}' does not exist.")
        return 1
    directory = target.parent if str(target.parent) else Path()
    target_time = _get_file_creation_time(target)
    if target_time is None:
        return 1
    target_date = target_time.date()
    print(f"Input file: {filename}")
    print(f"Created on: {target_date}")
    print("-" * 40)
    found_files: list[tuple[datetime, str]] = []
    for name in os.listdir(directory):
        p = directory / name
        if not p.is_file() or p.samefile(target):
            continue
        file_time = _get_file_creation_time(p)
        if file_time is not None and file_time.date() == target_date:
            found_files.append((file_time, name))
    found_files.sort()
    if not found_files:
        print("No other files found created on the same day.")
    else:
        print(f"Found {len(found_files)} other file(s) created on the same day:")
        for file_time, name in found_files:
            print(f"{file_time.strftime('%H:%M:%S')} - {name}")
    return 0


def _file_age(path: Path) -> float:
    return time.time() - path.stat().st_ctime


def _unique_filename(dest_dir: Path, filename: str) -> Path:
    dest_path = dest_dir / filename
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    counter = 1
    while True:
        new_path = dest_dir / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def _move_file(path: Path, start_dir: Path, target_dir: Path) -> Path:
    rel_path = path.parent.relative_to(start_dir)
    dest_dir = target_dir / rel_path if str(rel_path) != "." else target_dir
    dest_dir.mkdir(exist_ok=True, parents=True)
    dest_path = _unique_filename(dest_dir, path.name)
    shutil.move(str(path), str(dest_path))
    return dest_path


def cmd_move(args: argparse.Namespace) -> int:
    start_dir = Path(args.dir).resolve()
    if not start_dir.is_dir():
        print(f"Directory not found: {start_dir}")
        return 1
    target_dir = start_dir / args.dest
    target_dir.mkdir(exist_ok=True, parents=True)
    threshold = args.minutes * SECONDS_PER_MINUTE
    extensions: set[str] | None = None
    if args.ext:
        extensions = {ext if ext.startswith(".") else f".{ext}" for ext in args.ext}
    if args.recursive:
        files = list(start_dir.rglob("*"))
    else:
        files = [f for f in start_dir.iterdir() if f.is_file()]
    moved = 0
    filtered = 0
    errors = 0
    for path in files:
        if not path.is_file():
            continue
        if target_dir in path.parents or path.parent == target_dir:
            continue
        if extensions is not None and path.suffix.lower() not in extensions:
            filtered += 1
            continue
        if args.min_size is not None and path.stat().st_size < args.min_size:
            filtered += 1
            continue
        try:
            age = _file_age(path)
            should_move = age > threshold if args.old else age <= threshold
            if should_move:
                dest_path = _move_file(path, start_dir, target_dir)
                label = "Moved (old)" if args.old else "Moved"
                print(f"{label}: {path.name} -> {dest_path.relative_to(start_dir)}")
                moved += 1
        except (OSError, PermissionError) as e:
            print(f"Error processing {path.name}: {e}")
            errors += 1
    print("\n" + "=" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Files moved: {moved}")
    print(f"Files filtered: {filtered}")
    print(f"Errors: {errors}")
    print("-" * 40)
    return 0


def _collect_files(cwd: Path, recursive: bool) -> list[Path]:
    files: list[Path] = []
    if recursive:
        for p in cwd.rglob("*"):
            if p.is_symlink() or any(part in EXCLUDED_DIRS for part in p.parts):
                continue
            if p.is_file():
                files.append(p)
    else:
        for p in cwd.glob("*"):
            if p.is_symlink() or any(part in EXCLUDED_DIRS for part in p.parts):
                continue
            if p.is_file() or p.is_dir():
                files.append(p)
    return files


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_newest(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    files = _collect_files(cwd, args.recursive)
    files.sort(key=lambda f: f.stat().st_ctime, reverse=True)
    print(f"\nTop {args.count} newest files (excluding .git & __pycache__):\n")
    for f in files[: args.count]:
        print(f"{_format_time(f.stat().st_ctime)}  -  {f.relative_to(cwd)}")
    return 0


def cmd_oldest(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    files = _collect_files(cwd, args.recursive)
    files.sort(key=lambda f: f.stat().st_mtime)
    print(f"\nTop {args.count} oldest files (excluding .git & __pycache__):\n")
    for f in files[: args.count]:
        print(f"{_format_time(f.stat().st_mtime)}  -  {f.relative_to(cwd)}")
    return 0


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16 * 1),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("__spare2", ctypes.c_uint64 * 14),
    ]


def _get_creation_time_statx(path: str) -> datetime | None:
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return None
    statx_buf = _Statx()
    result = libc.statx(AT_FDCWD, path.encode(), 0, STATX_BTIME, ctypes.byref(statx_buf))
    if result == 0 and (statx_buf.stx_mask & STATX_BTIME):
        return datetime.fromtimestamp(statx_buf.stx_btime.tv_sec, tz=UTC)
    return None


def cmd_btime(args: argparse.Namespace) -> int:
    result = _get_creation_time_statx(args.path)
    print(f"Creation time: {result}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filetools")
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan-ctime")
    p_scan.set_defaults(func=cmd_scan_ctime)
    p_filter = sub.add_parser("filter")
    p_filter.add_argument("minutes", nargs="?", type=float, default=60.0)
    p_filter.set_defaults(func=cmd_filter)
    p_filter_r = sub.add_parser("filter-recursive")
    p_filter_r.add_argument("minutes", nargs="?", type=float, default=60.0)
    p_filter_r.set_defaults(func=cmd_filter_recursive)
    p_recent = sub.add_parser("recent-modified")
    p_recent.add_argument("minutes", type=int)
    p_recent.set_defaults(func=cmd_recent_modified)
    p_same = sub.add_parser("same-day")
    p_same.add_argument("filename")
    p_same.set_defaults(func=cmd_same_day)
    p_move = sub.add_parser("move")
    p_move.add_argument("--dir", default=".")
    p_move.add_argument("--minutes", type=int, default=8)
    p_move.add_argument("--ext", nargs="+")
    p_move.add_argument("--min-size", type=int)
    p_move.add_argument("--non-recursive", action="store_true")
    p_move.add_argument("--old", action="store_true")
    p_move.add_argument("--dest", default="5min")
    p_move.set_defaults(func=cmd_move)
    p_newest = sub.add_parser("newest")
    p_newest.add_argument("-r", "--recursive", action="store_true")
    p_newest.add_argument("-n", "--count", type=int, default=20)
    p_newest.set_defaults(func=cmd_newest)
    p_oldest = sub.add_parser("oldest")
    p_oldest.add_argument("-r", "--recursive", action="store_true")
    p_oldest.add_argument("-n", "--count", type=int, default=10)
    p_oldest.set_defaults(func=cmd_oldest)
    p_btime = sub.add_parser("btime")
    p_btime.add_argument("path")
    p_btime.set_defaults(func=cmd_btime)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
