#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python source files and wheel archives."""

import argparse
import ast
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
import re
import shutil
import tempfile
from typing import Final
import zipfile

from dh import SKIP_DIRS
from loguru import logger


POOL_SIZE: Final[int] = 8


@dataclass
class FileResult:
    path: str
    is_error: bool = False
    error_message: str = ""
    comments_removed: int = 0
    docstrings_removed: int = 0
    is_wheel_member: bool = False


@dataclass
class ProcessingStats:
    total_files: int = 0
    changed_files: int = 0
    comments_removed: int = 0
    docstrings_removed: int = 0
    errors: int = 0
    results: list[FileResult] = field(default_factory=list)


class CommentRemover:
    def __init__(self, source: str) -> None:
        self.source: str = source
        self.lines: list[str] = source.split("\n")
        self.comments_removed: int = 0

    def remove_comments(self) -> str:
        result_lines: list[str] = []
        for line in self.lines:
            processed_line, removed = self._process_line(line)
            result_lines.append(processed_line)
            self.comments_removed += removed
        return "\n".join(result_lines)

    def _process_line(self, line: str) -> tuple[str, int]:
        if self._is_shebang(line):
            return (line, 0)
        if self._is_encoding_declaration(line):
            return (line, 0)
        if self._is_type_comment(line):
            return (line, 0)
        processed = self._strip_inline_comment(line)
        removed = 1 if processed != line and processed.strip() else 0
        if removed and (not processed.strip()):
            return ("", 1)
        return (processed, removed)

    @staticmethod
    def _is_shebang(line: str) -> bool:
        return line.startswith("#!")

    @staticmethod
    def _is_encoding_declaration(line: str) -> bool:
        return re.match(r"#.*?coding[:=]\s*([-\w.]+)", line) is not None

    @staticmethod
    def _is_type_comment(line: str) -> bool:
        return "# type:" in line or "# noqa" in line or "# pragma" in line or ("# pylint" in line)

    @staticmethod
    def _strip_inline_comment(line: str) -> str:
        result: list[str] = []
        i = 0
        in_string = False
        string_char: str | None = None
        in_triple = False
        while i < len(line):
            if i + 2 < len(line):
                triple = line[i : i + 3]
                if triple in ('"""', "'''"):
                    if in_triple and string_char == triple:
                        in_triple = False
                        result.append(triple)
                        i += 3
                        continue
                    elif not in_string and (not in_triple):
                        in_triple = True
                        string_char = triple
                        result.append(triple)
                        i += 3
                        continue
            char = line[i]
            if char in ('"', "'") and (not in_triple):
                if in_string and string_char == char:
                    if i > 0 and line[i - 1] != "\\":
                        in_string = False
                        string_char = None
                elif not in_string:
                    in_string = True
                    string_char = char
                result.append(char)
                i += 1
                continue
            if char == "#" and (not in_string) and (not in_triple):
                break
            result.append(char)
            i += 1
        return "".join(result).rstrip()


class DocstringRemover(ast.NodeTransformer):
    def __init__(self, remove_module_docstring: bool = False) -> None:
        self.remove_module_docstring: bool = remove_module_docstring
        self.docstrings_removed: int = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        if (
            self._has_docstring(node)
            and (not self.remove_module_docstring or not self._is_module_level(node))
            and (
                isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            )
        ):
            self.docstrings_removed += 1
            node.body = node.body[1:]
            if not node.body:
                node.body = [ast.Pass()]
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        if (
            self._has_docstring(node)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            self.docstrings_removed += 1
            node.body = node.body[1:]
            if not node.body:
                node.body = [ast.Pass()]
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        if (
            self._has_docstring(node)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            self.docstrings_removed += 1
            node.body = node.body[1:]
            if not node.body:
                node.body = [ast.Pass()]
        self.generic_visit(node)
        return node

    def visit_Module(self, node: ast.Module) -> ast.Module:
        if (
            self.remove_module_docstring
            and self._has_docstring(node)
            and (
                isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            )
        ):
            self.docstrings_removed += 1
            node.body = node.body[1:]
        self.generic_visit(node)
        return node

    @staticmethod
    def _has_docstring(node: ast.AST) -> bool:
        return (
            bool(getattr(node, "body", []))
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        )

    @staticmethod
    def _is_module_level(node: ast.AST) -> bool:
        return False


def _remove_docstrings_from_source(source: str, remove_module_docstring: bool) -> tuple[str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    docstring_lines: set[int] = set()

    def _collect_docstring_lines(node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if not body:
            return
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            start = first.lineno
            end = getattr(first, "end_lineno", start)
            for line_no in range(start, end + 1):
                docstring_lines.add(line_no)

    if remove_module_docstring:
        _collect_docstring_lines(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _collect_docstring_lines(node)
    if not docstring_lines:
        return source, 0
    lines = source.split("\n")
    removed_count = 0
    keep_lines: list[str] = []
    skip_until: int = -1
    for idx, line in enumerate(lines, start=1):
        if idx <= skip_until:
            continue
        if idx in docstring_lines:
            skip_until = idx
            removed_count += 1
            continue
        keep_lines.append(line)
    result = "\n".join(keep_lines)
    try:
        new_tree = ast.parse(result)
    except SyntaxError:
        return result, removed_count
    needs_pass = False
    for node in ast.walk(new_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.body:
            needs_pass = True
            break
        if isinstance(node, ast.Module) and not node.body:
            needs_pass = True
            break
    if not needs_pass:
        return result, removed_count
    lines = result.split("\n")
    insertions: list[tuple[int, str]] = []
    for node in ast.walk(new_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.body:
            insert_at = getattr(node, "lineno", 1)
            indent = " " * (len(lines[insert_at - 1]) - len(lines[insert_at - 1].lstrip()))
            insertions.append((insert_at, f"{indent}    pass"))
        if isinstance(node, ast.Module) and not node.body:
            insertions.append((0, "pass"))
    for line_no, text in sorted(insertions, reverse=True):
        lines.insert(line_no, text)
    return "\n".join(lines), removed_count


def process_single_file(path: Path, remove_module_docstring: bool = False, dry_run: bool = False) -> FileResult:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            original_source = f.read()
        comment_remover = CommentRemover(original_source)
        no_comments = comment_remover.remove_comments()
        processed_source, docstrings_removed = _remove_docstrings_from_source(no_comments, remove_module_docstring)
        try:
            ast.parse(processed_source)
        except SyntaxError as e:
            return FileResult(
                path=str(path),
                is_error=True,
                error_message=f"Validation error: {e}",
            )
        if processed_source != original_source and (not dry_run):
            try:
                temp_fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp.", suffix=".py")
                try:
                    with open(temp_fd, "w", encoding="utf-8") as f:
                        f.write(processed_source)
                    shutil.move(temp_path, path)
                except Exception:
                    if Path(temp_path).exists():
                        Path(temp_path).unlink()
                    raise
            except Exception as e:
                return FileResult(path=str(path), is_error=True, error_message=f"Write error: {e}")
        return FileResult(
            path=str(path),
            comments_removed=comment_remover.comments_removed,
            docstrings_removed=docstrings_removed,
        )
    except Exception as e:
        return FileResult(path=str(path), is_error=True, error_message=str(e))


def process_wheel_file(
    wheel_path: Path, remove_module_docstring: bool = False, dry_run: bool = False
) -> list[FileResult]:
    results: list[FileResult] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        wheel_name = wheel_path.name
        try:
            with zipfile.ZipFile(wheel_path, "r") as whl:
                whl.extractall(temp_path)
            any_changed = False
            for py_file in temp_path.rglob("*.py"):
                result = process_single_file(py_file, remove_module_docstring, dry_run=True)
                if result.comments_removed > 0 or result.docstrings_removed > 0:
                    result = process_single_file(py_file, remove_module_docstring, dry_run=dry_run)
                    any_changed = True
                relative = py_file.relative_to(temp_path)
                result.path = f"{wheel_name}::{relative}"
                result.is_wheel_member = True
                results.append(result)
            if any_changed and (not dry_run):
                temp_wheel = temp_path / f"{wheel_name}.tmp"
                with zipfile.ZipFile(temp_wheel, "w", zipfile.ZIP_DEFLATED) as whl:
                    for path in temp_path.rglob("*"):
                        if path.is_file():
                            relative = path.relative_to(temp_path)
                            whl.write(path, arcname=str(relative))
                shutil.move(str(temp_wheel), str(wheel_path))
        except Exception as e:
            results.append(
                FileResult(
                    path=wheel_name,
                    is_error=True,
                    error_message=f"Wheel processing error: {e}",
                )
            )
    return results


def _worker_process_file(args: tuple[Path, bool, bool]) -> FileResult:
    path, remove_module_docstring, dry_run = args
    return process_single_file(path, remove_module_docstring, dry_run)


def discover_files(start_path: str) -> tuple[list[Path], list[Path]]:
    start = Path(start_path).resolve()
    if not start.exists():
        logger.error(f"Path not found: {start}")
        return ([], [])
    python_files: list[Path] = []
    wheel_files: list[Path] = []
    if start.is_file():
        if start.suffix == ".py":
            python_files.append(start)
        elif start.suffix == ".whl":
            wheel_files.append(start)
        return (python_files, wheel_files)
    for root, dirs, files in start.walk(top_down=True):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        root_path = Path(root)
        for filename in files:
            path = root_path / filename
            if path.suffix == ".py":
                python_files.append(path)
            elif path.suffix == ".whl":
                wheel_files.append(path)
    return (python_files, wheel_files)


def print_header(python_count: int, wheel_count: int) -> None:
    print(f"Found: {python_count} Python files, {wheel_count} wheel files")


def print_results(stats: ProcessingStats, base_dir: Path) -> None:
    results = sorted(stats.results, key=lambda r: r.path)
    for result in results:
        if result.is_error:
            logger.error(f"✗ {result.path}")
            logger.error(f"  Error: {result.error_message}")
        elif result.comments_removed == 0 and result.docstrings_removed == 0:
            print(f"○ {result.path} (no change)")
        else:
            changes: list[str] = []
            if result.comments_removed > 0:
                changes.append(f"{result.comments_removed} comment{('s' if result.comments_removed != 1 else '')}")
            if result.docstrings_removed > 0:
                changes.append(
                    f"{result.docstrings_removed} docstring{('s' if result.docstrings_removed != 1 else '')}"
                )
            print(f"✓ {result.path} ({', '.join(changes)} removed)")


def print_summary(stats: ProcessingStats) -> None:
    print("=" * 40)
    print("Summary:")
    print(f"  Total files processed: {stats.total_files}")
    print(f"  Files changed: {stats.changed_files}")
    print(f"  Total comments removed: {stats.comments_removed}")
    print(f"  Total docstrings removed: {stats.docstrings_removed}")
    if stats.errors > 0:
        print(f"  Errors: {stats.errors}")
    print("=" * 40)


def _accumulate_result(stats: ProcessingStats, result: FileResult) -> None:
    stats.results.append(result)
    if result.is_error:
        stats.errors += 1
    elif result.comments_removed > 0 or result.docstrings_removed > 0:
        stats.changed_files += 1
        stats.comments_removed += result.comments_removed
        stats.docstrings_removed += result.docstrings_removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip comments and docstrings from Python source files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\nExamples:\n\n  python strip_comments.py\n\n\n  python strip_comments.py src/main.py\n\n\n  python strip_comments.py --remove-module-docstring\n\n\n  python strip_comments.py --dry-run\n        ",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="File or directory to process (default: current directory)",
    )
    parser.add_argument(
        "--remove-module-docstring",
        action="store_true",
        help="Also strip module-level docstrings (preserved by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    args = parser.parse_args()
    python_files, wheel_files = discover_files(args.path)
    if not python_files and (not wheel_files):
        logger.error("No Python files found")
        return 1
    print_header(len(python_files), len(wheel_files))
    stats = ProcessingStats(total_files=len(python_files) + len(wheel_files))
    for wheel_file in wheel_files:
        results = process_wheel_file(
            wheel_file,
            remove_module_docstring=args.remove_module_docstring,
            dry_run=args.dry_run,
        )
        for result in results:
            _accumulate_result(stats, result)
    if python_files:
        tasks: list[tuple[Path, bool, bool]] = [
            (path, args.remove_module_docstring, args.dry_run) for path in python_files
        ]
        with Pool(processes=POOL_SIZE) as pool:
            async_results = [pool.apply_async(_worker_process_file, (t,)) for t in tasks]
            processed = 0
            for async_result in async_results:
                result = async_result.get()
                _accumulate_result(stats, result)
                processed += 1
                if processed % 10 == 0:
                    print(f"  Processed: {processed}/{len(python_files)}")
        print(f"  Processed: {len(python_files)}/{len(python_files)}")
    base_dir = Path(args.path).resolve()
    print_results(stats, base_dir)
    print_summary(stats)
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
