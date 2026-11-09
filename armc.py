#!/data/data/com.termux/files/usr/bin/env python
"""Recursively strip the comments from Python files with tree-sitter.
Removes the comment nodes (keeping the shebang), verifies with ast that the result still parses and no comment is left, and processes the files of the current directory or of the given paths in a pool, logging a per-file result.
Usage: armc [paths ...] Positional arguments: paths ...
Files or directories to scan (defaults to recursively checking current directory).
Examples: $ armc src/ Command-line script: run ``python armc.py`` or the ``armc`` command.
The module is importable as well."""

import argparse
import ast
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Any, Iterable, NamedTuple

from loguru import logger
from tree_sitter import Language, Parser
import tree_sitter_python as tspython


logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:7}</level> | <level>{message}</level>",
    level="INFO",
)
PY_LANGUAGE = Language(tspython.language())


class ProcessResult(NamedTuple):
    path: Path
    removed_count: int
    unremoved_count: int
    modified: bool
    error: str | None = None


def is_shebang(byte_content: bytes, node: Any) -> bool:
    return node.start_point[0] == 0 and byte_content.startswith(b"#!")


def remove_comments_from_code(source_bytes: bytes) -> tuple[bytes, int]:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source_bytes)
    comment_nodes = []
    cursor = tree.walk()

    def visit_node() -> None:
        if cursor.node.type == "comment":
            if not is_shebang(source_bytes, cursor.node):
                comment_nodes.append(cursor.node)
            return
        if cursor.goto_first_child():
            while True:
                visit_node()
                if not cursor.goto_next_sibling():
                    break
            cursor.goto_parent()

    visit_node()
    if not comment_nodes:
        return source_bytes, 0
    comment_nodes.sort(key=lambda n: n.start_byte, reverse=True)
    buffer = bytearray(source_bytes)
    for node in comment_nodes:
        del buffer[node.start_byte : node.end_byte]
    return bytes(buffer), len(comment_nodes)


def check_remaining_comments(source_bytes: bytes) -> int:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source_bytes)
    remaining = 0
    cursor = tree.walk()

    def count_comments() -> None:
        nonlocal remaining
        if cursor.node.type == "comment":
            if not is_shebang(source_bytes, cursor.node):
                remaining += 1
            return
        if cursor.goto_first_child():
            while True:
                count_comments()
                if not cursor.goto_next_sibling():
                    break
            cursor.goto_parent()

    count_comments()
    return remaining


def process_single_file(path: Path) -> ProcessResult:
    try:
        source_bytes = path.read_bytes()
        cleaned_bytes, removed_count = remove_comments_from_code(source_bytes)
        if removed_count == 0:
            return ProcessResult(path, 0, 0, False)
        try:
            ast.parse(cleaned_bytes, filename=str(path))
        except SyntaxError as e:
            return ProcessResult(
                path,
                0,
                0,
                False,
                error=f"AST validation failed after stripping comments: {e}",
            )
        unremoved_count = check_remaining_comments(cleaned_bytes)
        path.write_bytes(cleaned_bytes)
        return ProcessResult(path, removed_count, unremoved_count, True)
    except Exception as exc:
        return ProcessResult(path, 0, 0, False, error=str(exc))


def collect_python_files(inputs: list[str]) -> list[Path]:
    files: set[Path] = set()
    if not inputs:
        inputs = ["."]
    for item in inputs:
        p = Path(item).resolve()
        if p.is_file() and p.suffix == ".py":
            files.add(p)
        elif p.is_dir():
            files.update(f for f in p.rglob("*.py") if f.is_file())
        else:
            logger.warning(f"Skipping invalid target or non-Python path: {item}")
    return list(files)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recursively strip comments from Python files in-place using Tree-sitter."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan (defaults to recursively checking current directory).",
    )
    args = parser.parse_args()
    targets = collect_python_files(args.paths)
    if not targets:
        print("No Python files found to process.")
        return
    print(f"Dispatched {len(targets)} target files to 8 multiprocessing workers...")
    async_results = []
    with mp.Pool(processes=8) as pool:
        for path in targets:
            res = pool.apply_async(process_single_file, args=(path,))
            async_results.append(res)
        pool.close()
        pool.join()
    total_removed = 0
    total_modified = 0
    for async_res in async_results:
        res: ProcessResult = async_res.get()
        if res.error:
            logger.error(f"Error processing {res.path}: {res.error}")
            continue
        if res.modified:
            total_modified += 1
            total_removed += res.removed_count
            print(f"Modified: {res.path} | Removed comments: {res.removed_count}")
            if res.unremoved_count > 0:
                logger.warning(f"Lingering comments detected in {res.path}: {res.unremoved_count} remain.")
    print("--- Execution Summary ---")
    print(f"Total files scanned:  {len(targets)}")
    print(f"Files modified:        {total_modified}")
    print(f"Comments stripped:     {total_removed}")


if __name__ == "__main__":
    main()
