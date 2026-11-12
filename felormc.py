#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments and docstrings of Python files with LibCST.
Keeps the shebangs, the module docstrings and the pragma comments (``# type:``, ``# fmt:``, ``# noqa``), validates every result before writing and offers ``--dry-run``, ``--verbose`` and a spawn-based pool over the given paths or the current tree.
Usage: felormc [--dry-run] [--verbose] [paths ...] Positional arguments: paths ...
Files or directories to process.
Defaults to current directory (recursive).
Options: --dry-run Report what would change without writing any files.
--verbose Enable verbose (DEBUG-level) logging.
Examples: $ felormc --dry-run src/ Command-line script: run ``python felormc.py`` or the ``felormc`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Final, Iterable, Iterator, Optional

from dh import MAX_WORKERS
import libcst as cst
from libcst import BaseStatement, ClassDef, EmptyLine, FunctionDef, Module, SimpleStatementLine, TrailingWhitespace
from loguru import logger

PRAGMA_PREFIXES: Final[tuple[str, ...]] = ("# type:", "# fmt:", "# fmt: skip", "# noqa")
SHEBANG_PREFIX: Final[str] = "#!"
PY_EXT: Final[frozenset[str]] = frozenset({".py", ".pyi", ".pyw"})
PY_SHEBANG_HINTS: Final[tuple[str, ...]] = ("python",)


def configure_logging(verbose: bool = False) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{function}</cyan> - <level>{message}</level>"
        ),
        colorize=True,
        backtrace=False,
        diagnose=False,
    )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    path: Path
    success: bool
    changed: bool
    message: str = ""


def _looks_like_python_shebang(first_line: str) -> bool:
    if not first_line.startswith(SHEBANG_PREFIX):
        return False
    lowered = first_line.lower()
    return any(hint in lowered for hint in PY_SHEBANG_HINTS)


def _is_extensionless_python_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            first_line = fh.readline()
    except OSError as exc:
        logger.debug(f"Could not sniff {path}: {exc}")
        return False
    return _looks_like_python_shebang(first_line)


def is_python_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix in PY_EXT:
        return True
    if path.suffix == "":
        return _is_extensionless_python_file(path)
    return False


def iter_target_files(inputs: Iterable[Path]) -> Iterator[Path]:
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".tox",
        "node_modules",
    }
    seen: set[Path] = set()
    for raw in inputs:
        path = raw.resolve()
        if path.is_file():
            if is_python_file(path) and path not in seen:
                seen.add(path)
                yield path
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if any(part in skip_dirs for part in candidate.parts):
                    continue
                if candidate.is_file() and is_python_file(candidate):
                    resolved = candidate.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield resolved
        else:
            logger.warning(f"Path does not exist, skipping: {path}")


def _is_pragma_comment(comment_value: str) -> bool:
    stripped = comment_value.strip()
    return any(stripped.startswith(prefix) for prefix in PRAGMA_PREFIXES)


def _is_shebang_comment(comment_value: str) -> bool:
    return comment_value.startswith(SHEBANG_PREFIX)


class CommentDocstringStripper(cst.CSTTransformer):
    def __init__(self) -> None:
        super().__init__()
        self._is_module_level: bool = True
        self._module_docstring_seen: bool = False

    def leave_EmptyLine(self, original_node: EmptyLine, updated_node: EmptyLine) -> EmptyLine:
        comment = updated_node.comment
        if comment is None:
            return updated_node
        value = comment.value
        if _is_shebang_comment(value) or _is_pragma_comment(value):
            return updated_node
        return updated_node.with_changes(comment=None)

    def leave_TrailingWhitespace(
        self, original_node: TrailingWhitespace, updated_node: TrailingWhitespace
    ) -> TrailingWhitespace:
        comment = updated_node.comment
        if comment is None:
            return updated_node
        value = comment.value
        if _is_pragma_comment(value):
            return updated_node
        return updated_node.with_changes(comment=None)

    def visit_Module(self, node: Module) -> Optional[bool]:
        self._is_module_level = True
        return None

    def leave_Module(self, original_node: Module, updated_node: Module) -> Module:
        return updated_node

    def leave_FunctionDef(self, original_node: FunctionDef, updated_node: FunctionDef) -> FunctionDef:
        return updated_node.with_changes(body=self._strip_docstring_body(updated_node.body))

    def leave_ClassDef(self, original_node: ClassDef, updated_node: ClassDef) -> ClassDef:
        return updated_node.with_changes(body=self._strip_docstring_body(updated_node.body))

    @staticmethod
    def _is_docstring_statement(stmt: BaseStatement) -> bool:
        if not isinstance(stmt, SimpleStatementLine):
            return False
        if len(stmt.body) != 1:
            return False
        expr_stmt = stmt.body[0]
        if not isinstance(expr_stmt, cst.Expr):
            return False
        value = expr_stmt.value
        return isinstance(value, (cst.SimpleString, cst.ConcatenatedString))

    def _strip_docstring_body(self, body: cst.IndentedBlock) -> cst.IndentedBlock:
        if not body.body:
            return body
        first_stmt = body.body[0]
        if not self._is_docstring_statement(first_stmt):
            return body
        remaining = list(body.body[1:])
        if not remaining:
            pass_stmt = SimpleStatementLine(body=[cst.Pass()])
            remaining = [pass_stmt]
        return body.with_changes(body=tuple(remaining))


class ModuleDocstringPreserver(cst.CSTTransformer):
    def __init__(self) -> None:
        super().__init__()

    def leave_Module(self, original_node: Module, updated_node: Module) -> Module:
        return updated_node


def strip_comments_and_docstrings(source: str) -> str:
    module = cst.parse_module(source)
    transformed = module.visit(CommentDocstringStripper())
    return transformed.code


def validate_python_source(source: str, filename: str) -> None:
    compile(source, filename=filename, mode="exec", dont_inherit=True)


def process_file(path_str: str, dry_run: bool) -> ProcessResult:
    path = Path(path_str)
    try:
        original_source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ProcessResult(path=path, success=False, changed=False, message=f"read error: {exc}")
    try:
        new_source = strip_comments_and_docstrings(original_source)
    except cst.ParserSyntaxError as exc:
        return ProcessResult(path=path, success=False, changed=False, message=f"parse error: {exc}")
    except Exception as exc:
        return ProcessResult(path=path, success=False, changed=False, message=f"transform error: {exc}")
    try:
        validate_python_source(new_source, filename=str(path))
    except SyntaxError as exc:
        return ProcessResult(
            path=path,
            success=False,
            changed=False,
            message=f"validation failed, aborting write: {exc}",
        )
    if new_source == original_source:
        return ProcessResult(path=path, success=True, changed=False, message="no changes")
    if dry_run:
        return ProcessResult(path=path, success=True, changed=True, message="dry-run: would change")
    try:
        path.write_text(new_source, encoding="utf-8")
    except OSError as exc:
        return ProcessResult(path=path, success=False, changed=False, message=f"write error: {exc}")
    return ProcessResult(path=path, success=True, changed=True, message="stripped")


def run(paths: list[Path], workers: int, dry_run: bool) -> int:
    search_roots = paths or [Path.cwd()]
    targets = list(iter_target_files(search_roots))
    if not targets:
        logger.warning("No Python files found to process.")
        return 0
    logger.info(f"Discovered {len(targets)} Python file(s). Processing with {workers} workers...")
    failures: list[ProcessResult] = []
    changed_count = 0
    processed_count = 0
    with mp.Pool(processes=workers) as pool:
        async_results = [pool.apply_async(process_file, args=(str(target), dry_run)) for target in targets]
        for async_result in async_results:
            try:
                result: ProcessResult = async_result.get()
            except Exception as exc:
                logger.error(f"Worker raised an unexpected exception: {exc}")
                continue
            processed_count += 1
            if not result.success:
                failures.append(result)
                logger.error(f"{result.path}: {result.message}")
            elif result.changed:
                changed_count += 1
                logger.info(f"{result.path}: {result.message}")
            else:
                logger.debug(f"{result.path}: {result.message}")
    logger.info(f"Done. processed={processed_count} changed={changed_count} failed={len(failures)} dry_run={dry_run}")
    if failures:
        logger.error(f"{len(failures)} file(s) failed to process.")
        return 1
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strip comments and docstrings from Python files using LibCST.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process. Defaults to current directory (recursive).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing any files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG-level) logging.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)
    workers = max(1, MAX_WORKERS)
    if workers != MAX_WORKERS:
        logger.debug(f"Using {workers} workers (overridden from default {MAX_WORKERS}).")
    try:
        return run(paths=args.paths, workers=workers, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130


if __name__ == "__main__":
    mp.set_start_method("spawn", force=False)
    sys.exit(main())
"""
### Key design notes
1. **Comment stripping**: Handled via `EmptyLine` (standalone comment lines) and `TrailingWhitespace` (inline comments) leave-visitors — the two places LibCST attaches comments. Shebang and pragma (`# type:`, `# fmt:`, `# noqa`) comments are detected by prefix and preserved.
2. **Docstring stripping**: Only applied inside `FunctionDef`/`ClassDef` bodies (module docstring is untouched by design since the transformer never inspects `Module.body` directly). If the docstring was the sole statement, the body is replaced with a single `pass` statement to avoid an empty/invalid block.
3. **Extension-less files**: Detected by reading just the first line and checking for a `#!...python` shebang — avoids loading the whole file for detection.
4. **Validation before write**: `compile()` is used as a cheap, authoritative syntax validator prior to writing; on failure, the original file is left untouched.
5. **Concurrency**: Fixed 8-worker `multiprocessing.Pool` using `apply_async`, with `.get()` collected per-task so failures don't halt the batch. `process_file` is a top-level function (required for pickling on `spawn`).
6. **Memory efficiency**: Files are read/processed/written one at a time per worker; `rglob` is a generator so directory walking doesn't buffer the whole tree in memory.
7. **Robustness**: Every failure mode (read, parse, transform, validate, write) is caught and reported per-file without crashing the whole batch; exit code reflects overall success/failure.
---
LiveDoc: https://felo.ai/zh-Hans/livedoc/bc5K2Yo6mvjUrECkfe3S6x
[state] thread_short_id=bAbvbaPSrv2KvikMHGC9C8 live_doc_short_id=bc5K2Yo6mvjUrECkfe3S6x live_doc_url=https://felo.ai/zh-Hans/livedoc/bc5K2Yo6mvjUrECkfe3S6x
"""
