#!/data/data/com.termux/files/usr/bin/env python
"""Swap ``import re`` for ``import regex as re`` across a tree.
Walks the Python files and rewrites the import lines (``-r`` reverses it, dropping the ``regex`` alias back to ``re``), reporting each file it touched from a process pool.
Usage: re2regex [-r] [--pool-method MAP] Options: -r, --reverse Reverse the replacement (regex as re -> re) --pool-method Multiprocessing pool method to use for updates.
Examples: $ re2regex $ re2regex -r Command-line script: run ``python re2regex.py`` or the ``re2regex`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from multiprocessing.pool import AsyncResult, Pool
from pathlib import Path
import re
from typing import TYPE_CHECKING, Final, TypeAlias

from dh import MAX_WORKERS
from loguru import logger


POOL_METHODS: Final[tuple[str, ...]] = (
    "map",
    "starmap",
    "imap_unordered",
    "apply_async",
)
NORMAL_IMPORT: Final[str] = r"^import re\b"
REGEX_IMPORT: Final[str] = r"^import regex as re\b"
FileResult: TypeAlias = str | None
UpdateTask: TypeAlias = tuple[Path, bool]


def get_pyfiles(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def update_file(file_path: Path, reverse: bool = False) -> FileResult:
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return f"Error processing {file_path}: {exc}"
    new_lines: list[str] = []
    changed = False
    search_pat = REGEX_IMPORT if reverse else NORMAL_IMPORT
    replacement = "import re" if reverse else "import regex as re"
    for line in lines:
        if not changed and re.match(search_pat, line):
            new_lines.append(re.sub(search_pat, replacement, line))
            changed = True
        else:
            new_lines.append(line)
    if not changed:
        return None
    try:
        file_path.write_text("".join(new_lines), encoding="utf-8")
    except OSError as exc:
        return f"Error processing {file_path}: {exc}"
    return f"Updated: {file_path}"


def _update_file_tuple(task: UpdateTask) -> FileResult:
    path, reverse = task
    return update_file(path, reverse)


def _run_pool(tasks: Sequence[UpdateTask], method: str) -> list[FileResult]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "map":
            return pool.map(_update_file_tuple, tasks)
        if method == "starmap":
            return pool.starmap(update_file, tasks)
        if method == "imap_unordered":
            return list(pool.imap_unordered(_update_file_tuple, tasks))
        if method == "apply_async":
            async_results: list[AsyncResult[FileResult]] = [
                pool.apply_async(_update_file_tuple, (task,)) for task in tasks
            ]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recursively swap 'import re' with 'import regex as re'")
    parser.add_argument(
        "-r",
        "--reverse",
        action="store_true",
        help="Reverse the replacement (regex as re -> re)",
    )
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="map",
        help="Multiprocessing pool method to use for updates.",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    reverse: bool = bool(args.reverse)
    pool_method: str = args.pool_method
    cwd = Path.cwd()
    py_files = get_pyfiles(cwd)
    logger.info(f"Scanning {len(py_files)} files...")
    tasks: list[UpdateTask] = [(path, reverse) for path in py_files]
    if not tasks:
        logger.info("Task complete. Files modified: 0")
        return 0
    results = _run_pool(tasks, pool_method)
    updates: list[str] = [r for r in results if r is not None]
    for msg in updates:
        logger.info(msg)
    logger.info(f"Task complete. Files modified: {len(updates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
