#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments and docstrings of Python files with LibCST.
Removes the comments and the docstrings (preserving the shebangs, the encoding declarations and the ``# type:`` comments), validates the result, writes it atomically and supports ``--dry-run``, ``--diff``, ``--backup``, ``--stats`` and every pool dispatch method.
Requires Python 3.12+.
Usage: crmc [--diff|--dry-run|--backup] [--pool-method MAP] [paths ...] Positional arguments: paths ...
Files or directories to process recursively (default: current directory) Options: --pool-method Multiprocessing.Pool dispatch method (default: %(default)s) -n, --stats Report the number of comments and docstrings removed per file -d, --dry-run Show what would change without writing anything --diff Print a unified diff of every change --backup Copy each file to '<name>.bak' before modifying it (never overwrites a .bak) --fallback-encoding Decode files that are not valid UTF-8 with ENCODING (e.g.
cp1252) and write them back in that encoding; by default such files are skipped --exclude Extra directory name to skip while walking (repeatable) -v, --verbose Debug logging -q, --quiet Only log errors Examples: $ crmc --diff --stats src/ Command-line script: run ``python crmc.py`` or the ``crmc`` command.
The module is importable as well."""

import argparse
import ast
import codecs
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
import contextlib
from dataclasses import dataclass
import difflib
import enum
import functools
import io
import multiprocessing as mp
from multiprocessing.pool import Pool
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import tokenize
from typing import TYPE_CHECKING, Final, Literal, get_args

from dh import MAX_WORKERS
import libcst as cst
from loguru import logger

if TYPE_CHECKING:
    from multiprocessing.pool import AsyncResult
"""Exact number of worker processes in the pool (a hard requirement)."""
CHUNKSIZE: Final[int] = 16
"""Chunk size for map-like pool methods; amortises IPC overhead on big trees."""
MAX_IN_FLIGHT: Final[int] = MAX_WORKERS * 8
"""Upper bound of outstanding tasks for ``apply_async`` (bounds memory use)."""
TARGET_PYTHON: Final[str] = "3.12"
TARGET_VERSION: Final[tuple[int, int]] = (3, 12)
PY_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyw", ".pyi"})
"""Extensions picked up while walking directories."""
DEFAULT_EXCLUDES: Final[frozenset[str]] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    ".eggs",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
})
"""Directory names that are never descended into while walking a directory."""
PoolMethod = Literal["map", "imap", "imap_unordered", "apply", "apply_async", "starmap"]
POOL_METHODS: Final[tuple[str, ...]] = get_args(PoolMethod)
_PRESERVED_COMMENT: Final[re.Pattern[str]] = re.compile(r"#\s*(?:type|fmt):")
_UTF8_COMPATIBLE: Final[frozenset[str]] = frozenset({"utf-8", "utf-8-sig", "ascii", "us-ascii"})


class Status(enum.StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Options:
    dry_run: bool = False
    backup: bool = False
    want_diff: bool = False
    fallback_encoding: str | None = None


@dataclass(frozen=True, slots=True)
class FileResult:
    path: Path
    status: Status
    bytes_before: int = 0
    bytes_after: int = 0
    comments_removed: int = 0
    docstrings_removed: int = 0
    diff: str = ""
    message: str = ""

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after


@dataclass(slots=True)
class Summary:
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    missing: int = 0
    bytes_processed: int = 0
    bytes_saved: int = 0
    comments: int = 0
    docstrings: int = 0

    def add(self, result: FileResult) -> None:
        self.scanned += 1
        match result.status:
            case Status.CHANGED:
                self.changed += 1
                self.bytes_saved += result.bytes_saved
                self.comments += result.comments_removed
                self.docstrings += result.docstrings_removed
                self.bytes_processed += result.bytes_before
            case Status.UNCHANGED:
                self.unchanged += 1
                self.bytes_processed += result.bytes_before
            case Status.SKIPPED:
                self.skipped += 1
            case Status.FAILED:
                self.failed += 1


class SkipFile(Exception):
    pass


def _is_preserved(comment: str) -> bool:
    return _PRESERVED_COMMENT.match(comment) is not None


def _is_plain_string(expr: cst.BaseExpression) -> bool:
    if isinstance(expr, cst.SimpleString):
        return "b" not in expr.prefix.lower()
    if isinstance(expr, cst.ConcatenatedString):
        return _is_plain_string(expr.left) and _is_plain_string(expr.right)
    return False


def _is_docstring_statement(stmt: cst.BaseSmallStatement) -> bool:
    return isinstance(stmt, cst.Expr) and _is_plain_string(stmt.value)


class StripCommentsAndDocstrings(cst.CSTTransformer):
    def __init__(self, *, has_shebang: bool) -> None:
        super().__init__()
        self.comments_removed: int = 0
        self.docstrings_removed: int = 0
        self._shebang_pending: bool = has_shebang

    def leave_EmptyLine(
        self, original_node: cst.EmptyLine, updated_node: cst.EmptyLine
    ) -> cst.EmptyLine | cst.RemovalSentinel:
        comment = updated_node.comment
        if self._shebang_pending:
            self._shebang_pending = False
            if comment is not None and comment.value.startswith("#!"):
                return updated_node
        if comment is None or _is_preserved(comment.value):
            return updated_node
        self.comments_removed += 1
        return cst.RemoveFromParent()

    def leave_TrailingWhitespace(
        self,
        original_node: cst.TrailingWhitespace,
        updated_node: cst.TrailingWhitespace,
    ) -> cst.TrailingWhitespace:
        comment = updated_node.comment
        if comment is None or _is_preserved(comment.value):
            return updated_node
        self.comments_removed += 1
        return updated_node.with_changes(comment=None, whitespace=cst.SimpleWhitespace(""))

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        return updated_node.with_changes(body=self._strip_docstring(updated_node.body))

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        return updated_node.with_changes(body=self._strip_docstring(updated_node.body))

    def _strip_docstring(self, body: cst.BaseSuite) -> cst.BaseSuite:
        if isinstance(body, cst.SimpleStatementSuite):
            small = body.body
            if not small or not _is_docstring_statement(small[0]):
                return body
            self.docstrings_removed += 1
            rest = small[1:]
            return body.with_changes(body=rest or [cst.Pass()])
        if not isinstance(body, cst.IndentedBlock) or not body.body:
            return body
        first = body.body[0]
        if (
            not isinstance(first, cst.SimpleStatementLine)
            or not first.body
            or not _is_docstring_statement(first.body[0])
        ):
            return body
        self.docstrings_removed += 1
        rest_of_line = first.body[1:]
        rest_of_block = body.body[1:]
        if rest_of_line:
            new_first = first.with_changes(body=rest_of_line)
            return body.with_changes(body=(new_first, *rest_of_block))
        if rest_of_block:
            return body.with_changes(body=rest_of_block)
        return body.with_changes(body=(first.with_changes(body=[cst.Pass()]),))


def _check_declared_encoding(raw: bytes) -> None:
    try:
        declared, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except (SyntaxError, LookupError):
        return
    if declared.lower() not in _UTF8_COMPATIBLE:
        msg = f"declares non-UTF-8 source encoding {declared!r}"
        raise SkipFile(msg)


def _decode_source(raw: bytes, fallback: str | None) -> tuple[str, str, bytes]:
    if b"\x00" in raw:
        msg = "binary or non-text content (NUL bytes)"
        raise SkipFile(msg)
    _check_declared_encoding(raw)
    bom = b""
    if raw.startswith(codecs.BOM_UTF8):
        bom, raw = codecs.BOM_UTF8, raw[len(codecs.BOM_UTF8) :]
    try:
        return raw.decode("utf-8"), "utf-8", bom
    except UnicodeDecodeError as exc:
        utf8_error = exc
    if fallback is None or bom:
        msg = f"not valid UTF-8 ({utf8_error.reason} at byte {utf8_error.start}); use --fallback-encoding to process it"
        raise SkipFile(msg) from utf8_error
    try:
        return raw.decode(fallback), fallback, b""
    except UnicodeDecodeError as exc:
        msg = f"cannot decode as UTF-8 or {fallback}: {exc.reason}"
        raise SkipFile(msg) from exc


def strip_source(source: str) -> tuple[str, int, int]:
    module = cst.parse_module(source, config=cst.PartialParserConfig(python_version=TARGET_PYTHON))
    transformer = StripCommentsAndDocstrings(has_shebang=source.startswith("#!"))
    new_module = module.visit(transformer)
    return new_module.code, transformer.comments_removed, transformer.docstrings_removed


def _make_diff(path: Path, old: str, new: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return diff if not diff or diff.endswith("\n") else diff + "\n"


def _make_backup(path: Path) -> None:
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        msg = f"backup already exists: {backup}"
        raise FileExistsError(msg)
    shutil.copy2(path, backup)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            shutil.copymode(path, tmp)
        try:
            tmp.replace(path)
        except OSError:
            path.write_bytes(data)
            tmp.unlink(missing_ok=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _process_file(path: Path, options: Options) -> FileResult:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return FileResult(path, Status.FAILED, message=f"cannot read file: {exc}")
    try:
        source, encoding, bom = _decode_source(raw, options.fallback_encoding)
    except SkipFile as exc:
        return FileResult(path, Status.SKIPPED, message=str(exc))
    try:
        new_source, n_comments, n_docstrings = strip_source(source)
    except cst.ParserSyntaxError:
        return FileResult(path, Status.SKIPPED, message=f"not valid Python {TARGET_PYTHON} source")
    if new_source == source:
        return FileResult(path, Status.UNCHANGED, bytes_before=len(raw), bytes_after=len(raw))
    try:
        ast.parse(new_source, filename=str(path), feature_version=TARGET_VERSION)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return FileResult(
            path,
            Status.FAILED,
            message=f"transformed code failed validation, file untouched: {exc}",
        )
    new_raw = bom + new_source.encode(encoding)
    diff = _make_diff(path, source, new_source) if options.want_diff else ""
    changed = FileResult(
        path,
        Status.CHANGED,
        bytes_before=len(raw),
        bytes_after=len(new_raw),
        comments_removed=n_comments,
        docstrings_removed=n_docstrings,
        diff=diff,
    )
    if options.dry_run:
        return changed
    if not os.access(path, os.W_OK):
        return FileResult(path, Status.FAILED, message="file is not writable (permission denied)")
    try:
        if options.backup:
            _make_backup(path)
        _atomic_write(path, new_raw)
    except OSError as exc:
        return FileResult(path, Status.FAILED, message=f"cannot write file: {exc}")
    return changed


def process_file(path: Path, options: Options) -> FileResult:
    try:
        return _process_file(path, options)
    except Exception as exc:
        return FileResult(path, Status.FAILED, message=f"unexpected {type(exc).__name__}: {exc}")


def _init_worker() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _looks_like_python_script(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(256)
    except OSError:
        return False
    return first_line.startswith(b"#!") and b"python" in first_line


def _log_walk_error(error: OSError) -> None:
    logger.warning("Cannot traverse {}: {}", error.filename, error.strerror or error)


def iter_python_files(targets: Iterable[Path], excludes: frozenset[str]) -> Iterator[Path]:
    seen: set[Path] = set()
    for target in targets:
        resolved = target.resolve()
        if resolved.is_file():
            if resolved not in seen:
                seen.add(resolved)
                yield resolved
            continue
        for root, dirnames, filenames in resolved.walk(on_error=_log_walk_error):
            dirnames[:] = sorted(d for d in dirnames if d not in excludes and not d.endswith(".egg-info"))
            for name in sorted(filenames):
                candidate = root / name
                if candidate.is_symlink():
                    continue
                if candidate.suffix in PY_SUFFIXES or (not candidate.suffix and _looks_like_python_script(candidate)):
                    if candidate not in seen:
                        seen.add(candidate)
                        yield candidate


def iter_results(pool: Pool, method: PoolMethod, paths: Iterable[Path], options: Options) -> Iterator[FileResult]:
    worker = functools.partial(process_file, options=options)
    match method:
        case "map":
            yield from pool.map(worker, list(paths), chunksize=CHUNKSIZE)
        case "starmap":
            yield from pool.starmap(process_file, [(path, options) for path in paths], chunksize=CHUNKSIZE)
        case "imap":
            yield from pool.imap(worker, paths, chunksize=CHUNKSIZE)
        case "imap_unordered":
            yield from pool.imap_unordered(worker, paths, chunksize=CHUNKSIZE)
        case "apply":
            for path in paths:
                yield pool.apply(process_file, (path, options))
        case "apply_async":
            pending: deque[AsyncResult[FileResult]] = deque()
            for path in paths:
                pending.append(pool.apply_async(process_file, (path, options)))
                if len(pending) >= MAX_IN_FLIGHT:
                    yield pending.popleft().get()
            while pending:
                yield pending.popleft().get()


def report_result(result: FileResult, *, stats: bool, options: Options) -> None:
    match result.status:
        case Status.CHANGED:
            tag = "[dry-run] " if options.dry_run else ""
            if stats:
                detail = f"{result.comments_removed:,} comments, {result.docstrings_removed:,} docstrings removed"
            else:
                detail = f"{result.bytes_saved:,} bytes reduced ({result.bytes_before:,} -> {result.bytes_after:,})"
            print(f"{tag}{result.path.name}: {detail}")
            if result.diff:
                sys.stdout.write(result.diff)
        case Status.UNCHANGED:
            logger.debug("Unchanged: {}", result.path)
        case Status.SKIPPED:
            logger.warning("Skipped {}: {}", result.path, result.message)
        case Status.FAILED:
            logger.error("Failed {}: {}", result.path, result.message)


def print_summary(summary: Summary, *, options: Options, interrupted: bool = False) -> None:
    verb = "would be saved" if options.dry_run else "saved"
    pct = (summary.bytes_saved / summary.bytes_processed * 100) if summary.bytes_processed else 0.0
    print()
    if interrupted:
        print("Interrupted - partial results:")
    print(
        f"Files: {summary.scanned} scanned, {summary.changed} changed, "
        f"{summary.unchanged} unchanged, {summary.skipped} skipped, "
        f"{summary.failed} failed, {summary.missing} missing"
    )
    print(f"Removed: {summary.comments:,} comments and {summary.docstrings:,} docstrings")
    print(f"Total space {verb}: {summary.bytes_saved:,} bytes ({pct:.1f}% of {summary.bytes_processed:,})")


def _codec_name(value: str) -> str:
    try:
        codecs.lookup(value)
    except LookupError as exc:
        msg = f"unknown encoding: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strip_comments",
        description="Strip comments and docstrings from Python files in place (libcst).",
        epilog="Kept: shebang, module docstring, '# type:' and '# fmt:' comments.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to process recursively (default: current directory)",
    )
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="imap_unordered",
        help="multiprocessing.Pool dispatch method (default: %(default)s)",
    )
    parser.add_argument(
        "-n",
        "--stats",
        action="store_true",
        help="report the number of comments and docstrings removed per file",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="show what would change without writing anything",
    )
    parser.add_argument("--diff", action="store_true", help="print a unified diff of every change")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="copy each file to '<name>.bak' before modifying it (never overwrites a .bak)",
    )
    parser.add_argument(
        "--fallback-encoding",
        type=_codec_name,
        metavar="ENCODING",
        help="decode files that are not valid UTF-8 with ENCODING (e.g. cp1252) and "
        "write them back in that encoding; by default such files are skipped",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="DIRNAME",
        help="extra directory name to skip while walking (repeatable)",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only log errors")
    return parser


def configure_logging(*, verbose: bool, quiet: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "ERROR" if quiet else "INFO"
    logger.add(sys.stderr, level=level, format="<level>{level: <8}</level> | {message}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    options = Options(
        dry_run=args.dry_run,
        backup=args.backup,
        want_diff=args.diff,
        fallback_encoding=args.fallback_encoding,
    )
    summary = Summary()
    targets: list[Path] = []
    for raw_target in args.paths or [Path.cwd()]:
        if raw_target.exists():
            targets.append(raw_target)
        else:
            logger.error("Path does not exist: {}", raw_target)
            summary.missing += 1
    excludes = DEFAULT_EXCLUDES | frozenset(args.exclude)
    logger.info(
        "Processing {} target(s) with {} workers (method={}{})",
        len(targets),
        MAX_WORKERS,
        args.pool_method,
        ", dry-run" if options.dry_run else "",
    )
    interrupted = False
    try:
        with mp.Pool(processes=MAX_WORKERS, initializer=_init_worker) as pool:
            paths = iter_python_files(targets, excludes)
            for result in iter_results(pool, args.pool_method, paths, options):
                summary.add(result)
                report_result(result, stats=args.stats, options=options)
            pool.close()
            pool.join()
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Interrupted; files are written atomically so none are corrupted")
    if summary.scanned == 0 and not interrupted:
        logger.warning("No Python files found")
    print_summary(summary, options=options, interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if (summary.failed or summary.missing) else 0


if __name__ == "__main__":
    if sys.version_info < (3, 12):
        sys.exit("strip_comments requires Python 3.12 or newer")
    raise SystemExit(main())
