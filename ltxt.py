#!/data/data/com.termux/files/usr/bin/env python
"""Count the unique non-blank lines of text files, per extension.
Walks the given paths (or the current directory), skips binaries by extension and counts the stripped lines that appear more than once, grouping the counts per file extension.
``--pool-method`` selects how the counting jobs are spread over the worker pool.
Usage: ltxt [paths ...] [--pool-method map|starmap|imap_unordered|apply_async] Options: --pool-method Multiprocessing pool method to use for line counting.
Examples: $ ltxt words/ Command-line script: run ``python ltxt.py`` or the ``ltxt`` command.
The module is importable as well."""

import argparse
from collections import Counter
from collections.abc import Sequence
from multiprocessing.pool import AsyncResult, Pool
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from dh import BINARY_EXT, MAX_WORKERS
from loguru import logger


EXCLUDED_EXT: frozenset[str] = frozenset(BINARY_EXT)
POOL_METHODS: tuple[str, ...] = ("map", "starmap", "imap_unordered", "apply_async")
LineCounter: TypeAlias = Counter[str]


def process_file(path: Path) -> LineCounter:
    counter: LineCounter = Counter()
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line:
                    counter[line] += 1
    except Exception as exc:
        logger.error(f"Error reading {path}: {exc}")
    return counter


def collect_files_by_extension() -> dict[str, list[Path]]:
    ext_map: dict[str, list[Path]] = {}
    cwd = Path.cwd()
    for root, _dirnames, filenames in cwd.walk():
        for fname in filenames:
            if fname.startswith("."):
                continue
            path = root / fname
            ext = path.suffix
            if ext in EXCLUDED_EXT:
                continue
            if not ext:
                ext = ".no_ext"
            ext_map.setdefault(ext, []).append(path)
    return ext_map


def process_paths(paths: Sequence[Path], method: str) -> list[LineCounter]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "map":
            return pool.map(process_file, paths)
        if method == "starmap":
            return pool.starmap(process_file, [(path,) for path in paths])
        if method == "imap_unordered":
            return list(pool.imap_unordered(process_file, paths))
        if method == "apply_async":
            async_results: list[AsyncResult[LineCounter]] = [pool.apply_async(process_file, (path,)) for path in paths]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def collect_lines_for_extension(ext: str, files: Sequence[Path], pool_method: str) -> None:
    if not files:
        return
    global_counter: LineCounter = Counter()
    logger.info(f"Processing {len(files)} files with extension '{ext}'")
    for result in process_paths(files, pool_method):
        global_counter.update(result)
    output_name = ext.lstrip(".") or "no_ext"
    output_file = Path(f"{output_name}.txt")
    written_lines = 0
    with output_file.open("w", encoding="utf-8") as fo:
        for line, count in global_counter.most_common():
            if count >= 2:
                fo.write(line + "\n")
                written_lines += 1
    logger.info(f"Saved {written_lines} duplicate lines to {output_file}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="imap_unordered",
        help="Multiprocessing pool method to use for line counting.",
    )
    return parser.parse_args()


def main() -> None:
    args: argparse.Namespace = parse_args()
    pool_method: str = args.pool_method
    ext_map = collect_files_by_extension()
    if not ext_map:
        logger.info("No eligible files found.")
        return
    for ext, files in ext_map.items():
        collect_lines_for_extension(ext, files, pool_method)


if __name__ == "__main__":
    raise SystemExit(main())
