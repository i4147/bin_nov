#!/data/data/com.termux/files/usr/bin/env python
"""Diff two files line by line, optionally ignoring comments and docstrings.

With -s, strips leading/trailing whitespace (and comments and docstrings from .py files via libcst) before comparing; -m moves identical pairs into a group_N directory.
"""

import argparse
import hashlib
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import libcst as cst
from dh import MAX_WORKERS, cprint, read_lines


def count_lines(path: Path) -> int:
    return path.read_bytes().count(b"\n") + 1


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class StripPythonTransformer(cst.CSTTransformer):
    def leave_Comment(
        self, original_node: cst.Comment, updated_node: cst.Comment
    ) -> cst.RemovalSentinel:
        return cst.RemoveFromParent()

    def leave_EmptyLine(
        self, original_node: cst.EmptyLine, updated_node: cst.EmptyLine
    ) -> cst.RemovalSentinel:
        return cst.RemoveFromParent()

    def _is_docstring(self, stmt: cst.BaseStatement) -> bool:
        if not isinstance(stmt, cst.SimpleStatementLine):
            return False
        if len(stmt.body) != 1:
            return False
        expr = stmt.body[0]
        if not isinstance(expr, cst.Expr):
            return False
        return isinstance(expr.value, (cst.SimpleString, cst.ConcatenatedString))

    def _strip_docstring(
        self, node: cst.FunctionDef | cst.ClassDef
    ) -> cst.FunctionDef | cst.ClassDef:
        body = node.body
        if not isinstance(body, cst.IndentedBlock):
            return node
        stmts = body.body
        if not stmts or not self._is_docstring(stmts[0]):
            return node
        new_stmts = stmts[1:]
        if not new_stmts:
            new_stmts = (cst.SimpleStatementLine(body=[cst.Pass()]),)
        return node.with_changes(body=body.with_changes(body=new_stmts))

    def leave_Module(
        self, original_node: cst.Module, updated_node: cst.Module
    ) -> cst.Module:
        body = updated_node.body
        if body and self._is_docstring(body[0]):
            return updated_node.with_changes(body=body[1:])
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        return self._strip_docstring(updated_node)

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        return self._strip_docstring(updated_node)


def strip_python_source(source: str) -> str:
    module = cst.parse_module(source)
    return module.visit(StripPythonTransformer()).code


def read_file_task(path: Path, use_mmap: bool, strip: bool) -> tuple[Path, list[str]]:
    if strip and path.suffix == ".py":
        try:
            source = path.read_text(encoding="utf-8")
            raw_lines = strip_python_source(source).splitlines()
        except (cst.ParserSyntaxError, UnicodeDecodeError, ValueError):
            raw_lines = read_lines(path, ke=False)
    else:
        raw_lines = read_lines(path, ke=False)
    if strip:
        raw_lines = [line.strip() for line in raw_lines]
    return path, [line for line in raw_lines if line]


def filter_diff_chunk(
    chunk: list[str], exclude_set: frozenset[str], mode: str
) -> list[str]:
    if mode == "only_in_first":
        return [p for p in chunk if p not in exclude_set]
    else:
        return [p for p in chunk if p in exclude_set]


def next_group_dir(base: Path) -> Path:
    n = 1
    while (base / f"group_{n}").exists():
        n += 1
    return base / f"group_{n}"


def move_files_to_group(path1: Path, path2: Path) -> None:
    base = Path.cwd()
    target = next_group_dir(base)
    target.mkdir()
    shutil.move(str(path1.resolve()), str(target / path1.name))
    shutil.move(str(path2.resolve()), str(target / path2.name))
    cprint(f"moved to {target.name}/", color="magenta")


def report_diff_lines(
    path1: Path,
    path2: Path,
    strip: bool = False,
    move: bool = False,
    num_workers: int = 2,
) -> None:
    if file_hash(path1) == file_hash(path2) and not strip:
        cprint(f"{path1.name} and {path2.name} are identical", color="blue")
        if move:
            move_files_to_group(path1, path2)
        return
    lines1_count = count_lines(path1)
    lines2_count = count_lines(path2)
    use_mmap1 = lines1_count > 5000
    use_mmap2 = lines2_count > 5000
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future1 = executor.submit(read_file_task, path1, use_mmap1, strip)
        future2 = executor.submit(read_file_task, path2, use_mmap2, strip)
        _, lines1 = future1.result()
        _, lines2 = future2.result()
    set1 = set(lines1)
    set2 = set(lines2)
    if lines1_count > 10000 and lines2_count > 10000:
        chunk_size = max(1000, len(lines1) // num_workers)
        chunks = [lines1[i : i + chunk_size] for i in range(0, len(lines1), chunk_size)]
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(
                    filter_diff_chunk, chunk, frozenset(set2), "only_in_first"
                )
                for chunk in chunks
            ]
            only_in_first: list[str] = []
            for future in as_completed(futures):
                only_in_first.extend(future.result())
    else:
        only_in_first = [p for p in lines1 if p not in set2]
    only_in_second = [p for p in lines2 if p not in set1]
    common_count = len(set1 & set2)
    if move and strip and not only_in_first and not only_in_second:
        move_files_to_group(path1, path2)
    if only_in_first:
        cprint(f"only in {path1.name}:", color="cyan")
        for line in only_in_first[:150]:
            cprint(f"  - {line}", color="green")
        if len(only_in_first) > 150:
            cprint(f"  ... and {len(only_in_first) - 5} lines more", color="green")
    if only_in_second:
        cprint(f"only in {path2.name}:", color="cyan")
        for line in only_in_second[:150]:
            cprint(f"  - {line}", color="yellow")
        if len(only_in_second) > 150:
            cprint(f"  ... and {len(only_in_second) - 5} lines more", color="yellow")
    cprint(
        f"common lines: {common_count}\n"
        f"only in {path1.name}: {len(only_in_first)}\n"
        f"only in {path2.name}: {len(only_in_second)}",
        color="blue",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pydiff", description="Diff two files line by line"
    )
    parser.add_argument("file1", type=Path)
    parser.add_argument("file2", type=Path)
    parser.add_argument(
        "-s",
        "--strip",
        action="store_true",
        help="strip leading/trailing whitespace; for .py files also remove comments and docstrings",
    )
    parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="with -s, if files match, move both into a group_N subdir in the current folder",
    )
    args = parser.parse_args()
    report_diff_lines(
        args.file1,
        args.file2,
        strip=args.strip,
        move=args.move,
        num_workers=MAX_WORKERS,
    )


if __name__ == "__main__":
    raise SystemExit(main())
