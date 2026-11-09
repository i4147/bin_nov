#!/data/data/com.termux/files/usr/bin/env python
"""Optimise PNG files in place with optipng.
Runs optipng (``-o`` level 7, ``--strip all``) on each PNG, validates the result and keeps it only when the new file is smaller; ``--dry-run``, ``-q``, extra optipng arguments and a timeout are supported, and the tree is walked with a process pool.
Usage: opng [paths ...] [-o LEVEL] [--strip] [--dry-run] Positional arguments: paths ...
Files or directories to process (default: current directory).
Options: -q, --quiet Do not print optipng's own stdout/stderr; only summary lines.
--dry-run Run and validate, but do not modify any files.
-o --strip Pass '-strip all' to optipng to remove ancillary chunks.
--optipng Path to the optipng executable (default: search PATH).
--optipng-arg Extra argument to pass to optipng; may be repeated.
--timeout Examples: $ opng assets/ -o 7 --strip Command-line script: run ``python opng.py`` or the ``opng`` command.
The module is importable as well."""

import argparse
from collections.abc import Iterator, Sequence
import contextlib
from functools import partial
from multiprocessing import Pool
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Final, NamedTuple

from dh import MAX_WORKERS, SKIP_DIRS


PNG_SUFFIXES: Final[frozenset[str]] = frozenset({".png"})
PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
WALK_CHUNK: Final[int] = 512
IMAP_CHUNKSIZE: Final[int] = 4
DEFAULT_TIMEOUT: Final[float] = 300.0
COMPARE_CHUNK: Final[int] = 1 << 16
DEFAULT_OPT_LEVEL: Final[int] = 7
OPT_LEVELS: Final[range] = range(1, 8)
OPTIPNG_SUCCESS: Final[int] = 0


class ProcessResult(NamedTuple):
    path: Path
    original_size: int
    new_size: int
    stdout: str
    stderr: str
    skipped: bool
    no_change: bool
    error: str | None


def walk_files(directory: Path) -> Iterator[Path]:
    for root, dirs, files in directory.walk():
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield root / name


def format_bytes(n: int) -> str:
    sign = "-" if n < 0 else ""
    value = float(abs(n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{sign}{int(value)} B"
            return f"{sign}{value:.2f} {unit}"
        value /= 1024.0
    return f"{sign}{value:.2f} TiB"


def _dedupe(path: Path, seen: set[Path]) -> bool:
    try:
        key = path.resolve()
    except OSError:
        key = path
    if key in seen:
        return False
    seen.add(key)
    return True


def _iter_directory(root: Path, seen: set[Path]) -> Iterator[Path]:
    try:
        found = list(walk_files(root))
    except Exception as exc:
        print(f"warning: walk_files failed for {root}: {exc}", file=sys.stderr)
        return
    for start in range(0, len(found), WALK_CHUNK):
        for path in found[start : start + WALK_CHUNK]:
            if path.suffix.lower() not in PNG_SUFFIXES:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            if any(part in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            try:
                if path.is_symlink():
                    continue
            except OSError:
                continue
            if not _dedupe(path, seen):
                continue
            yield path


def iter_png_files(roots: Sequence[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for raw_root in roots:
        try:
            if not raw_root.exists():
                print(
                    f"warning: path does not exist: {raw_root}",
                    file=sys.stderr,
                )
                continue
            if raw_root.is_file():
                if raw_root.is_symlink():
                    print(
                        f"warning: skipping symlink: {raw_root}",
                        file=sys.stderr,
                    )
                    continue
                if raw_root.suffix.lower() in PNG_SUFFIXES and _dedupe(raw_root, seen):
                    yield raw_root
                continue
            if raw_root.is_dir():
                try:
                    root = raw_root.resolve()
                except OSError as exc:
                    print(
                        f"warning: cannot resolve {raw_root}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                yield from _iter_directory(root, seen)
                continue
            print(
                f"warning: not a file or directory: {raw_root}",
                file=sys.stderr,
            )
        except OSError as exc:
            print(
                f"warning: cannot inspect {raw_root}: {exc}",
                file=sys.stderr,
            )


def _files_identical(a: Path, b: Path, a_size: int, b_size: int) -> bool:
    if a_size != b_size:
        return False
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            chunk_a = fa.read(COMPARE_CHUNK)
            chunk_b = fb.read(COMPARE_CHUNK)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def _is_valid_png(path: Path) -> tuple[bool, str | None]:
    try:
        with path.open("rb") as f:
            header = f.read(len(PNG_MAGIC))
    except OSError as exc:
        return False, f"cannot read output: {exc}"
    if header != PNG_MAGIC:
        return False, "output is not a PNG (bad signature)"
    return True, None


def process_file(
    path: Path,
    *,
    optipng: str,
    opt_level: int,
    extra_args: Sequence[str],
    strip: bool,
    timeout: float,
    dry_run: bool,
) -> ProcessResult:
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        return ProcessResult(path, 0, 0, "", "", False, False, f"cannot resolve: {exc}")
    try:
        st = target.stat()
    except OSError as exc:
        return ProcessResult(path, 0, 0, "", "", False, False, f"cannot stat: {exc}")
    original_size = st.st_size
    original_mode = stat.S_IMODE(st.st_mode)
    tmp_path: Path | None = None
    try:
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=target.stem + ".",
                suffix=target.suffix + ".tmp",
                dir=str(target.parent),
            )
            with contextlib.suppress(OSError):
                os.close(fd)
            tmp_path = Path(tmp_name)
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                0,
                "",
                "",
                False,
                False,
                f"cannot create temp file: {exc}",
            )
        try:
            shutil.copy2(target, tmp_path)
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                0,
                "",
                "",
                False,
                False,
                f"cannot stage temp copy: {exc}",
            )
        cmd: list[str] = [optipng, "-o", str(opt_level)]
        if strip:
            cmd += ["-strip", "all"]
        cmd.extend(extra_args)
        cmd.append("--")
        cmd.append(str(tmp_path))
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProcessResult(
                path,
                original_size,
                0,
                "",
                "",
                False,
                False,
                f"optipng timed out after {timeout:g}s",
            )
        except FileNotFoundError:
            return ProcessResult(
                path,
                original_size,
                0,
                "",
                "",
                False,
                False,
                f"optipng executable not found: {optipng!r}",
            )
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                0,
                "",
                "",
                False,
                False,
                f"failed to run optipng: {exc}",
            )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != OPTIPNG_SUCCESS:
            return ProcessResult(
                path,
                original_size,
                0,
                stdout,
                stderr,
                False,
                False,
                f"optipng exited with status {completed.returncode}",
            )
        try:
            new_size = tmp_path.stat().st_size
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                0,
                stdout,
                stderr,
                False,
                False,
                f"optipng produced no output: {exc}",
            )
        if new_size == 0:
            return ProcessResult(
                path,
                original_size,
                0,
                stdout,
                stderr,
                False,
                False,
                "optipng produced an empty file",
            )
        ok, sig_err = _is_valid_png(tmp_path)
        if not ok:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                False,
                False,
                sig_err,
            )
        try:
            identical = _files_identical(target, tmp_path, original_size, new_size)
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                False,
                False,
                f"cannot compare files: {exc}",
            )
        if identical:
            return ProcessResult(
                path,
                original_size,
                original_size,
                stdout,
                stderr,
                False,
                True,
                None,
            )
        if new_size > original_size:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                True,
                False,
                None,
            )
        if new_size == original_size:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                True,
                False,
                None,
            )
        if dry_run:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                False,
                False,
                None,
            )
        try:
            os.chmod(tmp_path, original_mode)
            os.replace(tmp_path, target)
        except OSError as exc:
            return ProcessResult(
                path,
                original_size,
                new_size,
                stdout,
                stderr,
                False,
                False,
                f"failed to replace original: {exc}",
            )
        tmp_path = None
        return ProcessResult(
            path,
            original_size,
            new_size,
            stdout,
            stderr,
            False,
            False,
            None,
        )
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _classify(result: ProcessResult, dry_run: bool) -> str:
    if result.error is not None:
        return "ERROR"
    if result.no_change:
        return "NOCHG"
    if result.skipped:
        return "SAME" if result.new_size == result.original_size else "SKIP"
    if dry_run:
        return "DRY-RUN"
    return "OK"


def _emit_optipng_output(result: ProcessResult) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


def _print_status(result: ProcessResult, tag: str) -> None:
    path = result.path
    orig = result.original_size
    new = result.new_size
    if tag == "OK":
        saved = orig - new
        pct = (saved / orig * 100.0) if orig else 0.0
        print(f"[OK]      {path}  {format_bytes(orig)} -> {format_bytes(new)}  (-{format_bytes(saved)}, -{pct:.1f}%)")
    elif tag == "DRY-RUN":
        saved = orig - new
        pct = (saved / orig * 100.0) if orig else 0.0
        print(
            f"[DRY-RUN] {path}  "
            f"{format_bytes(orig)} -> {format_bytes(new)}  "
            f"(would save {format_bytes(saved)}, {pct:.1f}%)"
        )
    elif tag == "NOCHG":
        print(f"[NOCHG]   {path}  {format_bytes(orig)}  (byte-identical)")
    elif tag == "SAME":
        print(f"[SAME]    {path}  {format_bytes(orig)}  (same size, different bytes)")
    elif tag == "SKIP":
        growth = new - orig
        print(f"[SKIP]    {path}  {format_bytes(orig)} -> {format_bytes(new)}  (larger by {format_bytes(growth)})")
    else:
        print(f"[ERROR]   {path}  {result.error}", file=sys.stderr)


def _print_summary(counters: dict[str, int], total_before: int, total_after: int) -> None:
    total = sum(counters.values())
    skipped = counters["SAME"] + counters["SKIP"]
    print()
    print("-" * 66)
    print("Summary")
    print("-" * 66)
    print(f"  Processed : {total}")
    print(f"  Changed   : {counters['OK']}")
    if counters["DRY-RUN"]:
        print(f"  Dry-run   : {counters['DRY-RUN']}")
    print(f"  Unchanged : {counters['NOCHG']}")
    print(f"  Skipped   : {skipped}")
    if skipped:
        print(f"      same-size-different-bytes : {counters['SAME']}")
        print(f"      larger-output             : {counters['SKIP']}")
    print(f"  Errored   : {counters['ERROR']}")
    if total_before:
        saved = total_before - total_after
        pct = (saved / total_before * 100.0) if total_before else 0.0
        print()
        print(
            f"  Bytes     : {format_bytes(total_before)} -> "
            f"{format_bytes(total_after)}  "
            f"(saved {format_bytes(saved)}, {pct:.1f}%)"
        )
    else:
        print()
        print("  Bytes     : no size changes")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="optipng-optimize",
        description=(
            "Optimize PNG files in place using optipng. With no paths, the current directory is walked recursively."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Files or directories to process (default: current directory).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not print optipng's own stdout/stderr; only summary lines.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run and validate, but do not modify any files.",
    )
    parser.add_argument(
        "-o",
        dest="opt_level",
        type=int,
        choices=OPT_LEVELS,
        default=DEFAULT_OPT_LEVEL,
        metavar="1-7",
        help=(f"optipng optimization level (-o), 1 (fastest) to 7 (best). Default: {DEFAULT_OPT_LEVEL}."),
    )
    parser.add_argument(
        "--strip",
        action="store_true",
        help="Pass '-strip all' to optipng to remove ancillary chunks.",
    )
    parser.add_argument(
        "--optipng",
        default="optipng",
        metavar="PATH",
        help="Path to the optipng executable (default: search PATH).",
    )
    parser.add_argument(
        "--optipng-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument to pass to optipng; may be repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Per-file optipng timeout (default: {DEFAULT_TIMEOUT:g}s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    roots = [Path(p) for p in args.paths] if args.paths else [Path()]
    worker = partial(
        process_file,
        optipng=args.optipng,
        opt_level=args.opt_level,
        extra_args=tuple(args.optipng_arg),
        strip=args.strip,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    counters: dict[str, int] = {
        "OK": 0,
        "DRY-RUN": 0,
        "NOCHG": 0,
        "SAME": 0,
        "SKIP": 0,
        "ERROR": 0,
    }
    total_before = 0
    total_after = 0
    files = iter_png_files(roots)
    try:
        with Pool(processes=MAX_WORKERS) as pool:
            for result in pool.imap_unordered(worker, files, chunksize=IMAP_CHUNKSIZE):
                if not args.quiet:
                    _emit_optipng_output(result)
                tag = _classify(result, args.dry_run)
                counters[tag] += 1
                if tag in ("OK", "DRY-RUN"):
                    total_before += result.original_size
                    total_after += result.new_size
                _print_status(result, tag)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    _print_summary(counters, total_before, total_after)
    return 1 if counters["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
