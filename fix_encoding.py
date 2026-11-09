#!/data/data/com.termux/files/usr/bin/env python
"""Detect the text file encodings and convert everything to UTF-8 with LF endings.
Reads each text file through charset_normalizer/binaryornot, converts the non-UTF-8 content, normalises the CRLF line endings (``--no-dos2unix`` keeps them), can strip the BOM and offers ``--dry-run``, ``--backup``, ``--preserve-mtime``, include/exclude globs and a size limit.
Usage: fix_encoding [root] [-n] [-b] [--strip-bom] [-i GLOB] [-e GLOB] Positional arguments: root Options: -n, --dry-run -b, --backup --no-dos2unix --strip-bom -a, --assume-encoding -c, --candidates -x, --exclude-dir -i, --include -e, --exclude -m, --max-size ...
plus 3 more; run ``fix_encoding --help`` for the full list.
Examples: $ fix_encoding docs/ -n $ fix_encoding src/ -b Command-line script: run ``python fix_encoding.py`` or the ``fix_encoding`` command.
The module is importable as well."""

import argparse
import codecs
from collections import Counter
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from enum import Enum
import fnmatch
from functools import partial
import math
import os
from pathlib import Path
import sys
import time

from atomicio import atomic_write
from binaryornot.check import is_binary
from charset_normalizer import from_bytes
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.text import Text


UTF8_NAMES: frozenset[str] = frozenset({"utf-8", "utf-8-sig"})


class Action(str, Enum):
    CONVERTED = "converted"
    DOS2UNIX = "dos2unix"
    UNCHANGED = "unchanged"
    DRY_RUN = "dry-run"
    ERROR = "error"


ACTION_STYLES: dict[Action, str] = {
    Action.CONVERTED: "yellow",
    Action.DOS2UNIX: "cyan",
    Action.UNCHANGED: "dim",
    Action.DRY_RUN: "magenta",
    Action.ERROR: "bold red",
}


@dataclass(frozen=True, slots=True)
class Options:
    dos2unix: bool
    strip_bom: bool
    backup: bool
    dry_run: bool
    preserve_mtime: bool
    assume_encoding: str | None
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Result:
    path: Path
    encoding: str
    action: Action
    error: str | None = None


def walk(root: Path, skip_dirs: frozenset[str]) -> Iterator[Path]:
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        logger.error("{}: cannot list directory: {}", root, exc)
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError as exc:
            logger.error("{}: cannot stat: {}", entry, exc)
            continue
        if is_dir:
            if entry.name not in skip_dirs:
                yield from walk(entry, skip_dirs)
        elif is_file:
            yield entry


def is_selected(name: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatch(name, pattern) for pattern in exclude)


def iter_text_files(
    root: Path,
    skip_dirs: frozenset[str],
    include: Sequence[str],
    exclude: Sequence[str],
    max_size: int,
) -> Iterator[Path]:
    for path in walk(root, skip_dirs):
        if not is_selected(path.name, include, exclude):
            continue
        try:
            size = path.stat().st_size
            if size == 0 or (max_size > 0 and size > max_size):
                continue
            if is_binary(str(path)):
                continue
        except Exception as exc:
            logger.error("{}: cannot inspect: {}", path, exc)
            continue
        yield path


def detect_encoding(data: bytes, assume: str | None, candidates: Sequence[str] = ()) -> str:
    if assume is not None:
        if assume in UTF8_NAMES:
            data.decode(assume)
        return assume
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return "utf-8"
    match = from_bytes(data, cp_isolation=list(candidates) or None).best()
    if match is None:
        msg = "encoding could not be detected"
        raise ValueError(msg)
    return codecs.lookup(match.encoding).name


def convert(data: bytes, encoding: str, opts: Options) -> bytes:
    if encoding in UTF8_NAMES:
        payload = data
        if encoding == "utf-8-sig" and opts.strip_bom:
            payload = payload[len(codecs.BOM_UTF8) :]
        if opts.dos2unix:
            payload = payload.replace(b"\r\n", b"\n")
        return payload
    text = data.decode(encoding)
    if opts.dos2unix:
        text = text.replace("\r\n", "\n")
    return text.encode("utf-8")


def process_file(path: Path, opts: Options) -> Result:
    encoding = "-"
    try:
        with path.open("rb") as handle:
            data = handle.read()
            st = os.fstat(handle.fileno())
        encoding = detect_encoding(data, opts.assume_encoding, opts.candidates)
        payload = convert(data, encoding, opts)
        if payload == data:
            return Result(path, encoding, Action.UNCHANGED)
        encoding_changed = encoding not in UTF8_NAMES or (encoding == "utf-8-sig" and opts.strip_bom)
        action = Action.CONVERTED if encoding_changed else Action.DOS2UNIX
        if opts.dry_run:
            return Result(path, encoding, Action.DRY_RUN)
        atomic_write(path, payload, st, opts)
        return Result(path, encoding, action)
    except Exception as exc:
        return Result(path, encoding, Action.ERROR, f"{type(exc).__name__}: {exc}")


def compute_chunksize(total: int, jobs: int) -> int:
    return max(1, min(64, math.ceil(total / (jobs * 4))))


def run(files: Sequence[Path], opts: Options, jobs: int) -> Iterator[Result]:
    worker = partial(process_file, opts=opts)
    workers = min(jobs, len(files))
    if workers <= 1:
        yield from map(worker, files)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(worker, files, chunksize=compute_chunksize(len(files), workers))


def make_row(
    counter: str,
    path: str,
    encoding: str,
    action: Action,
    counter_width: int,
    encoding_width: int,
    action_width: int,
) -> Table:
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(width=counter_width, no_wrap=True)
    grid.add_column(ratio=1, min_width=10, no_wrap=True, overflow="ellipsis")
    grid.add_column(width=encoding_width, no_wrap=True, overflow="ellipsis")
    grid.add_column(width=action_width, no_wrap=True)
    grid.add_row(
        Text(counter),
        Text(path),
        Text(encoding, style="green"),
        Text(action.value, style=ACTION_STYLES[action]),
    )
    return grid


def configure_logging(log_file: Path | None) -> None:
    logger.remove()
    logger.add(sys.stderr, level="ERROR", format="<red>{level}</red>: {message}")
    if log_file is not None:
        logger.add(log_file, level="ERROR", rotation="5 MB", encoding="utf-8")


def codec_name(value: str) -> str:
    try:
        return codecs.lookup(value).name
    except LookupError as exc:
        msg = f"unknown encoding: {value}"
        raise argparse.ArgumentTypeError(msg) from exc


def codec_list(value: str) -> tuple[str, ...]:
    return tuple(codec_name(item.strip()) for item in value.split(",") if item.strip())


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fix_encoding",
        description="Report text file encodings, convert to UTF-8 and normalize CRLF to LF in place.",
    )
    parser.add_argument("root", nargs="?", type=Path, default=Path())
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-b", "--backup", action="store_true")
    parser.add_argument("--no-dos2unix", action="store_true")
    parser.add_argument("--strip-bom", action="store_true")
    parser.add_argument("-a", "--assume-encoding", type=codec_name, metavar="ENC")
    parser.add_argument("-c", "--candidates", type=codec_list, default=(), metavar="ENC,ENC")
    parser.add_argument("-x", "--exclude-dir", action="append", default=None, metavar="NAME")
    parser.add_argument("-i", "--include", action="append", default=None, metavar="GLOB")
    parser.add_argument("-e", "--exclude", action="append", default=None, metavar="GLOB")
    parser.add_argument("-m", "--max-size", type=float, default=64.0, metavar="MB")
    parser.add_argument("-p", "--preserve-mtime", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-l", "--log-file", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_file)
    console = Console()
    root: Path = args.root.resolve()
    if not root.is_dir():
        logger.error("{}: not a directory", root)
        return 2
    if MAX_WORKERS < 1:
        logger.error("--jobs must be >= 1")
        return 2
    opts = Options(
        dos2unix=not args.no_dos2unix,
        strip_bom=args.strip_bom,
        backup=args.backup,
        dry_run=args.dry_run,
        preserve_mtime=args.preserve_mtime,
        assume_encoding=args.assume_encoding,
        candidates=args.candidates,
    )
    skip_dirs = SKIP_DIRS | frozenset(args.exclude_dir or [])
    exclude: list[str] = [*(args.exclude or []), "*.bak"]
    max_size = int(args.max_size * 1024 * 1024)
    start = time.perf_counter()
    files = list(iter_text_files(root, skip_dirs, args.include or [], exclude, max_size))
    total = len(files)
    if total == 0:
        console.print("no text files found")
        return 0
    counter_width = len(f"[{total}/{total}]")
    action_width = 10
    encoding_width = max(10, min(20, console.width // 6))
    counts: Counter[Action] = Counter()
    try:
        for index, result in enumerate(run(files, opts, MAX_WORKERS), 1):
            counts[result.action] += 1
            if result.error is not None:
                logger.error("{}: {}", result.path, result.error)
            if args.quiet and result.action is Action.UNCHANGED:
                continue
            console.print(
                make_row(
                    f"[{index}/{total}]",
                    str(result.path.relative_to(root)),
                    result.encoding,
                    result.action,
                    counter_width,
                    encoding_width,
                    action_width,
                )
            )
    except KeyboardInterrupt:
        console.print("[bold red]interrupted[/bold red]")
        return 130
    elapsed = time.perf_counter() - start
    parts = [f"{action.value} {counts[action]}" for action in Action if counts[action]]
    console.print(Text(f"{total} files | {' | '.join(parts)} | {elapsed:.2f}s", style="bold"))
    return 1 if counts[Action.ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
