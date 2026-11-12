#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments and docstrings of Python files with LibCST.
Rewrites each file without its comments and docstrings while preserving the shebangs, the ``# type:`` hints and the ``# fmt:`` markers, validates the syntax and processes the targets in a pool.
Usage: grmcst [paths ...] Positional arguments: paths ...
Target files or directories to process.
Defaults to '.' if empty.
Examples: $ grmcst src/ Command-line script: run ``python grmcst.py`` or the ``grmcst`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Sequence
from multiprocessing import Pool
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Final

from dh import MAX_WORKERS
import libcst as cst

if TYPE_CHECKING:
    from multiprocessing.pool import AsyncResult
PRESERVE_PREFIXES: Final[tuple[str, ...]] = ("#!", "# type:", "# fmt:")
PRESERVE_EXACT: Final[frozenset[str]] = frozenset({"# fmt: skip", "# fmt: on", "# fmt: off"})


def should_preserve_comment(comment_text: str) -> bool:
    stripped: str = comment_text.strip()
    return stripped.startswith(PRESERVE_PREFIXES) or stripped in PRESERVE_EXACT


class StripTransformer(cst.CSTTransformer):
    def leave_Comment(
        self, original_node: cst.Comment, updated_node: cst.Comment
    ) -> cst.BaseLeaf | cst.RemovalSentinel:
        if should_preserve_comment(updated_node.value):
            return updated_node
        return cst.RemovalSentinel.REMOVE

    @staticmethod
    def _strip_leading_string(
        body: Sequence[cst.BaseStatement],
    ) -> tuple[cst.BaseStatement, ...]:
        if not body:
            return tuple(body)
        first: cst.BaseStatement = body[0]
        if (
            isinstance(first, cst.SimpleStatementLine)
            and len(first.body) == 1
            and isinstance(first.body[0], cst.Expr)
            and isinstance(first.body[0].value, cst.SimpleString)
        ):
            return tuple(body[1:])
        return tuple(body)

    def _strip_suite(self, suite: cst.BaseSuite) -> cst.BaseSuite:
        if isinstance(suite, cst.IndentedBlock):
            new_body: tuple[cst.BaseStatement, ...] = self._strip_leading_string(suite.body)
            if not new_body:
                new_body = (cst.SimpleStatementLine(body=[cst.Pass()]),)
            return suite.with_changes(body=new_body)
        if isinstance(suite, cst.SimpleStatementSuite):
            new_body: tuple[cst.BaseStatement, ...] = self._strip_leading_string(suite.body)
            if not new_body:
                new_body = (cst.Pass(),)
            return suite.with_changes(body=new_body)
        return suite

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        new_body: tuple[cst.BaseStatement, ...] = self._strip_leading_string(updated_node.body)
        return updated_node.with_changes(body=new_body)

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        return updated_node.with_changes(body=self._strip_suite(updated_node.body))

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        return updated_node.with_changes(body=self._strip_suite(updated_node.body))


def process_file(file_path: Path) -> str:
    try:
        source: str = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"[ERROR] Failed to read {file_path}: {exc}"
    try:
        module: cst.Module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return f"[ERROR] Failed to parse {file_path}: {exc}"
    try:
        transformer: StripTransformer = StripTransformer()
        new_module: cst.Module = module.visit(transformer)
    except Exception as exc:
        return f"[ERROR] Transformation failed for {file_path}: {exc}"
    final_code: str = new_module.code
    if final_code == source:
        return f"[SKIPPED] No structural modifications needed for {file_path}"
    try:
        ast.parse(final_code, filename=str(file_path))
    except SyntaxError as exc:
        return f"[WARNING] Validation failed for {file_path} (Changes rejected): {exc}"
    try:
        file_path.write_text(final_code, encoding="utf-8")
        return f"[SUCCESS] Processed and stripped: {file_path}"
    except Exception as exc:
        return f"[ERROR] Failed to save updates to {file_path}: {exc}"


def gather_files(inputs: list[str]) -> list[Path]:
    files: set[Path] = set()
    if not inputs:
        files.update(Path().rglob("*.py"))
        return sorted(files)
    for item in inputs:
        p: Path = Path(item)
        if p.is_file() and p.suffix == ".py":
            files.add(p)
        elif p.is_dir():
            files.update(p.rglob("*.py"))
    return sorted(files)


def main() -> None:
    parser = argparse.ArgumentParser(description="Strip comments and docstrings using libcst safely.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Target files or directories to process. Defaults to '.' if empty.",
    )
    args: argparse.Namespace = parser.parse_args()
    targets: list[Path] = gather_files(args.paths)
    if not targets:
        print("No target Python source files detected.")
        sys.exit(0)
    print(f"Queue loaded. Processing {len(targets)} target files via Parallel Pipeline...")
    with Pool(processes=MAX_WORKERS) as pool:
        async_results: list[AsyncResult[str]] = [pool.apply_async(process_file, (target,)) for target in targets]
        for async_res in async_results:
            result_string: str = async_res.get()
            print(result_string)


if __name__ == "__main__":
    raise SystemExit(main())
