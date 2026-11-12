#!/data/data/com.termux/files/usr/bin/env python
"""Format shell scripts with ``shfmt`` in parallel.
Formats the files given on the command line or, without arguments, every file below the current directory that either ends in ``.sh`` or starts with a shell shebang.
Files that fail to format are listed and, with ``--move``, copied into an ``error`` directory.
Usage: fmt_bash [options] [paths ...] Positional arguments: paths ...
Optional file(s) to format.
A single file is formatted directly (no Pool); the file does not need a .sh extension.
If omitted, shell files under CWD are used.
Options: --pool-method Multiprocessing pool method to use for formatting (default: starmap).
-m, --move Examples: $ fmt_bash scripts/ $ fmt_bash setup.sh --move Command-line script: run ``python fmt_bash.py`` or the ``fmt_bash`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
import contextlib
from multiprocessing.pool import AsyncResult, Pool
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Final, TypeAlias

from dh import MAX_WORKERS, get_files, is_binary, runcmd
from loguru import logger

POOL_METHODS: Final[tuple[str, ...]] = (
    "starmap",
    "map",
    "imap_unordered",
    "apply_async",
)
SHEBANG_READ_BYTES: Final[int] = 256
ERROR_DIR_NAME: Final[str] = "error"
FormatResult: TypeAlias = tuple[bool, str]


def has_shell_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            first = f.readline(SHEBANG_READ_BYTES).decode("utf-8", errors="ignore").strip()
        return first.startswith("#!") and ("bash" in first or "sh" in first or "zsh" in first)
    except OSError:
        return False


def is_shell_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix in {".zsh", ".sh"} or (not path.suffix and has_shell_shebang(path)):
        return not is_binary(path)
    return False


def is_formattable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return not is_binary(path)


def process_file(path_str: str) -> FormatResult:
    path = Path(path_str)
    logger.info(f"Formatting:  {path.name}")
    res_code, _, stderr = runcmd(["shfmt", "-w", str(path)], show_output=True)
    if res_code != 0:
        logger.error(f"shfmt failed on {path.name}: {stderr.strip()}")
        return (False, path_str)
    return (True, path_str)


def _process_file_tuple(item: tuple[str]) -> FormatResult:
    return process_file(item[0])


def _run_pool(paths: Sequence[str], method: str) -> list[FormatResult]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "starmap":
            return pool.starmap(process_file, [(p,) for p in paths])
        if method == "map":
            return pool.map(_process_file_tuple, [(p,) for p in paths])
        if method == "imap_unordered":
            return list(pool.imap_unordered(_process_file_tuple, [(p,) for p in paths]))
        if method == "apply_async":
            async_results: list[AsyncResult[FormatResult]] = [
                pool.apply_async(_process_file_tuple, ((p,),)) for p in paths
            ]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def collect_shell_files(cwd: Path) -> list[Path]:
    files = [p for p in get_files(cwd) if (not p.suffix and has_shell_shebang(p)) or p.suffix == ".sh"]
    return [p for p in files if not is_binary(p)]


def move_failed_files(failed: Sequence[Path], cwd: Path) -> list[Path]:
    error_dir: Path = cwd / ERROR_DIR_NAME
    error_dir.mkdir(exist_ok=True)
    moved: list[Path] = []
    for src in failed:
        try:
            src_resolved = src.resolve()
        except OSError:
            src_resolved = src
        if error_dir.resolve() in src_resolved.parents:
            logger.warning(f"Skipping move (already in error dir): {src}")
            continue
        if not src.exists():
            logger.warning(f"Skipping move (missing): {src}")
            continue
        dest: Path = error_dir / src.name
        if dest.exists():
            stem: str = src.stem
            suffix: str = src.suffix
            counter: int = 1
            while dest.exists():
                dest = error_dir / f"{stem}.{counter}{suffix}"
                counter += 1
        try:
            shutil.move(str(src), str(dest))
            logger.info(f"Moved to error dir: {src} -> {dest}")
            moved.append(dest)
        except OSError as exc:
            logger.error(f"Failed to move {src} to {dest}: {exc}")
    return moved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional file(s) to format. A single file is formatted directly "
        "(no Pool); the file does not need a .sh extension. If omitted, "
        "shell files under CWD are used.",
    )
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="imap_unordered",
        help="Multiprocessing pool method to use for formatting (default: starmap).",
    )
    parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help=f"Move files that failed formatting into the '{ERROR_DIR_NAME}' subdirectory under CWD.",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    pool_method: str = args.pool_method
    move_errors: bool = args.move
    cwd: Path = Path.cwd()
    if len(args.paths) == 1:
        single = Path(args.paths[0])
        if not is_formattable_file(single):
            logger.warning(f"Not a formattable file (missing or binary), skipping: {single}")
            return 0
        logger.info(f"Single file mode: formatting {single.name} directly.")
        success, p_str = process_file(str(single))
        if not success:
            failed_path = Path(p_str)
            with contextlib.suppress(ValueError):
                failed_path = failed_path.relative_to(cwd)
            if move_errors:
                move_failed_files([failed_path], cwd)
        return 0
    if args.paths:
        non_binary_files: list[Path] = [Path(p) for p in args.paths if is_shell_file(Path(p))]
    else:
        non_binary_files = collect_shell_files(cwd)
    if not non_binary_files:
        logger.warning("No shell files found to format.")
        return 0
    file_strings: list[str] = [str(f) for f in non_binary_files]
    logger.info(f"Processing {len(file_strings)} files...")
    results: list[FormatResult] = _run_pool(file_strings, pool_method)
    failed: list[Path] = []
    for success, p_str in results:
        if not success:
            try:
                failed.append(Path(p_str).relative_to(cwd))
            except ValueError:
                failed.append(Path(p_str))
    if failed:
        logger.warning("Failed files:")
        for f in failed:
            logger.warning(f"  - {f}")
        if move_errors:
            move_failed_files(failed, cwd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
