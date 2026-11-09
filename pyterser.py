#!/data/data/com.termux/files/usr/bin/env python
"""Minify JavaScript files in place with terser.
Runs terser with the compress/mangle/toplevel/module options on the ``.js``/``.mjs``/``.cjs`` files, keeps the result only when it is smaller and valid, supports ``--dry-run``, ``-q`` and a timeout, and processes the tree in a pool.
Usage: pyterser [paths ...] [--dry-run] [--no-mangle] Positional arguments: paths ...
Files or directories to process (default: current directory).
Options: -q, --quiet Do not print terser's own stdout/stderr; only summary lines.
--dry-run Run and validate, but do not modify any files.
--mangle Pass --mangle to terser (default: on).
Use --no-mangle to skip.
--compress Pass --compress to terser (default: on).
Use --no-compress to skip.
--toplevel Pass --toplevel to terser (mangle/compress top-level names).
Requires --mangle or --compress to have any effect.
--module Pass --module to terser (treat input as an ES module).
--ecma Pass --ecma N to terser (target ECMAScript version, e.g.
5, 2015, 2020).
Omitted by default so terser picks its own.
--terser Path to the terser executable (default: search PATH).
--terser-arg Extra argument to pass to terser; may be repeated.
--timeout Examples: $ pyterser static/js --toplevel Command-line script: run ``python pyterser.py`` or the ``pyterser`` command.
The module is importable as well."""

import argparse
import contextlib
from functools import partial
from multiprocessing import Pool
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Final, Iterator, NamedTuple, Sequence

from dh import MAX_WORKERS, SKIP_DIRS


JS_SUFFIXES: Final[frozenset[str]] = frozenset({".js", ".mjs", ".cjs"})
WALK_CHUNK: Final[int] = 512
IMAP_CHUNKSIZE: Final[int] = 4
DEFAULT_TIMEOUT: Final[float] = 300.0
COMPARE_CHUNK: Final[int] = 1 << 16
TERSER_SUCCESS: Final[int] = 0
MIN_SAVINGS_BYTES: Final[int] = 2


class ProcessResult(NamedTuple):
    path: Path
    original_size: int
    new_size: int
    stdout: str
    stderr: str
    skipped: bool
    no_change: bool
    error: str | None


def walk_files(directory: str) -> Iterator[str]:
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root).joinpath(name)


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


def display_path(path: Path) -> str:
    try:
        return Path(path).relative_to(Path.cwd())
    except (OSError, ValueError):
        return str(path)


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
        found = list(walk_files(str(root)))
    except Exception as exc:
        print(f"warning: walk_files failed for {root}: {exc}", file=sys.stderr)
        return
    for start in range(0, len(found), WALK_CHUNK):
        for entry in found[start : start + WALK_CHUNK]:
            path = Path(entry)
            if path.suffix.lower() not in JS_SUFFIXES:
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


def iter_js_files(roots: Sequence[Path]) -> Iterator[Path]:
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
                if raw_root.suffix.lower() in JS_SUFFIXES and _dedupe(raw_root, seen):
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


def _run_terser(cmd: Sequence[str], timeout: float) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"terser timed out after {timeout:g}s"
    except FileNotFoundError:
        return None, f"terser executable not found: {cmd[0]!r}"
    except OSError as exc:
        return None, f"failed to run terser: {exc}"
    return completed, None


def _build_terser_cmd(
    terser: str,
    target: Path,
    tmp_path: Path,
    *,
    mangle: bool,
    compress: bool,
    toplevel: bool,
    module: bool,
    ecma: int | None,
    extra_args: Sequence[str],
) -> list[str]:
    cmd: list[str] = [terser]
    if mangle:
        cmd.append("--mangle")
    if compress:
        cmd.append("--compress")
    if toplevel and (mangle or compress):
        cmd.append("--toplevel")
    if module:
        cmd.append("--module")
    if ecma is not None:
        cmd += ["--ecma", str(ecma)]
    cmd.extend(extra_args)
    cmd += ["-o", str(tmp_path), str(target)]
    return cmd


def process_file(
    path: Path,
    *,
    terser: str,
    mangle: bool,
    compress: bool,
    toplevel: bool,
    module: bool,
    ecma: int | None,
    extra_args: Sequence[str],
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
        cmd = _build_terser_cmd(
            terser,
            target,
            tmp_path,
            mangle=mangle,
            compress=compress,
            toplevel=toplevel,
            module=module,
            ecma=ecma,
            extra_args=extra_args,
        )
        completed, run_error = _run_terser(cmd, timeout)
        if completed is None:
            return ProcessResult(path, original_size, 0, "", "", False, False, run_error)
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != TERSER_SUCCESS:
            return ProcessResult(
                path,
                original_size,
                0,
                stdout,
                stderr,
                False,
                False,
                f"terser exited with status {completed.returncode}",
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
                f"terser produced no output: {exc}",
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
                "terser produced an empty file",
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
        if original_size - new_size < MIN_SAVINGS_BYTES:
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
        return "SKIP"
    if dry_run:
        return "DRY-RUN"
    return "OK"


def _emit_terser_output(result: ProcessResult) -> None:
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
    shown = display_path(result.path)
    orig = result.original_size
    new = result.new_size
    if tag == "OK":
        saved = orig - new
        pct = (saved / orig * 100.0) if orig else 0.0
        print(f"[OK]      {shown}  {format_bytes(orig)} -> {format_bytes(new)}  (-{format_bytes(saved)}, -{pct:.1f}%)")
    elif tag == "DRY-RUN":
        saved = orig - new
        pct = (saved / orig * 100.0) if orig else 0.0
        print(
            f"[DRY-RUN] {shown}  "
            f"{format_bytes(orig)} -> {format_bytes(new)}  "
            f"(would save {format_bytes(saved)}, {pct:.1f}%)"
        )
    elif tag == "NOCHG":
        print(f"[NOCHG]   {shown}  {format_bytes(orig)}  (no meaningful change)")
    elif tag == "SKIP":
        growth = new - orig
        print(f"[SKIP]    {shown}  {format_bytes(orig)} -> {format_bytes(new)}  (larger by {format_bytes(growth)})")
    else:
        print(f"[ERROR]   {shown}  {result.error}", file=sys.stderr)


def _print_summary(counters: dict[str, int], total_before: int, total_after: int) -> None:
    total = sum(counters.values())
    print()
    print("-" * 66)
    print("Summary")
    print("-" * 66)
    print(f"  Processed : {total}")
    print(f"  Changed   : {counters['OK']}")
    if counters["DRY-RUN"]:
        print(f"  Dry-run   : {counters['DRY-RUN']}")
    print(f"  Unchanged : {counters['NOCHG']}")
    print(f"  Skipped   : {counters['SKIP']}")
    if counters["SKIP"]:
        print(f"      (output was larger than input)")
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
        prog="terser-optimize",
        description=(
            "Minify .js / .mjs / .cjs files in place using terser. "
            "With no paths, the current directory is walked recursively."
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
        help="Do not print terser's own stdout/stderr; only summary lines.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run and validate, but do not modify any files.",
    )
    parser.add_argument(
        "--mangle",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass --mangle to terser (default: on). Use --no-mangle to skip.",
    )
    parser.add_argument(
        "--compress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=("Pass --compress to terser (default: on). Use --no-compress to skip."),
    )
    parser.add_argument(
        "--toplevel",
        action="store_true",
        help=(
            "Pass --toplevel to terser (mangle/compress top-level names). "
            "Requires --mangle or --compress to have any effect."
        ),
    )
    parser.add_argument(
        "--module",
        action="store_true",
        help="Pass --module to terser (treat input as an ES module).",
    )
    parser.add_argument(
        "--ecma",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Pass --ecma N to terser (target ECMAScript version, e.g. 5, "
            "2015, 2020). Omitted by default so terser picks its own."
        ),
    )
    parser.add_argument(
        "--terser",
        default="terser",
        metavar="PATH",
        help="Path to the terser executable (default: search PATH).",
    )
    parser.add_argument(
        "--terser-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument to pass to terser; may be repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Per-file terser timeout (default: {DEFAULT_TIMEOUT:g}s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    roots = [Path(p) for p in args.paths] if args.paths else [Path()]
    worker = partial(
        process_file,
        terser=args.terser,
        mangle=args.mangle,
        compress=args.compress,
        toplevel=args.toplevel,
        module=args.module,
        ecma=args.ecma,
        extra_args=tuple(args.terser_arg),
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    counters: dict[str, int] = {
        "OK": 0,
        "DRY-RUN": 0,
        "NOCHG": 0,
        "SKIP": 0,
        "ERROR": 0,
    }
    total_before = 0
    total_after = 0
    files = iter_js_files(roots)
    try:
        with Pool(processes=MAX_WORKERS) as pool:
            for result in pool.imap_unordered(worker, files, chunksize=IMAP_CHUNKSIZE):
                if not args.quiet:
                    _emit_terser_output(result)
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
