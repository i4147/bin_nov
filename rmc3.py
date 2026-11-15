#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python files."""

import argparse
import ast
import codecs
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from functools import cache, partial
import importlib.util
import io
from multiprocessing import Pool
from pathlib import Path, PurePath
import re
import subprocess
import sys
import tempfile
import textwrap
import tokenize
from typing import TYPE_CHECKING, TypeAlias
import warnings
import zipfile

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS
import libcst as cst
from loguru import logger
from tree_sitter import Node, Parser


try:
    from rich.console import Console
    from rich.text import Text
except ImportError:
    print("rich required: pip install rich", file=sys.stderr)
    raise SystemExit(1)

SKIP_DIR_PATTERNS: tuple[str, ...] = ("*.dist-info",)
MAX_SNIFF_SIZE: int = 2_000_000
DEFAULT_BACKENDS: tuple[str, ...] = ("libcst", "tree-sitter")
INSTALL_HINTS: dict[str, str] = {
    "libcst": "libcst not installed: pip install libcst",
    "tree-sitter": "tree-sitter not installed: pip install tree-sitter tree-sitter-python",
}
NEWLINE_RE: re.Pattern[bytes] = re.compile(rb"\r\n|\r|\n")
SEMICOLON_RE: re.Pattern[bytes] = re.compile(rb"[ \t]*;[ \t]*")
STRING_PREFIX_RE: re.Pattern[bytes] = re.compile(rb"[A-Za-z]*")
KEEP_COMMENT_RE: re.Pattern[str] = re.compile(r"#+\s*(?:type:|fmt\s*:)")
CODING_RE: re.Pattern[str] = re.compile(r"#.*?coding[:=]\s*[-\w.]+")
BYTES_COL_WIDTH: int = 12
console = Console()


@dataclass(frozen=True, slots=True)
class Options:
    strip_comments: bool = True
    strip_docstrings: bool = False
    strip_all: bool = False
    backends: tuple[str, ...] = ("libcst", "tree-sitter")
    dry_run: bool = False
    remove_module_docstring: bool = False
    clipboard: bool = False

    @property
    def drop_comments(self) -> bool:
        return self.strip_comments or self.strip_all

    @property
    def drop_docstrings(self) -> bool:
        return self.strip_docstrings or self.strip_all

    @property
    def drop_module_docstring(self) -> bool:
        return self.strip_all or self.remove_module_docstring


@dataclass(slots=True)
class FileResult:
    path: Path
    bytes_removed: int = 0
    comments_removed: int = 0
    docstrings_removed: int = 0
    error: str | None = None
    changed: bool = False
    final_code: str | None = None
    backend: str = ""


@dataclass(frozen=True, slots=True)
class Stripped:
    code: str
    comments: int
    docstrings: int
    backend: str = ""


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    replacement: bytes
    kind: str


@dataclass(frozen=True, slots=True)
class _CommentEntry:
    lineno: int
    is_full_line: bool
    text: str
    edit: Edit


Backend: TypeAlias = Callable[[str, Options], Stripped]


def configure_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{level: <8} {message}", colorize=False)


def decode_source(raw: bytes) -> tuple[str, str]:
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def parse_tree(text: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(text)


def parses(text: str) -> bool:
    try:
        parse_tree(text)
    except Exception:
        return False
    return True


def _parses_as_meaningful_code(code: str) -> bool:
    try:
        module = parse_tree(code)
    except Exception:
        return False
    for stmt in module.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Name, ast.Constant)):
            continue
        return True
    return False


def _looks_like_code(comment_texts: Sequence[str]) -> bool:
    if not comment_texts:
        return False

    def strip_hash(line: str) -> str:
        return line.removeprefix("#")

    def strip_hash_space(line: str) -> str:
        if line.startswith("#"):
            line = line[1:]
            line = line.removeprefix(" ")
        return line

    for stripper in (strip_hash, strip_hash_space):
        joined = "\n".join(stripper(line) for line in comment_texts)
        dedented = textwrap.dedent(joined)
        if _parses_as_meaningful_code(dedented):
            return True
    return False


def _is_commented_out_docstring(content: str) -> bool:
    if "\n" not in content.strip():
        return False
    dedented = textwrap.dedent(content)
    return _parses_as_meaningful_code(dedented)


def line_starts(raw: bytes) -> list[int]:
    return [0, *(match.end() for match in NEWLINE_RE.finditer(raw))]


def apply_edits(raw: bytes, edits: Iterable[Edit]) -> tuple[bytes, int, int]:
    out = bytearray()
    position = 0
    comments = 0
    docstrings = 0
    for edit in sorted(edits, key=lambda item: (item.start, item.end)):
        if edit.start < position:
            continue
        out += raw[position : edit.start]
        out += edit.replacement
        position = edit.end
        if edit.kind == "comment":
            comments += 1
        elif edit.kind == "docstring":
            docstrings += 1
    out += raw[position:]
    return bytes(out), comments, docstrings


def keep_comment(text: str, lineno: int) -> bool:
    if lineno == 1 and text.startswith("#!"):
        return True
    if lineno <= 2 and CODING_RE.match(text):
        return True
    return KEEP_COMMENT_RE.match(text) is not None


def comment_edit(raw: bytes, line_start: int, comment_start: int, comment_end: int) -> Edit:
    prefix = raw[line_start:comment_start]
    if not prefix.strip():
        return Edit(line_start, comment_end, b"", "comment")
    return Edit(comment_start - (len(prefix) - len(prefix.rstrip())), comment_end, b"", "comment")


def statement_edit(raw: bytes, start: int, end: int, replacement: bytes, kind: str) -> Edit:
    match = SEMICOLON_RE.match(raw, end)
    if match is not None:
        end = match.end()
    return Edit(start, end, replacement, kind)


def _filter_comments(entries: Sequence[_CommentEntry], opts: Options) -> list[Edit]:
    n = len(entries)
    group_ids: list[int | None] = [None] * n
    i = 0
    while i < n:
        if not entries[i].is_full_line:
            i += 1
            continue
        j = i
        while j + 1 < n and entries[j + 1].is_full_line and entries[j + 1].lineno == entries[j].lineno + 1:
            j += 1
        for k in range(i, j + 1):
            group_ids[k] = i
        i = j + 1
    group_is_code: dict[int, bool] = {}
    for i, gid in enumerate(group_ids):
        if gid is None or gid in group_is_code:
            continue
        members = [entries[k].text for k in range(gid, n) if group_ids[k] == gid]
        group_is_code[gid] = _looks_like_code(members)
    filtered: list[Edit] = []
    for i, entry in enumerate(entries):
        if not opts.strip_all:
            gid = group_ids[i]
            if gid is not None and group_is_code.get(gid, False):
                continue
            if keep_comment(entry.text, entry.lineno):
                continue
        filtered.append(entry.edit)
    return filtered


def strip_comments(text: str, opts: Options) -> tuple[str, int]:
    if not opts.drop_comments:
        return text, 0
    raw = text.encode()
    lines = io.StringIO(text, newline="").readlines()
    starts = line_starts(raw)
    entries: list[_CommentEntry] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text, newline="").readline):
            if tok.type != tokenize.COMMENT:
                continue
            lineno, col = tok.start
            prefix = lines[lineno - 1][:col]
            is_full_line = prefix.strip() == ""
            line_start = starts[lineno - 1]
            byte_col = len(prefix.encode())
            comment_start = line_start + byte_col
            comment_end = comment_start + len(tok.string.encode())
            edit = comment_edit(raw, line_start, comment_start, comment_end)
            entries.append(_CommentEntry(lineno, is_full_line, tok.string, edit))
    except (tokenize.TokenError, SyntaxError) as exc:
        msg = f"tokenize failed: {exc}"
        raise SyntaxError(msg) from exc
    edits = _filter_comments(entries, opts)
    code, comments, _ = apply_edits(raw, edits)
    return code.decode(), comments


def _libcst_docstring_content(node: object) -> str | None:
    import libcst as cst

    if not isinstance(node, cst.Expr):
        return None
    value = node.value
    if isinstance(value, (cst.SimpleString, cst.ConcatenatedString)):
        result = value.evaluated_value
        if isinstance(result, str):
            return result
    return None


@cache
def libcst_remover() -> type[cst.CSTTransformer]:
    import libcst as cst

    def is_docstring(node: cst.BaseSmallStatement) -> bool:
        if not isinstance(node, cst.Expr):
            return False
        value = node.value
        if isinstance(value, (cst.SimpleString, cst.ConcatenatedString)):
            return isinstance(value.evaluated_value, str)
        return False

    class Remover(cst.CSTTransformer):
        def __init__(self, drop_module: bool, drop_other: bool, preserve_commented_code: bool) -> None:
            super().__init__()
            self.drop_module = drop_module
            self.drop_other = drop_other
            self.preserve_commented_code = preserve_commented_code
            self.removed = 0

        def _is_commented_docstring(self, node: cst.BaseSmallStatement) -> bool:
            if not self.preserve_commented_code:
                return False
            content = _libcst_docstring_content(node)
            if content is None:
                return False
            return _is_commented_out_docstring(content)

        def strip_lines(self, body: Sequence[cst.BaseStatement], keep_empty: bool) -> list[cst.BaseStatement]:
            first = body[0] if body else None
            if not isinstance(first, cst.SimpleStatementLine) or not first.body or not is_docstring(first.body[0]):
                return list(body)
            if self._is_commented_docstring(first.body[0]):
                return list(body)
            self.removed += 1
            if len(first.body) > 1:
                return [first.with_changes(body=list(first.body[1:])), *body[1:]]
            if len(body) > 1 or keep_empty:
                return list(body[1:])
            return [first.with_changes(body=[cst.Pass()])]

        def strip_suite(self, suite: cst.BaseSuite) -> cst.BaseSuite:
            if isinstance(suite, cst.IndentedBlock):
                return suite.with_changes(body=self.strip_lines(suite.body, False))
            small = suite.body
            if small and is_docstring(small[0]):
                if self._is_commented_docstring(small[0]):
                    return suite
                self.removed += 1
                return suite.with_changes(body=list(small[1:]) or [cst.Pass()])
            return suite

        def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
            if not self.drop_module:
                return updated_node
            return updated_node.with_changes(body=self.strip_lines(updated_node.body, True))

        def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
            if not self.drop_other:
                return updated_node
            return updated_node.with_changes(body=self.strip_suite(updated_node.body))

        def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
            if not self.drop_other:
                return updated_node
            return updated_node.with_changes(body=self.strip_suite(updated_node.body))

    return Remover


def strip_libcst(text: str, opts: Options) -> Stripped:
    import libcst as cst

    remover = libcst_remover()(
        opts.drop_module_docstring,
        opts.drop_docstrings,
        not opts.strip_all,
    )
    code = cst.parse_module(text).visit(remover).code
    code, comments = strip_comments(code, opts)
    return Stripped(code, comments, remover.removed)


@cache
def tree_sitter_parser() -> Parser:
    from tree_sitter import Language, Parser
    import tree_sitter_python

    return Parser(Language(tree_sitter_python.language()))


def is_plain_string(node: Node, raw: bytes) -> bool:
    if node.type == "string":
        head = raw[node.start_byte : node.end_byte]
        prefix = STRING_PREFIX_RE.match(head)
        letters = prefix.group(0).lower() if prefix else b""
        return not any(letter in b"bft" for letter in letters)
    if node.type == "concatenated_string":
        parts = node.named_children
        return bool(parts) and all(part.type == "string" and is_plain_string(part, raw) for part in parts)
    return False


def _extract_string_content(raw: bytes, node: Node) -> str | None:
    try:
        text = raw[node.start_byte : node.end_byte].decode("utf-8", "replace")
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def tree_sitter_docstring(node: Node, raw: bytes, opts: Options) -> Edit | None:
    parent = node.parent
    if parent is None:
        return None
    if parent.type == "module":
        is_module = True
    elif (
        parent.type == "block"
        and parent.parent is not None
        and parent.parent.type in ("function_definition", "class_definition")
    ):
        is_module = False
    else:
        return None
    if is_module and not opts.drop_module_docstring:
        return None
    if not is_module and not opts.drop_docstrings:
        return None
    siblings = [child for child in parent.named_children if child.type != "comment"]
    if not siblings or siblings[0] != node:
        return None
    if len(node.named_children) != 1 or not is_plain_string(node.named_children[0], raw):
        return None
    if not opts.strip_all:
        content = _extract_string_content(raw, node.named_children[0])
        if content is not None and _is_commented_out_docstring(content):
            return None
    replacement = b"pass" if (not is_module and len(siblings) == 1) else b""
    return statement_edit(raw, node.start_byte, node.end_byte, replacement, "docstring")


def _tree_sitter_comment_edits(raw: bytes, comment_nodes: Sequence[Node], opts: Options) -> list[Edit]:
    entries: list[_CommentEntry] = []
    for node in comment_nodes:
        end = node.end_byte
        while end > node.start_byte and raw[end - 1] in (10, 13):
            end -= 1
        comment_text = raw[node.start_byte : end].decode("utf-8", "replace")
        line_start = raw.rfind(b"\n", 0, node.start_byte) + 1
        line_prefix = raw[line_start : node.start_byte]
        is_full_line = line_prefix.strip() == b""
        lineno = raw.count(b"\n", 0, node.start_byte) + 1
        edit = comment_edit(raw, line_start, node.start_byte, end)
        entries.append(_CommentEntry(lineno, is_full_line, comment_text, edit))
    entries.sort(key=lambda entry: entry.lineno)
    return _filter_comments(entries, opts)


def strip_tree_sitter(text: str, opts: Options) -> Stripped:
    raw = text.encode()
    tree = tree_sitter_parser().parse(raw)
    edits: list[Edit] = []
    comment_nodes: list[Node] = []
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "comment":
            comment_nodes.append(node)
        elif node.type == "expression_statement":
            edit = tree_sitter_docstring(node, raw, opts)
            if edit is not None:
                edits.append(edit)
        stack.extend(node.children)
    if opts.drop_comments:
        edits += _tree_sitter_comment_edits(raw, comment_nodes, opts)
    code, comments, docstrings = apply_edits(raw, edits)
    return Stripped(code.decode(), comments, docstrings)


BACKENDS: dict[str, Backend] = {
    "libcst": strip_libcst,
    "tree-sitter": strip_tree_sitter,
}


def backend_available(name: str) -> bool:
    if name == "libcst":
        return importlib.util.find_spec("libcst") is not None
    if name == "tree-sitter":
        return all(importlib.util.find_spec(module) is not None for module in ("tree_sitter", "tree_sitter_python"))
    return False


def resolve_backends(requested: str | None) -> tuple[str, ...]:
    if requested is not None and not backend_available(requested):
        raise SystemExit(INSTALL_HINTS.get(requested, f"Unknown backend: {requested}"))
    order: list[str] = [requested] if requested else []
    order += [name for name in DEFAULT_BACKENDS if name not in order and backend_available(name)]
    if not order:
        hints = "\n".join(INSTALL_HINTS.values())
        msg = f"No stripping backend available. Install one:\n{hints}"
        raise SystemExit(msg)
    return tuple(order)


def strip_source(text: str, opts: Options) -> Stripped:
    source_valid = parses(text)
    errors: list[Exception] = []
    primary = opts.backends[0] if opts.backends else None
    for index, name in enumerate(opts.backends):
        try:
            result = BACKENDS[name](text, opts)
            if source_valid:
                parse_tree(result.code)
            if index > 0 and primary is not None:
                logger.info("{} failed; fell back to {}", primary, name)
            return replace(result, backend=name)
        except Exception as exc:
            logger.debug("backend {} failed: {}", name, exc)
            errors.append(exc)
    syntax_error = next((error for error in errors if isinstance(error, SyntaxError)), None)
    if syntax_error is not None:
        raise syntax_error
    raise errors[-1] if errors else RuntimeError("no backend available")


def process_file(path: Path, opts: Options) -> FileResult:
    result = FileResult(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("I/O error reading {}: {}", path, exc)
        return result
    text, encoding = decode_source(raw)
    try:
        stripped = strip_source(text, opts)
    except SyntaxError as exc:
        result.error = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        logger.error("SyntaxError in {} line {}: {}", path, exc.lineno, exc.msg)
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.opt(exception=exc).error("Failed processing {}", path)
        return result
    updated = stripped.code.encode(encoding)
    result.backend = stripped.backend
    result.comments_removed = stripped.comments
    result.docstrings_removed = stripped.docstrings
    result.bytes_removed = max(0, len(raw) - len(updated))
    result.changed = updated != raw
    if opts.clipboard:
        result.final_code = stripped.code
        return result
    if result.changed and not opts.dry_run:
        try:
            atomic_write(path, updated)
        except OSError as exc:
            result.error = f"{type(exc).__name__} writing: {exc}"
            result.changed = False
            logger.opt(exception=exc).error("Write failed for {}", path)
    return result


def process_wheel(path: Path, opts: Options) -> FileResult:
    result = FileResult(path)
    wheel_backends: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(prefix="strip_whl_", dir=path.parent) as directory:
            staged = Path(directory) / path.name
            removed = comments = docstrings = 0
            modified = False
            with ExitStack() as stack:
                source = stack.enter_context(zipfile.ZipFile(path))
                broken = source.testzip()
                if broken is not None:
                    result.error = f"Corrupted wheel member: {broken}"
                    return result
                target = (
                    None
                    if opts.dry_run
                    else stack.enter_context(zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED))
                )
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename.endswith(".py"):
                        text, encoding = decode_source(data)
                        try:
                            stripped = strip_source(text, opts)
                        except Exception as exc:
                            logger.error("{} in {}!{}: {}", type(exc).__name__, path, info.filename, exc)
                        else:
                            if stripped.backend:
                                wheel_backends.add(stripped.backend)
                            updated = stripped.code.encode(encoding)
                            if updated != data:
                                removed += len(data) - len(updated)
                                comments += stripped.comments
                                docstrings += stripped.docstrings
                                modified = True
                                data = updated
                    if target is not None:
                        target.writestr(info, data)
            result.bytes_removed = max(0, removed)
            result.comments_removed = comments
            result.docstrings_removed = docstrings
            result.changed = modified
            result.backend = ",".join(sorted(wheel_backends))
            if modified and not opts.dry_run:
                with suppress(OSError):
                    staged.chmod(path.stat().st_mode & 0o7777)
                staged.replace(path)
    except zipfile.BadZipFile as exc:
        result.error = f"Invalid wheel archive: {exc}"
        logger.error("Invalid wheel {}: {}", path, exc)
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.opt(exception=exc).error("Wheel I/O failure {}", path)
    return result


def is_python_file(path: Path) -> bool:
    suffix = path.suffix
    if suffix == ".py":
        return True
    if suffix:
        return False
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
        if first.startswith(b"#!") and b"python" in first:
            return True
        if first.strip():
            return False
        if path.stat().st_size > MAX_SNIFF_SIZE:
            return False
        parse_tree(decode_source(path.read_bytes())[0])
        return True
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        return False


def is_candidate(path: Path) -> bool:
    return path.suffix == ".whl" or is_python_file(path)


def walk_directory(root: Path) -> Iterator[Path]:
    for current, directories, files in root.walk():
        directories[:] = [
            name
            for name in directories
            if name not in SKIP_DIRS and not any(PurePath(name).match(pattern) for pattern in SKIP_DIR_PATTERNS)
        ]
        for name in files:
            candidate = current / name
            if candidate.is_symlink() or not candidate.exists():
                continue
            if is_candidate(candidate):
                yield candidate


def discover(roots: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for root in roots:
        if root.is_file():
            found: Iterator[Path] = iter((root,)) if is_candidate(root) else iter(())
        elif root.is_dir():
            found = walk_directory(root)
        else:
            logger.error("Path does not exist: {}", root)
            continue
        for path in found:
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            yield path


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def format_name(name: str, width: int) -> str:
    if len(name) <= width:
        return name.ljust(width)
    if width <= 3:
        return name[:width]
    return name[: width - 1] + "…"


def report(index: int, result: FileResult, counter_width: int, name_width: int, bytes_width: int) -> None:
    name = display_path(result.path)
    counter = f"[{index}]".ljust(counter_width)
    name_col = format_name(name, name_width)
    line = Text()
    line.append(counter)
    line.append(" ")
    if result.error:
        line.append(name_col, style="bold red")
        line.append(" ERROR: ", style="bold red")
        line.append(result.error, style="red")
    elif result.changed:
        line.append(name_col, style="green")
        line.append(f" {result.bytes_removed:>{bytes_width}}", style="green")
    else:
        line.append(name_col, style="dim")
        line.append(f" {'(unchanged)':>{bytes_width}}", style="dim")
    console.print(line)


def summarize(results: Sequence[FileResult]) -> None:
    changed = sum(1 for item in results if item.changed)
    removed = sum(item.bytes_removed for item in results)
    comments = sum(item.comments_removed for item in results)
    docstrings = sum(item.docstrings_removed for item in results)
    errors = sum(1 for item in results if item.error)
    backend_counts: Counter[str] = Counter()
    for item in results:
        for name in item.backend.split(","):
            if name:
                backend_counts[name] += 1
    console.print()
    summary = Text()
    summary.append("\nSummary: ")
    summary.append(f"{len(results)} files | ")
    summary.append(f"{changed} changed", style="green" if changed else "dim")
    summary.append(f" | {removed} bytes removed | {comments} comments | {docstrings} docstrings | ")
    summary.append(f"{errors} errors", style="red" if errors else "dim")
    console.print(summary)
    if backend_counts:
        parts = ", ".join(f"{name}={count}" for name, count in sorted(backend_counts.items()))
        console.print(Text(f"Backends used: {parts}", style="dim"))


def copy_to_clipboard(code: str) -> None:
    try:
        subprocess.run(["termux-clipboard-set"], input=code, text=True, check=True)
    except FileNotFoundError:
        logger.error("termux-clipboard-set not found; install Termux:API")
    except subprocess.CalledProcessError as exc:
        logger.error("termux-clipboard-set failed: {}", exc)


def run_sources(paths: Sequence[Path], opts: Options) -> Iterator[FileResult]:
    if not paths:
        return
    worker = partial(process_file, opts=opts)
    if MAX_WORKERS <= 1:
        yield from map(worker, paths)
        return
    with Pool(workers, initializer=configure_logging) as pool:
        yield from pool.imap_unordered(worker, paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strip comments and docstrings from Python files.")
    parser.add_argument("paths", nargs="*", type=Path, help="Files or directories (default: cwd)")
    parser.add_argument(
        "-c",
        "--comments",
        action="store_true",
        help="Remove comments (full-line blocks that parse as code are preserved unless -a/-x).",
    )
    parser.add_argument(
        "-d",
        "--docstring",
        action="store_true",
        help="Remove docstrings except module docstring. Triple-quoted blocks whose content parses as code are preserved unless -a/-x.",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Remove all comments and docstrings, even preserved ones (including commented-out code).",
    )
    parser.add_argument(
        "-x",
        action="store_true",
        help="Equivalent to -a; nothing written, output copied to clipboard.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=sorted(BACKENDS),
        default=None,
        help=(
            "Preferred backend. Regardless of choice, any file that fails to parse or "
            "produce valid output with the preferred backend is retried with the others. "
            "Default order: libcst, then tree-sitter as fallback."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    parser.add_argument("--remove-module-docstring", action="store_true", help="Also strip module-level docstrings.")
    return parser


def make_options(args: argparse.Namespace) -> Options:
    strip_all: bool = args.all or args.x
    default_comments = not (args.comments or args.docstring or strip_all)
    return Options(
        strip_comments=args.comments or default_comments or strip_all,
        strip_docstrings=args.docstring,
        strip_all=strip_all,
        backends=resolve_backends(args.backend),
        dry_run=args.dry_run or args.x,
        remove_module_docstring=args.remove_module_docstring,
        clipboard=args.x,
    )


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    opts = make_options(args)
    roots: list[Path] = args.paths or [Path.cwd()]
    candidates = list(discover(roots))
    wheels = [path for path in candidates if path.suffix == ".whl"]
    sources = [path for path in candidates if path.suffix != ".whl"]
    total = len(sources) + len(wheels)
    counter_width = len(f"[{total}]") if total else 3
    bytes_width = BYTES_COL_WIDTH
    name_width = max(20, console.width - counter_width - bytes_width - 3)
    results: list[FileResult] = []
    index = 0
    for result in run_sources(sources, opts):
        index += 1
        report(index, result, counter_width, name_width, bytes_width)
        results.append(result)
    for wheel in wheels:
        index += 1
        result = process_wheel(wheel, opts)
        report(index, result, counter_width, name_width, bytes_width)
        results.append(result)
    summarize(results)
    if opts.clipboard:
        outputs = [item.final_code for item in results if item.final_code is not None]
        if len(outputs) == 1:
            copy_to_clipboard(outputs[0])
        elif outputs:
            copy_to_clipboard("\n".join(outputs))
        else:
            logger.warning("No output produced for clipboard")
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
