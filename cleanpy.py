#!/data/data/com.termux/files/usr/bin/env python
"""Remove the comments and docstrings of Python files recursively.
Uses LibCST to delete the comments and the class/function docstrings while preserving the shebangs, the ``# fmt``/``# type`` markers and the module docstrings (``-r`` removes those too), and processes the tree with eight workers.
Usage: cleanpy [-r] [paths ...] Positional arguments: paths ...
Files and/or directories to process (default: current directory) Options: -r, --remove-module-docstring Remove module-level docstrings too Examples: $ cleanpy src/ Command-line script: run ``python cleanpy.py`` or the ``cleanpy`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from dh import SKIP_DIRS
import libcst as cst
from libcst import RemovalSentinel
from libcst.metadata import MetadataWrapper, PositionProvider
from loguru import logger

PRESERVED_COMMENT_MARKERS: tuple[str, ...] = ("# fmt", "# type")
DEFAULT_POOL_SIZE: int = 8


@dataclass(slots=True)
class FileResult:
    path: str
    comments_removed: int = 0
    docstrings_removed: int = 0
    changed: bool = False
    error: str | None = None


def iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for input_path in paths:
        path: Path = input_path.expanduser()
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                if path.suffix == ".py":
                    resolved: Path = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield path
                continue
            if not path.is_dir():
                logger.warning(f"not found or unsupported: {path}")
                continue
            for child in path.rglob("*"):
                if not child.is_file() or child.suffix != ".py":
                    continue
                if child.is_symlink():
                    continue
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if any(part in SKIP_DIRS for part in child.parts):
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield child
        except OSError as exc:
            logger.warning(f"cannot traverse {path}: {exc}")


def _comment_should_be_preserved(text: str, is_first_line: bool) -> bool:
    stripped: str = text.lstrip()
    if is_first_line and stripped.startswith("#!"):
        return True
    lower: str = text.lower()
    return any(marker in lower for marker in PRESERVED_COMMENT_MARKERS)


class _Transformer(cst.CSTTransformer):
    def __init__(self, remove_module_docstring: bool) -> None:
        self.remove_module_docstring: bool = remove_module_docstring
        self.comments_removed: int = 0
        self.docstrings_removed: int = 0

    def leave_Comment(self, original_node: cst.Comment, updated_node: cst.Comment) -> cst.Comment | RemovalSentinel:
        pos = self.get_metadata(PositionProvider, original_node, None)
        is_first_line: bool = pos is not None and pos.start.line == 1
        if _comment_should_be_preserved(original_node.value, is_first_line):
            return updated_node
        self.comments_removed += 1
        return cst.RemoveFromParent()

    def _handle_docstring(
        self,
        body: cst.IndentedBlock,
        remove: bool,
    ) -> cst.IndentedBlock:
        if not remove:
            return body
        statements: list[cst.BaseStatement] = list(body.body)
        if not statements:
            return body
        first: cst.BaseStatement = statements[0]
        if not (
            isinstance(first, cst.SimpleStatementLine)
            and first.body
            and isinstance(first.body[0], cst.Expr)
            and isinstance(first.body[0].value, cst.SimpleString)
        ):
            return body
        self.docstrings_removed += 1
        if len(statements) == 1:
            replacement: cst.BaseStatement = cst.SimpleStatementLine(body=[cst.Pass()])
            new_statements: list[cst.BaseStatement] = [replacement]
        else:
            new_statements = statements[1:]
        return body.with_changes(body=new_statements)

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, True)
        return updated_node.with_changes(body=new_body)

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, True)
        return updated_node.with_changes(body=new_body)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, self.remove_module_docstring)
        return updated_node.with_changes(body=new_body)


def _remove_blank_lines(data: str) -> str:
    lines: list[str] = data.splitlines(keepends=True)
    result: list[str] = []
    for line in lines:
        if line.strip() == "":
            continue
        result.append(line)
    return "".join(result)


def process_file(path_str: str, remove_module_docstring: bool) -> FileResult:
    path: Path = Path(path_str)
    result: FileResult = FileResult(path=str(path))
    try:
        source_bytes: bytes = path.read_bytes()
        try:
            source: str = source_bytes.decode("utf-8")
        except UnicodeDecodeError:
            source = source_bytes.decode("utf-8-sig")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            result.error = f"SyntaxError: {exc}"
            return result
        module: cst.Module = cst.parse_module(source)
        wrapper: MetadataWrapper = MetadataWrapper(module)
        transformer: _Transformer = _Transformer(remove_module_docstring)
        try:
            updated_module: cst.Module = wrapper.visit(transformer)
        except Exception:
            transformer = _Transformer(remove_module_docstring)
            updated_module = module.visit(transformer)
        updated_text: str = updated_module.code
        updated_text = _remove_blank_lines(updated_text)
        if not updated_text.endswith("\n") and updated_text:
            updated_text += "\n"
        ast.parse(updated_text, filename=str(path))
        if updated_text == source:
            return result
        path.write_text(updated_text, encoding="utf-8")
        result.comments_removed = transformer.comments_removed
        result.docstrings_removed = transformer.docstrings_removed
        result.changed = True
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Remove Python comments and docstrings recursively, preserving "
            "shebangs, # fmt, # type, and module docstrings by default."
        )
    )
    parser.add_argument(
        "-r",
        "--remove-module-docstring",
        action="store_true",
        help="remove module-level docstrings too",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files and/or directories to process (default: current directory)",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    input_paths: list[Path] = args.paths or [Path()]
    files: list[Path] = list(iter_python_files(input_paths))
    if not files:
        print("No Python files found.")
        return 0
    changed_files: int = 0
    total_comments: int = 0
    total_docstrings: int = 0
    errors: int = 0
    pool: Pool = Pool(processes=DEFAULT_POOL_SIZE)
    try:
        async_results: list[tuple[Path, object]] = [
            (
                path,
                pool.apply_async(
                    process_file,
                    (str(path), args.remove_module_docstring),
                ),
            )
            for path in files
        ]
        pool.close()
        for path, async_result in async_results:
            try:
                result: FileResult = async_result.get()
            except Exception as exc:
                errors += 1
                logger.error(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if result.error:
                errors += 1
                logger.error(f"{result.path}: {result.error}")
                continue
            if result.changed:
                changed_files += 1
                total_comments += result.comments_removed
                total_docstrings += result.docstrings_removed
                print(
                    f"{result.path}: comments removed={result.comments_removed}, "
                    f"docstrings removed={result.docstrings_removed}"
                )
            else:
                print(f"{result.path}: no changes")
        pool.join()
    finally:
        pool.terminate()
    print(
        "Summary: "
        f"files changed={changed_files}, "
        f"comments removed={total_comments}, "
        f"docstrings removed={total_docstrings}, "
        f"errors={errors}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
