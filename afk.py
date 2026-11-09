#!/data/data/com.termux/files/usr/bin/env python
"""Find unused imports in Python files, directories and archives and optionally remove them.
Scans each source file for imports that are never referenced, honouring ``__all__``, ``# noqa`` markers and ``try`` blocks.
Archives (``.zip``, ``.tar``, ``.tar.gz``/``bz2``/``xz``, wheel and zstandard) are unpacked in memory, so the archive itself is only rewritten when a fix is requested.
Usage: afk [options] [PATHS ...] Positional arguments: paths ...
Python file, directory, or archive to scan (repeatable) Options: -x, --exclude Glob pattern matched against relative paths and each path component; repeatable (e.g.
-x 'tests/*' -x migrations) --no-default-excludes Do not skip .git, venv, __pycache__, node_modules and similar --check Report only and exit with status 1 if unused imports are found --fix Write the cleaned source back to disk -n, --dry-run Show a unified diff of proposed removals and never write files --diff Show a unified diff preview of proposed removals --include-init Also analyze __init__.py files (skipped by default because imports there are usually re-exports) --color Colorize output (default: auto, only when writing to a terminal) -q, --quiet Print only errors and the summary Examples: $ afk src/ --check $ afk dist/pkg.whl --fix --diff Command-line script: run ``python afk.py`` or the ``afk`` command.
The module is importable as well."""

import argparse
import ast
import bz2
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
import difflib
import fnmatch
import gzip
import io
import itertools
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tokenize
from typing import IO, Any, Callable, Iterator, Sequence, Union
import warnings
import zipfile

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS


MAX_FILE_BYTES: int = 8 * 1024 * 1024
LINE_LIMIT: int = 88
ZSTD_HINT: str = "zstandard support is unavailable (pip install zstandard)"
TRY_NODES: tuple[type, ...] = tuple(
    node_type for node_type in (ast.Try, getattr(ast, "TryStar", None)) if node_type is not None
)
ImportNode = ast.Import | ast.ImportFrom
EPILOG: str = """
examples:
  %(prog)s src/                           report unused imports in a tree
  %(prog)s app.py --diff                  show a diff preview for one file
  %(prog)s . --dry-run                    preview proposed removals, write nothing
  %(prog)s . --fix --dry-run              same preview, explicitly in fix mode
  %(prog)s . --fix                        remove unused imports in place
  %(prog)s . --check -x "tests/*" -j 8    CI gate: exit 1 if any are found
  %(prog)s dist/pkg.zip                   scan a zip archive without extracting
  %(prog)s rel.tar.gz rel.tar.zst         scan tar archives (zstd needs 'zstandard')
  %(prog)s module.py.gz                   scan a single compressed source file
exit status:
  0  nothing to report (or findings reported outside --check mode)
  1  --check found unused imports
  2  at least one file could not be read, parsed or written
"""


@dataclass(frozen=True, slots=True)
class SourceItem:
    display: str
    path: Path | None = None
    data: bytes | None = None
    error: str | None = None
    is_init: bool = False


@dataclass(frozen=True, slots=True)
class UnusedImport:
    lineno: int
    name: str
    fixable: bool


@dataclass(slots=True)
class FileResult:
    display: str
    path: Path | None
    unused: list[UnusedImport] = field(default_factory=list)
    removable: int = 0
    diff: str = ""
    new_bytes: bytes | None = None
    error: str | None = None


@dataclass(slots=True)
class Totals:
    scanned: int = 0
    with_unused: int = 0
    found: int = 0
    removable: int = 0
    removed: int = 0
    modified: int = 0
    errors: int = 0


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.wrap("1", text)

    def dim(self, text: str) -> str:
        return self.wrap("2", text)

    def red(self, text: str) -> str:
        return self.wrap("31", text)

    def green(self, text: str) -> str:
        return self.wrap("32", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def cyan(self, text: str) -> str:
        return self.wrap("36", text)


def color_enabled(mode: str, stream: IO[str]) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    return stream.isatty()


class UsageCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Load, ast.Del)):
            self.names.add(node.id)

    def add_expression_text(self, text: str) -> None:
        try:
            parsed = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            return
        for child in ast.walk(parsed):
            if isinstance(child, ast.Name):
                self.names.add(child.id)

    def add_string_annotation(self, node: ast.AST | None) -> None:
        if node is None:
            return
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                self.add_expression_text(child.value)

    def visit_arg(self, node: ast.arg) -> None:
        self.add_string_annotation(node.annotation)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.add_string_annotation(node.returns)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.add_string_annotation(node.annotation)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            target = func.attr
        elif isinstance(func, ast.Name):
            target = func.id
        else:
            target = ""
        if target == "cast" and node.args:
            self.add_string_annotation(node.args[0])
        self.generic_visit(node)


def exported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            for element in ast.walk(value):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    names.add(element.value)
    return names


def build_parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def inside_try(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, TRY_NODES):
            return True
        current = parents.get(id(current))
    return False


def containing_list(node: ast.AST, parents: dict[int, ast.AST]) -> list[ast.stmt] | None:
    parent = parents.get(id(node))
    if parent is None:
        return None
    for name in ("body", "orelse", "finalbody"):
        value = getattr(parent, name, None)
        if isinstance(value, list) and any(item is node for item in value):
            return value
    return None


def has_noqa(lines: list[str], node: ImportNode) -> bool:
    end = node.end_lineno or node.lineno
    return any("noqa" in line.lower() for line in lines[node.lineno - 1 : end])


def is_unused(node: ImportNode, alias: ast.alias, used: set[str]) -> bool:
    if alias.name == "*":
        return False
    if alias.asname is not None and alias.asname == alias.name:
        return False
    if isinstance(node, ast.Import):
        bound = alias.asname or alias.name.split(".")[0]
    else:
        bound = alias.asname or alias.name
    return bound not in used


def alias_display(node: ImportNode, alias: ast.alias) -> str:
    if isinstance(node, ast.Import):
        base = alias.name
    else:
        prefix = "." * node.level + (node.module + "." if node.module else "")
        base = prefix + alias.name
    if alias.asname:
        return f"{base} as {alias.asname}"
    return base


def char_index(line: str, byte_offset: int) -> int:
    return len(line.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\n", "\r")):
        return line[-1]
    return ""


def statement_span(lines: list[str], node: ImportNode) -> tuple[str, str, bool]:
    first = lines[node.lineno - 1]
    last = lines[(node.end_lineno or node.lineno) - 1]
    start = char_index(first, node.col_offset)
    end = char_index(last, node.end_col_offset or 0)
    indent = first[:start]
    tail = last[end:].strip()
    fixable = indent.strip() == "" and (tail == "" or tail.startswith("#"))
    return indent, tail, fixable


def render_import(node: ImportNode, kept: list[ast.alias], indent: str, newline: str) -> str:
    parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in kept]
    if isinstance(node, ast.Import):
        return indent + "import " + ", ".join(parts)
    head = "from " + "." * node.level + (node.module or "") + " import "
    single = indent + head + ", ".join(parts)
    if len(single) <= LINE_LIMIT:
        return single
    inner = indent + "    "
    body = "".join(inner + part + "," + newline for part in parts)
    return indent + head + "(" + newline + body + indent + ")"


def analyze_source(source: str) -> tuple[list[UnusedImport], str | None, int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    collector = UsageCollector()
    collector.visit(tree)
    used = collector.names | exported_names(tree)
    parents = build_parents(tree)
    lines = io.StringIO(source, newline="").readlines()
    grouped: dict[int, tuple[ImportNode, list[ast.alias]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if inside_try(node, parents) or has_noqa(lines, node):
            continue
        for alias in node.names:
            if is_unused(node, alias, used):
                grouped.setdefault(id(node), (node, []))[1].append(alias)
    spans = {key: statement_span(lines, node) for key, (node, _) in grouped.items()}
    full = {key for key, (node, aliases) in grouped.items() if spans[key][2] and len(aliases) == len(node.names)}
    unused: list[UnusedImport] = []
    edits: list[tuple[int, int, list[str]]] = []
    removable = 0
    for key, (node, aliases) in grouped.items():
        indent, tail, fixable = spans[key]
        for alias in aliases:
            unused.append(UnusedImport(node.lineno, alias_display(node, alias), fixable))
        if not fixable:
            continue
        removable += len(aliases)
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        ending = line_ending(lines[end - 1])
        if key in full:
            container = containing_list(node, parents)
            parent = parents.get(id(node))
            emptied = (
                container is not None
                and not isinstance(parent, ast.Module)
                and all(id(stmt) in full for stmt in container)
            )
            if emptied and container is not None and container[0] is node:
                edits.append((start, end, [indent + "pass" + ending]))
            else:
                edits.append((start, end, []))
        else:
            kept = [a for a in node.names if not any(a is u for u in aliases)]
            text = render_import(node, kept, indent, ending or "\n")
            if tail:
                text += "  " + tail
            edits.append((start, end, [text + ending]))
    unused.sort(key=lambda item: (item.lineno, item.name))
    if not edits:
        return unused, None, 0
    rewritten = list(lines)
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        rewritten[start:end] = replacement
    new_source = "".join(rewritten)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(new_source)
    except (SyntaxError, ValueError, RecursionError):
        return [replace(item, fixable=False) for item in unused], None, 0
    return unused, new_source, removable


def decode_source(data: bytes) -> tuple[str, str]:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding


def make_diff(display: str, old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{display}",
            tofile=f"b/{display}",
            lineterm="",
        )
    )


def process_item(item: SourceItem, include_init: bool) -> FileResult:
    result = FileResult(display=item.display, path=item.path)
    if item.error is not None:
        result.error = item.error
        return result
    try:
        data = item.data
        if data is None:
            if item.path is None:
                msg = "no source available"
                raise OSError(msg)
            if item.path.stat().st_size > MAX_FILE_BYTES:
                msg = "file exceeds size limit"
                raise OSError(msg)
            data = item.path.read_bytes()
        source, encoding = decode_source(data)
    except (OSError, SyntaxError, ValueError, LookupError) as exc:
        result.error = f"cannot read file: {exc}"
        return result
    if item.is_init and not include_init:
        return result
    try:
        unused, new_source, removable = analyze_source(source)
        result.unused = unused
        result.removable = removable
        if new_source is not None:
            result.diff = make_diff(item.display, source, new_source)
            result.new_bytes = new_source.encode(encoding)
    except SyntaxError as exc:
        location = f" (line {exc.lineno})" if exc.lineno else ""
        result.error = f"syntax error: {exc.msg}{location}"
    except (ValueError, RecursionError, MemoryError) as exc:
        result.error = f"cannot parse file: {exc}"
    except Exception as exc:
        result.error = f"internal error: {exc!r}"
    return result


def matches_any(relative: str, patterns: Sequence[str]) -> bool:
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    for pattern in patterns:
        cleaned = pattern.rstrip("/")
        if not cleaned:
            continue
        if fnmatch.fnmatchcase(normalized, cleaned):
            return True
        if any(fnmatch.fnmatchcase(part, cleaned) for part in parts):
            return True
    return False


def load_zstd() -> Any:
    try:
        import zstandard
    except ImportError:
        return None
    return zstandard


def read_limited(handle: IO[bytes]) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = handle.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            msg = "file exceeds size limit"
            raise ValueError(msg)
        chunks.append(chunk)
    return b"".join(chunks)


def read_single_compressed(path: Path) -> bytes:
    suffix = path.suffix.lower()
    if suffix in {".zst", ".zstd"}:
        module = load_zstd()
        if module is None:
            raise RuntimeError(ZSTD_HINT)
        with path.open("rb") as raw:
            reader = module.ZstdDecompressor().stream_reader(raw)
            return read_limited(reader)
    openers: dict[str, Callable[..., IO[bytes]]] = {
        ".gz": gzip.open,
        ".bz2": bz2.open,
        ".xz": lzma.open,
    }
    with openers[suffix](path, "rb") as handle:
        return read_limited(handle)


def classify(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".tar.zst", ".tar.zstd", ".tzst")):
        return "tar-zstd"
    if name.endswith((".py.zst", ".py.zstd", ".py.gz", ".py.bz2", ".py.xz")):
        return "py-compressed"
    if name.endswith(".py"):
        return "py"
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        return "py"
    return "py"


def iter_directory(root: Path, patterns: Sequence[str]) -> Iterator[SourceItem]:
    for current, dirs, files in os.walk(root):
        base = Path(current)
        relative_dir = base.relative_to(root)
        dirs[:] = sorted(d for d in dirs if not matches_any((relative_dir / d).as_posix(), patterns))
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            if matches_any((relative_dir / name).as_posix(), patterns):
                continue
            path = base / name
            yield SourceItem(display=str(path), path=path, is_init=name == "__init__.py")


def iter_zip(path: Path, patterns: Sequence[str]) -> Iterator[SourceItem]:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        yield SourceItem(display=str(path), error=f"cannot open archive: {exc}")
        return
    with archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            if info.is_dir() or not info.filename.endswith(".py"):
                continue
            if matches_any(info.filename, patterns):
                continue
            display = f"{path}::{info.filename}"
            if info.file_size > MAX_FILE_BYTES:
                yield SourceItem(display=display, error="file exceeds size limit")
                continue
            try:
                data = archive.read(info)
            except Exception as exc:
                yield SourceItem(display=display, error=f"cannot read member: {exc}")
                continue
            yield SourceItem(
                display=display,
                data=data,
                is_init=PurePosixPath(info.filename).name == "__init__.py",
            )


def iter_tar_members(tar: tarfile.TarFile, label: str, patterns: Sequence[str]) -> Iterator[SourceItem]:
    for member in tar:
        if not member.isfile() or not member.name.endswith(".py"):
            continue
        name = re.sub(r"^(\./)+", "", member.name)
        if matches_any(name, patterns):
            continue
        display = f"{label}::{name}"
        if member.size > MAX_FILE_BYTES:
            yield SourceItem(display=display, error="file exceeds size limit")
            continue
        try:
            handle = tar.extractfile(member)
            if handle is None:
                msg = "member is not readable"
                raise OSError(msg)
            data = handle.read()
        except Exception as exc:
            yield SourceItem(display=display, error=f"cannot read member: {exc}")
            continue
        yield SourceItem(
            display=display,
            data=data,
            is_init=PurePosixPath(name).name == "__init__.py",
        )


def iter_tar(path: Path, patterns: Sequence[str]) -> Iterator[SourceItem]:
    try:
        tar = tarfile.open(path, mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        yield SourceItem(display=str(path), error=f"cannot open archive: {exc}")
        return
    with tar:
        try:
            yield from iter_tar_members(tar, str(path), patterns)
        except (tarfile.TarError, OSError, EOFError) as exc:
            yield SourceItem(display=str(path), error=f"archive read error: {exc}")


def iter_tar_zstd(path: Path, patterns: Sequence[str]) -> Iterator[SourceItem]:
    module = load_zstd()
    if module is None:
        yield SourceItem(display=str(path), error=ZSTD_HINT)
        return
    try:
        with path.open("rb") as raw:
            reader = module.ZstdDecompressor().stream_reader(raw)
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                yield from iter_tar_members(tar, str(path), patterns)
    except Exception as exc:
        yield SourceItem(display=str(path), error=f"archive read error: {exc}")


def iter_single_compressed(path: Path) -> Iterator[SourceItem]:
    inner = path.name[: path.name.rfind(".")]
    try:
        data = read_single_compressed(path)
    except Exception as exc:
        yield SourceItem(display=str(path), error=f"cannot decompress: {exc}")
        return
    yield SourceItem(display=str(path), data=data, is_init=inner == "__init__.py")


def discover(target: Path, patterns: Sequence[str]) -> Iterator[SourceItem]:
    if target.is_dir():
        yield from iter_directory(target, patterns)
        return
    kind = classify(target)
    if kind == "zip":
        yield from iter_zip(target, patterns)
    elif kind == "tar":
        yield from iter_tar(target, patterns)
    elif kind == "tar-zstd":
        yield from iter_tar_zstd(target, patterns)
    elif kind == "py-compressed":
        yield from iter_single_compressed(target)
    else:
        yield SourceItem(display=str(target), path=target, is_init=target.name == "__init__.py")


def run_analysis(items: list[SourceItem], include_init: bool, workers: int) -> Iterator[FileResult]:
    if workers <= 1 or len(items) < 2:
        for item in items:
            yield process_item(item, include_init)
        return
    chunk = max(1, len(items) // (workers * 4))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(process_item, items, itertools.repeat(include_init), chunksize=chunk)


def format_diff(diff: str, style: Style) -> str:
    rendered: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            rendered.append(style.bold(line))
        elif line.startswith("@@"):
            rendered.append(style.cyan(line))
        elif line.startswith("+"):
            rendered.append(style.green(line))
        elif line.startswith("-"):
            rendered.append(style.red(line))
        else:
            rendered.append(line)
    return "\n".join(rendered)


def print_file_report(result: FileResult, style: Style, show_diff: bool, out: IO[str]) -> None:
    count_label = style.dim(f"({len(result.unused)} unused)")
    print(f"{style.bold(result.display)} {count_label}", file=out)
    for item in result.unused:
        line_label = style.cyan(f"{item.lineno:>5}")
        name_label = style.red(item.name)
        note = "" if item.fixable else style.yellow("  (manual fix needed)")
        print(f"  {line_label}  {name_label}{note}", file=out)
    if show_diff and result.diff:
        print(file=out)
        print(format_diff(result.diff, style), file=out)
    print(file=out)


def emit(
    result: FileResult,
    totals: Totals,
    style: Style,
    show_diff: bool,
    will_write: bool,
    quiet: bool,
    out: IO[str],
) -> None:
    totals.scanned += 1
    if result.error is not None:
        totals.errors += 1
        print(
            f"{style.bold(result.display)}  {style.red('error:')} {result.error}\n",
            file=out,
        )
        return
    if not result.unused:
        return
    totals.with_unused += 1
    totals.found += len(result.unused)
    totals.removable += result.removable
    if not quiet:
        print_file_report(result, style, show_diff, out)
    if will_write and result.new_bytes is not None and result.path is not None:
        try:
            atomic_write(result.path, result.new_bytes)
        except OSError as exc:
            totals.errors += 1
            print(
                f"{style.bold(result.display)}  {style.red('write failed:')} {exc}\n",
                file=out,
            )
        else:
            totals.removed += result.removable
            totals.modified += 1


def print_summary(totals: Totals, will_write: bool, style: Style, out: IO[str]) -> None:
    if will_write:
        action_row = ("Imports removed", totals.removed)
    else:
        action_row = ("Imports that would be removed", totals.removable)
    rows: list[tuple[str, int]] = [
        ("Files scanned", totals.scanned),
        ("Files with unused imports", totals.with_unused),
        ("Unused imports found", totals.found),
        action_row,
    ]
    if will_write:
        rows.append(("Files modified", totals.modified))
    rows.append(("Files with errors", totals.errors))
    width = max(len(label) for label, _ in rows)
    print(style.bold("Summary"), file=out)
    for label, value in rows:
        number = f"{value:>6}"
        if label == "Files with errors" and value:
            number = style.red(number)
        elif label == "Unused imports found" and value:
            number = style.yellow(number)
        print(f"  {label:<{width}}  {number}", file=out)


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        msg = f"invalid integer: {text}"
        raise argparse.ArgumentTypeError(msg) from exc
    if value < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unused_imports.py",
        description=(
            "Find unused imports in Python files, directories, and archives "
            "(zip, tar, tar.gz/bz2/xz, and zstandard when installed), "
            "optionally removing them in place."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Python file, directory, or archive to scan (repeatable)",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="glob pattern matched against relative paths and each path "
        "component; repeatable (e.g. -x 'tests/*' -x migrations)",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="do not skip .git, venv, __pycache__, node_modules and similar",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="report only and exit with status 1 if unused imports are found",
    )
    mode.add_argument(
        "--fix",
        action="store_true",
        help="write the cleaned source back to disk",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show a unified diff of proposed removals and never write files",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="show a unified diff preview of proposed removals",
    )
    parser.add_argument(
        "--include-init",
        action="store_true",
        help="also analyze __init__.py files (skipped by default because imports there are usually re-exports)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output (default: auto, only when writing to a terminal)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only errors and the summary",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    patterns: list[str] = list(args.exclude)
    if not args.no_default_excludes:
        patterns = list(SKIP_DIRS) + patterns
    will_write: bool = bool(args.fix and not args.dry_run)
    show_diff: bool = bool(args.diff or args.dry_run)
    targets = [Path(raw) for raw in args.paths]
    for target in targets:
        if not target.exists():
            parser.error(f"path does not exist: {target}")
        if will_write and not target.is_dir() and classify(target) != "py":
            parser.error(f"cannot modify archive or compressed input: {target}")
    style = Style(color_enabled(args.color, sys.stdout))
    totals = Totals()
    workers: int = MAX_WORKERS or min(32, os.cpu_count() or 1)
    try:
        items = [item for target in targets for item in discover(target, patterns)]
        for result in run_analysis(items, args.include_init, workers):
            emit(
                result,
                totals,
                style,
                show_diff,
                will_write,
                args.quiet,
                sys.stdout,
            )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    print_summary(totals, will_write, style, sys.stdout)
    if totals.errors:
        return 2
    if args.check and totals.found:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
