#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments, docstrings and type hints of Python files with LibCST.
``-c`` removes the comment blocks, ``-d`` the docstrings, ``-t`` the annotations plus the unused typing imports and ``-a`` everything; an ``.ignore`` file filters the tree, the writes can be previewed with ``--dry-run``/``--diff`` and backed up, and every pool method is supported.
Usage: py_clean [-c] [-d] [-t] [-a] [--diff] [paths ...] Positional arguments: paths ...
Files or directories (default: cwd) Options: -c, --strip-comments Also remove full-line comment blocks -d, --strip-docstrings Remove docstrings -t, --strip-type-hints Remove annotations and now-unused typing imports -a, --all Strip everything, including shebangs and protected comments --pool-method Multiprocessing dispatch method (default: apply_async) --backup Write a .bak copy before modifying --dry-run Show proposed changes (as a diff) without writing --diff Print a colorized diff of each change -n, --stats Print a per-file/total report --ignore Extra ignore pattern (repeatable) ...
plus 2 more; run ``py_clean --help`` for the full list.
Examples: $ py_clean -c -t --diff src/ Command-line script: run ``python py_clean.py`` or the ``py_clean`` command.
The module is importable as well."""

import argparse
from collections import deque
from dataclasses import dataclass, field
import difflib
import fnmatch
import io
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tokenize
from typing import Any, Callable, Iterable, Optional, Sequence

from dh import atomic_write
import libcst as cst
from libcst.helpers import get_full_name_for_node
from loguru import logger


SUPPORTED_EXT = frozenset({".py", ".pyw"})
IGNORE_FILENAME: str = ".ignore"
DEFAULT_IGNORES = frozenset({"__pycache__", ".git"})
TYPING_MODULES = frozenset({"typing", "typing_extensions"})
PYTHON_VERSION: tuple[int, int] = (3, 12)
COMMENT_REGEX = re.compile(r"^#(?:!|\s*(?:type|fmt):)")
BLOCK_COMMENT_FIELDS: tuple[str, ...] = ("leading_lines", "lines_after_decorators", "footer", "header", "empty_lines")


@dataclass(frozen=True, slots=True)
class Config:
    strip_comments: bool
    strip_docstrings: bool
    strip_type_hints: bool
    preserve_special_comments: bool
    detect_commented_code: bool

    @classmethod
    def from_flags(
        cls, *, strip_comments: bool, strip_docstrings: bool, strip_type_hints: bool, everything: bool
    ) -> "Config":
        if everything:
            return cls(True, True, True, False, False)
        return cls(strip_comments, strip_docstrings, strip_type_hints, True, True)


@dataclass(frozen=True, slots=True)
class RunOptions:
    backup: bool
    dry_run: bool
    want_diff: bool


@dataclass(frozen=True, slots=True)
class FileResult:
    path: str
    status: str
    comments_removed: int = 0
    docstrings_removed: int = 0
    hints_removed: int = 0
    bytes_saved: int = 0
    message: str = ""
    diff: str = ""


def is_simple_expr(node: cst.CSTNode) -> bool:
    if isinstance(node, cst.Expr):
        return isinstance(node.value, (cst.Name, cst.Constant, cst.Attribute))
    return isinstance(node, cst.AnnAssign) and node.value is None


def has_commented_out_code(comment_lines: list[str]) -> bool:
    if len(comment_lines) < 2:
        return False
    lines = []
    for line in comment_lines:
        content = line[1:]
        lines.append(content.removeprefix(" "))
    try:
        parsed = cst.parse_module("\n".join(lines))
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False
    return any(not is_simple_expr(stmt) for stmt in parsed.body)


class BaseTransformer(cst.CSTTransformer):
    def leave_SimpleStatementSuite(
        self, original_node: cst.SimpleStatementSuite, updated_node: cst.SimpleStatementSuite
    ) -> cst.CSTNode:
        if updated_node.body:
            return updated_node
        return updated_node.with_changes(body=[cst.Pass()])

    def leave_IndentedBlock(self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock) -> cst.CSTNode:
        if updated_node.body:
            return updated_node
        return updated_node.with_changes(body=[cst.SimpleStatementLine(body=[cst.Pass()])])


class CodeCleaner(BaseTransformer):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.comments_removed = 0
        self.docstrings_removed = 0
        self.hints_removed = 0

    def _is_protected(self, comment_text: str) -> bool:
        return self.config.preserve_special_comments and COMMENT_REGEX.match(comment_text) is not None

    def leave_TrailingWhitespace(
        self, original_node: cst.TrailingWhitespace, updated_node: cst.TrailingWhitespace
    ) -> cst.TrailingWhitespace:
        if not self.config.strip_comments or updated_node.comment is None:
            return updated_node
        val = updated_node.comment.value
        if self._is_protected(val):
            return updated_node
        self.comments_removed += 1
        return updated_node.with_changes(comment=None)

    def _filter_comment_lines(self, comment_nodes: Sequence[cst.CSTNode]) -> list[cst.CSTNode]:
        result = []
        buffer = []
        for item in [*comment_nodes, None]:
            if item is not None and item.comment is not None:
                buffer.append(item)
                continue
            if buffer:
                texts = [b.comment.value for b in buffer if b.comment]
                if self.config.detect_commented_code and has_commented_out_code(texts):
                    result.extend(buffer)
                else:
                    for b in buffer:
                        if b.comment and self._is_protected(b.comment.value):
                            result.append(b)
                        else:
                            self.comments_removed += 1
                buffer = []
            if item is not None:
                result.append(item)
        return result

    def on_leave(self, original_node: cst.CSTNode, updated_node: cst.CSTNode) -> cst.CSTNode:
        res = super().on_leave(original_node, updated_node)
        if self.config.strip_comments and isinstance(res, cst.CSTNode):
            changes = {}
            for field_name in BLOCK_COMMENT_FIELDS:
                val = getattr(res, field_name, None)
                if isinstance(val, Sequence) and val and isinstance(val[0], cst.EmptyLine):
                    filtered = self._filter_comment_lines(val)
                    if len(filtered) != len(val):
                        changes[field_name] = filtered
            if changes:
                return res.with_changes(**changes)
        return res

    @staticmethod
    def _is_docstring_expr(node: cst.CSTNode) -> bool:
        if not isinstance(node, cst.Expr):
            return False
        val = node.value
        if not isinstance(val, (cst.SimpleString, cst.ConcatenatedString)):
            return False
        return isinstance(val.evaluated_value, str)

    def _remove_first_docstring(
        self, body: Sequence[cst.BaseStatement], *, keep_if_empty: bool
    ) -> list[cst.BaseStatement]:
        if not body:
            return list(body)
        first = body[0]
        if (
            not isinstance(first, cst.SimpleStatementLine)
            or not first.body
            or not self._is_docstring_expr(first.body[0])
        ):
            return list(body)
        self.docstrings_removed += 1
        if len(first.body) > 1:
            return [first.with_changes(body=first.body[1:]), *body[1:]]
        remaining = list(body[1:])
        bg = [line for line in first.leading_lines if line.comment is not None]
        if remaining and bg:
            dest = remaining[0]
            if isinstance(dest, (cst.SimpleStatementLine, cst.BaseCompoundStatement)):
                remaining[0] = dest.with_changes(leading_lines=[*bg, *dest.leading_lines])
        elif not remaining and keep_if_empty:
            remaining = [cst.SimpleStatementLine(body=[cst.Pass()], leading_lines=bg)]
        return remaining

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        if not self.config.strip_docstrings:
            return updated_node
        new_body = self._remove_first_docstring(updated_node.body, keep_if_empty=True)
        return updated_node.with_changes(body=new_body)

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        if not self.config.strip_docstrings:
            return updated_node
        body = updated_node.body
        if isinstance(body, cst.IndentedBlock):
            new_body_list = self._remove_first_docstring(body.body, keep_if_empty=True)
            return updated_node.with_changes(body=body.with_changes(body=new_body_list))
        if body.body and self._is_docstring_expr(body.body[0]):
            self.docstrings_removed += 1
            new_body_list = list(body.body[1:]) or [cst.Pass()]
            return updated_node.with_changes(body=body.with_changes(body=new_body_list))
        return updated_node

    def _strip_def_docstring(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        body = (
            self._remove_first_docstring(node.body.body, keep_if_empty=True)
            if isinstance(node.body, cst.IndentedBlock)
            else node.body
        )
        return (
            node.with_changes(body=node.body.with_changes(body=body))
            if isinstance(node.body, cst.IndentedBlock)
            else node
        )

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        res = updated_node
        if self.config.strip_type_hints and res.returns is not None:
            self.hints_removed += 1
            res = res.with_changes(returns=None)
        if self.config.strip_docstrings:
            res = self._strip_def_docstring(res)
        return res

    def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.Param:
        if self.config.strip_type_hints and updated_node.annotation is not None:
            self.hints_removed += 1
            return updated_node.with_changes(annotation=None)
        return updated_node

    def leave_AnnAssign(self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign) -> cst.CSTNode:
        if not self.config.strip_type_hints or updated_node.value is None:
            return updated_node
        self.hints_removed += 1
        return cst.Assign(
            targets=[cst.AssignTarget(target=updated_node.target)],
            value=updated_node.value,
            semicolon=updated_node.semicolon,
        )

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.CSTNode:
        if self.config.strip_type_hints:
            filtered = [
                stmt for stmt in updated_node.body if not (isinstance(stmt, cst.AnnAssign) and stmt.value is None)
            ]
            if len(filtered) != len(updated_node.body):
                self.hints_removed += len(updated_node.body) - len(filtered)
                if not filtered:
                    return cst.RemoveFromParent()
                return updated_node.with_changes(body=filtered)
        return updated_node


class UsageVisitor(cst.CSTVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.used: set[str] = set()

    def visit_Name(self, node: cst.Name) -> bool:
        self.used.add(node.value)
        return False

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        return True

    def visit_SimpleString(self, node: cst.SimpleString) -> bool:
        return False


class TypingImportCleaner(BaseTransformer):
    def __init__(self, used_names: set[str]) -> None:
        super().__init__()
        self.used = used_names

    @staticmethod
    def _bound_name(alias: cst.ImportAlias) -> str:
        if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
            return alias.asname.name.value
        return get_full_name_for_node(alias.name) or ""

    @staticmethod
    def _fix_last_comma(aliases: list[cst.ImportAlias]) -> list[cst.ImportAlias]:
        if aliases:
            aliases[-1] = aliases[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return aliases

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.CSTNode | cst.RemovalSentinel:
        mod = updated_node.module
        names = updated_node.names
        if (
            updated_node.relative
            or not isinstance(mod, cst.Name)
            or mod.value not in TYPING_MODULES
            or isinstance(names, cst.ImportStar)
        ):
            return updated_node
        filtered = [name for name in names if self._bound_name(name) in self.used]
        if len(filtered) == len(names):
            return updated_node
        if not filtered:
            return cst.RemoveFromParent()
        return updated_node.with_changes(names=self._fix_last_comma(filtered))

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.CSTNode | cst.RemovalSentinel:
        filtered = []
        for alias in updated_node.names:
            full_name = get_full_name_for_node(alias.name)
            if full_name in TYPING_MODULES and self._bound_name(alias) not in self.used:
                continue
            filtered.append(alias)
        if len(filtered) == len(updated_node.names):
            return updated_node
        if not filtered:
            return cst.RemoveFromParent()
        return updated_node.with_changes(names=self._fix_last_comma(filtered))

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.CSTNode:
        if not updated_node.body:
            return cst.RemoveFromParent()
        return updated_node


@dataclass(frozen=True, slots=True)
class CleanedModule:
    code: str
    comments_removed: int
    docstrings_removed: int
    hints_removed: int

    @classmethod
    def run(cls, source_code: str, config: Config) -> "CleanedModule":
        tree = cst.parse_module(source_code)
        cleaner = CodeCleaner(config)
        tree = tree.visit(cleaner)
        if config.strip_type_hints:
            visitor = UsageVisitor()
            tree.visit(visitor)
            tree = tree.visit(TypingImportCleaner(frozenset(visitor.used)))
        return cls(tree.code, cleaner.comments_removed, cleaner.docstrings_removed, cleaner.hints_removed)


@dataclass(frozen=True, slots=True)
class DecodedFile:
    text: str
    encoding: str
    bom: bool

    def encode(self, code: str) -> bytes:
        data = code.encode(self.encoding)
        return b"\xef\xbb\xbf" + data if self.bom else data


def decode_file(raw_bytes: bytes) -> DecodedFile:
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    content = raw_bytes[len(b"\xef\xbb\xbf") :] if has_bom else raw_bytes
    alt_encodings = []
    try:
        detected, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
        if not detected.startswith("utf-8"):
            alt_encodings.append(detected)
    except (SyntaxError, LookupError):
        pass
    alt_encodings.extend(["utf-8", "cp1252"])
    for enc in dict.fromkeys(alt_encodings):
        try:
            return DecodedFile(content.decode(enc), enc, has_bom)
        except (UnicodeDecodeError, LookupError):
            continue
    msg = "could not decode file as utf-8, its declared encoding, or cp1252"
    raise ValueError(msg)


def is_python_file(path: Path) -> bool:
    if path.suffix in SUPPORTED_EXT:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as f:
            header = f.readline(256)
    except OSError:
        return False
    return header.startswith(b"#!") and b"python" in header


def get_diff(path: str, original: str, modified: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class WorkerContext:
    def __init__(self, config: Config, run_options: RunOptions) -> None:
        self.config = config
        self.run = run_options


_worker_context: Optional[WorkerContext] = None


def init_worker(config: Config, run_options: RunOptions) -> None:
    global _worker_context
    _worker_context = WorkerContext(config, run_options)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def process_file(path_str: str) -> FileResult:
    ctx = _worker_context
    if ctx is None:
        return FileResult(path_str, "error", message="worker not initialised")
    path = Path(path_str)
    try:
        raw_bytes = path.read_bytes()
    except PermissionError:
        return FileResult(path_str, "error", message="permission denied")
    except OSError as exc:
        return FileResult(path_str, "error", message=f"cannot read: {exc}")
    warnings = []
    try:
        decoded = decode_file(raw_bytes)
        if decoded.encoding != "utf-8":
            warnings.append(f"decoded as {decoded.encoding}; will be written back in that encoding")
        cleaned = CleanedModule.run(decoded.text, ctx.config)
    except ValueError as exc:
        return FileResult(path_str, "error", message=str(exc))
    except cst.ParserSyntaxError as exc:
        return FileResult(path_str, "error", message=f"cannot parse: {exc}")
    except Exception as exc:
        return FileResult(path_str, "error", message=f"transformer failed: {exc!r}")
    if cleaned.code == decoded.text:
        return FileResult(path_str, "unchanged", message="; ".join(warnings))
    try:
        cst.parse_module(cleaned.code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return FileResult(path_str, "error", message=f"output is invalid Python, skipped: {exc}")
    try:
        new_bytes = decoded.encode(cleaned.code)
        diff_text = ""
        if ctx.run.want_diff or ctx.run.dry_run:
            diff_text = get_diff(path_str, decoded.text, cleaned.code)
        if not ctx.run.dry_run:
            if ctx.run.backup:
                backup_path = path.with_name(path.name + ".bak")
                if backup_path.exists():
                    warnings.append("existing .bak kept (not overwritten)")
                else:
                    shutil.copy2(path, backup_path)
            atomic_write(path, new_bytes)
    except PermissionError:
        return FileResult(path_str, "error", message="permission denied")
    except (OSError, UnicodeEncodeError) as exc:
        return FileResult(path_str, "error", message=f"write failed: {exc}")
    bytes_saved = len(raw_bytes) - len(new_bytes)
    return FileResult(
        path=path_str,
        status="changed",
        comments_removed=cleaned.comments_removed,
        docstrings_removed=cleaned.docstrings_removed,
        hints_removed=cleaned.hints_removed,
        bytes_saved=bytes_saved,
        message="; ".join(warnings),
        diff=diff_text,
    )


class IgnoreRule:
    def __init__(self, pattern: str, dir_only: bool, anchored: bool) -> None:
        self.pattern = pattern
        self.dir_only = dir_only
        self.anchored = anchored


class IgnoreMatcher:
    def __init__(self, lines: Iterable[str]) -> None:
        self._rules: list[IgnoreRule] = []
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            dir_only = text.endswith("/")
            text = text.rstrip("/")
            anchored = "/" in text
            text = text.lstrip("/")
            if text:
                self._rules.append(IgnoreRule(text, dir_only, anchored))

    def matches(self, parts: tuple[str, ...], is_dir: bool) -> bool:
        for rule in self._rules:
            for depth in range(1, len(parts) + 1):
                if rule.dir_only and depth == len(parts) and not is_dir:
                    continue
                target = "/".join(parts[:depth]) if rule.anchored else parts[depth - 1]
                if fnmatch.fnmatchcase(target, rule.pattern):
                    return True
        return False


def read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"cannot read ignore file {path}: {exc}")
        return []


def find_files(paths: Sequence[Path], extra_ignore: Sequence[str], ignore_file_path: Optional[Path]) -> list[str]:
    seen: set[Path] = set()
    collected: list[str] = []

    def add_path(p: Path) -> None:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            collected.append(str(p))

    for base_path in paths:
        if base_path.is_file():
            if is_python_file(base_path):
                add_path(base_path)
            else:
                logger.warning(f"{base_path}: not a Python file (no .py suffix or python shebang), skipped")
            continue
        if not base_path.is_dir():
            logger.error(f"{base_path}: no such file or directory")
            continue
        rules = [*DEFAULT_IGNORES, *extra_ignore, *read_ignore_file(base_path / IGNORE_FILENAME)]
        if ignore_file_path is not None:
            rules.extend(read_ignore_file(ignore_file_path))
        matcher = IgnoreMatcher(rules)
        for root, dirs, files in base_path.walk():
            rel_parts = root.relative_to(base_path).parts
            dirs[:] = sorted(d for d in dirs if not matcher.matches((*rel_parts, d), True))
            for file_name in sorted(files):
                if matcher.matches((*rel_parts, file_name), False):
                    continue
                file_path = root / file_name
                if is_python_file(file_path):
                    add_path(file_path)
    return collected


class PoolExecutor:
    def __init__(self, method: str, config: Config, run_options: RunOptions) -> None:
        self.method = method
        self.config = config
        self.run = run_options

    def execute(self, paths: list[str], callback: Callable[[FileResult], None]) -> None:
        cpu_count = max(1, min(64, len(paths) // 32 or 1))
        context = multiprocessing.get_context()
        with context.Pool(processes=cpu_count, initializer=init_worker, initargs=(self.config, self.run)) as pool:
            if self.method == "map":
                for res in pool.map(process_file, paths, chunksize=max(1, len(paths) // (cpu_count * 4))):
                    callback(res)
            elif self.method == "imap":
                for res in pool.imap(process_file, paths, chunksize=max(1, len(paths) // (cpu_count * 4))):
                    callback(res)
            elif self.method == "imap_unordered":
                for res in pool.imap_unordered(process_file, paths, chunksize=max(1, len(paths) // (cpu_count * 4))):
                    callback(res)
            elif self.method == "apply":
                for p in paths:
                    callback(pool.apply(process_file, (p,)))
            else:
                queue = deque()
                limit = cpu_count * 4
                for p in paths:
                    queue.append(pool.apply_async(process_file, (p,)))
                    while len(queue) >= limit:
                        callback(queue.popleft().get())
                while queue:
                    callback(queue.popleft().get())


@dataclass(slots=True)
class StatsTracker:
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    errors: int = 0
    comments: int = 0
    docstrings: int = 0
    hints: int = 0
    bytes_saved: int = 0
    rows: list[FileResult] = field(default_factory=list)

    def add(self, res: FileResult) -> None:
        self.scanned += 1
        if res.status == "error":
            self.errors += 1
        elif res.status == "unchanged":
            self.unchanged += 1
        else:
            self.changed += 1
            self.comments += res.comments_removed
            self.docstrings += res.docstrings_removed
            self.hints += res.hints_removed
            self.bytes_saved += res.bytes_saved
            self.rows.append(res)


def supports_color() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def blue(text: Any) -> str:
    if supports_color():
        return f"\033[34m{text}\033[0m"
    return str(text)


def green(text: Any) -> str:
    if supports_color():
        return f"\033[32m{text}\033[0m"
    return str(text)


def colorize_diff(diff_text: str) -> str:
    mapping = {"+++": "1", "---": "1", "@@": "36", "+": "32", "-": "31"}
    lines = []
    for line in diff_text.splitlines():
        code = next((c for prefix, c in mapping.items() if line.startswith(prefix)), None)
        lines.append(f"\033[{code}m{line}\033[0m" if code else line)
    return "\n".join(lines)


def print_stats(tracker: StatsTracker, dry_run: bool) -> None:
    header = f"{'file':<60} {'comments':>9} {'docstrings':>11} {'hints':>6} {'bytes saved':>12}"
    print("\n" + header)
    print("-" * len(header))
    for row in tracker.rows:
        disp_path = row.path if len(row.path) <= 60 else "..." + row.path[-57:]
        b_saved = green(f"{row.bytes_saved}")
        print(
            f"{disp_path:<60} {row.comments_removed:>9} {row.docstrings_removed:>11} {row.hints_removed:>6} {b_saved:>12}"
        )
    print("-" * len(header))
    total_saved = green(f"{tracker.bytes_saved}")
    print(f"{'TOTAL':<60} {tracker.comments:>9} {tracker.docstrings:>11} {tracker.hints:>6} {total_saved:>12}")
    if dry_run:
        print("(dry run: nothing was written; numbers are what would change)")


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyclean",
        description="Strip comments, docstrings and type hints from Python files using LibCST.",
        epilog="By default only inline comments are removed. Shebangs, '# type:' and '# fmt:' comments, and blocks that look like commented-out code are preserved unless --all is given.",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories (default: cwd)")
    parser.add_argument("-c", "--strip-comments", action="store_true", help="also remove full-line comment blocks")
    parser.add_argument("-d", "--strip-docstrings", action="store_true", help="remove docstrings")
    parser.add_argument(
        "-t", "--strip-type-hints", action="store_true", help="remove annotations and now-unused typing imports"
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        dest="everything",
        help="strip everything, including shebangs and protected comments",
    )
    parser.add_argument(
        "--pool-method",
        choices=["map", "imap", "imap_unordered", "apply", "apply_async"],
        default="apply_async",
        help="multiprocessing dispatch method (default: apply_async)",
    )
    parser.add_argument("--backup", action="store_true", help="write a .bak copy before modifying")
    parser.add_argument("--dry-run", action="store_true", help="show proposed changes (as a diff) without writing")
    parser.add_argument("--diff", action="store_true", help="print a colorized diff of each change")
    parser.add_argument("-n", "--stats", action="store_true", help="print a per-file/total report")
    parser.add_argument(
        "--ignore", action="append", default=[], metavar="PATTERN", help="extra ignore pattern (repeatable)"
    )
    parser.add_argument(
        "--ignore-file",
        type=Path,
        metavar="FILE",
        help=f"additional ignore file (a '{IGNORE_FILENAME}' in each scanned directory is always read)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(args)


def setup_logger(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        colorize=sys.stderr.isatty(),
        format="<level>{level:<8}</level>{message}",
    )


def main(args: Optional[Sequence[str]] = None) -> int:
    if sys.version_info < PYTHON_VERSION:
        print("pyclean requires Python 3.12 or newer", file=sys.stderr)
        return 2
    parsed_args = parse_args(args)
    setup_logger(parsed_args.verbose)
    config = Config.from_flags(
        strip_comments=parsed_args.strip_comments,
        strip_docstrings=parsed_args.strip_docstrings,
        strip_type_hints=parsed_args.strip_type_hints,
        everything=parsed_args.everything,
    )
    run_options = RunOptions(
        backup=parsed_args.backup and not parsed_args.dry_run, dry_run=parsed_args.dry_run, want_diff=parsed_args.diff
    )
    if parsed_args.backup and parsed_args.dry_run:
        logger.info("--backup ignored in --dry-run mode")
    file_paths = find_files(parsed_args.paths or [Path.cwd()], parsed_args.ignore, parsed_args.ignore_file)
    if not file_paths:
        logger.warning("no Python files found")
        return 0
    cpu_count = max(1, min(64, len(file_paths) // 32 or 1))
    logger.info(f"processing {len(file_paths)} file(s) with {cpu_count} workers ({parsed_args.pool_method})")
    stats = StatsTracker()
    use_color = supports_color()
    show_diff = parsed_args.diff or parsed_args.dry_run

    def on_result(res: FileResult) -> None:
        stats.add(res)
        if res.status == "error":
            logger.error(f"{res.path}: {res.message}")
            return
        if res.message:
            logger.warning(f"{res.path}: {res.message}")
        if res.status == "changed":
            rel_path = Path(res.path).relative_to(Path())
            action = "would reduce" if run_options.dry_run else "reduced"
            logger.info(f"{rel_path}: {action} {green(f'{res.bytes_saved}')} bytes")
            if show_diff and res.diff:
                print(colorize_diff(res.diff) if use_color else res.diff, file=sys.stderr)

    try:
        PoolExecutor(parsed_args.pool_method, config, run_options).execute(file_paths, on_result)
    except KeyboardInterrupt:
        logger.error("interrupted; workers terminated (completed files were already written)")
        return 130
    action_word = "would be changed" if run_options.dry_run else "changed"
    logger.info(
        f"{stats.changed} file(s) {action_word}, {stats.unchanged} unchanged, {stats.errors} error(s); "
        f"total space freed: {green(stats.bytes_saved)} bytes"
    )
    if parsed_args.stats:
        print_stats(stats, run_options.dry_run)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
