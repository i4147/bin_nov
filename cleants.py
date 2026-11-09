#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments from source files of many languages with tree-sitter.
Detects the language from the suffix or shebang (bash, C/C++, Python, JS/TS, CSS ...), keeps the vital comments (licences, ``@license``, lint pragmas) and the doc blocks (``--keep-doc``), and offers ``--inplace``, ``--dryrun``, ``--check``, ``--diff``, ``--backup``, ``--exclude`` and a size limit, validating every result before writing.
Usage: cleants [options] [{--inplace,--check,--diff}] [paths ...] Positional arguments: paths ...
Options: --inplace --dryrun --check --diff --color --backup --backup-suffix --keep-doc --exclude --max-size ...
plus 2 more; run ``cleants --help`` for the full list.
Examples: $ cleants --check --diff src/ Command-line script: run ``python cleants.py`` or the ``cleants`` command.
The module is importable as well."""

import argparse
from collections import Counter
from dataclasses import dataclass
import difflib
from functools import partial
import importlib
import multiprocessing as mp
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
from typing import Iterator

from atomicio import atomic_write
from dh import MAX_WORKERS
from tree_sitter import Language, Node, Parser, Tree


@dataclass(frozen=True, slots=True)
class LangSpec:
    module: str
    attr: str
    comment_types: frozenset[str]


_LANG_ATTR = "language"
LANGUAGES: dict[str, LangSpec] = {
    "bash": LangSpec("tree_sitter_bash", _LANG_ATTR, frozenset({"comment"})),
    "c": LangSpec("tree_sitter_c", _LANG_ATTR, frozenset({"comment"})),
    "cpp": LangSpec("tree_sitter_cpp", _LANG_ATTR, frozenset({"comment"})),
    "css": LangSpec("tree_sitter_css", _LANG_ATTR, frozenset({"comment"})),
    "go": LangSpec("tree_sitter_go", _LANG_ATTR, frozenset({"comment"})),
    "html": LangSpec("tree_sitter_html", _LANG_ATTR, frozenset({"comment"})),
    "java": LangSpec("tree_sitter_java", _LANG_ATTR, frozenset({"line_comment", "block_comment"})),
    "javascript": LangSpec("tree_sitter_javascript", _LANG_ATTR, frozenset({"comment"})),
    "latex": LangSpec(
        "tree_sitter_latex",
        _LANG_ATTR,
        frozenset({"comment", "line_comment", "block_comment", "comment_environment"}),
    ),
    "lua": LangSpec("tree_sitter_lua", _LANG_ATTR, frozenset({"comment"})),
    "perl": LangSpec("tree_sitter_perl", _LANG_ATTR, frozenset({"comment"})),
    "php": LangSpec("tree_sitter_php", "language_php", frozenset({"comment"})),
    "python": LangSpec("tree_sitter_python", _LANG_ATTR, frozenset({"comment"})),
    "query": LangSpec("tree_sitter_query", _LANG_ATTR, frozenset({"comment"})),
    "rst": LangSpec("tree_sitter_rst", _LANG_ATTR, frozenset({"comment"})),
    "ruby": LangSpec("tree_sitter_ruby", _LANG_ATTR, frozenset({"comment"})),
    "rust": LangSpec("tree_sitter_rust", _LANG_ATTR, frozenset({"line_comment", "block_comment"})),
    "sql": LangSpec("tree_sitter_sql", _LANG_ATTR, frozenset({"comment"})),
    "toml": LangSpec("tree_sitter_toml", _LANG_ATTR, frozenset({"comment"})),
    "vim": LangSpec("tree_sitter_vim", _LANG_ATTR, frozenset({"comment"})),
    "xml": LangSpec("tree_sitter_xml", "language_xml", frozenset({"Comment", "comment"})),
    "yaml": LangSpec("tree_sitter_yaml", _LANG_ATTR, frozenset({"comment"})),
}
EXTENSIONS: dict[str, str] = {
    ".sh": "bash",
    ".bash": "bash",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".htm": "html",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".tex": "latex",
    ".sty": "latex",
    ".cls": "latex",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".php": "php",
    ".py": "python",
    ".pyw": "python",
    ".scm": "query",
    ".rst": "rst",
    ".rb": "ruby",
    ".rs": "rust",
    ".sql": "sql",
    ".toml": "toml",
    ".vim": "vim",
    ".xml": "xml",
    ".svg": "xml",
    ".xsl": "xml",
    ".xsd": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
INTERPRETERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"python[\d.]*"), "python"),
    (re.compile(r"(ba|da|z|k|a)?sh"), "bash"),
    (re.compile(r"node(js)?"), "javascript"),
    (re.compile(r"perl[\d.]*"), "perl"),
    (re.compile(r"php[\d.]*"), "php"),
    (re.compile(r"ruby[\d.]*"), "ruby"),
    (re.compile(r"lua[\d.]*"), "lua"),
]
VITAL_RE = re.compile(
    r"copyright|licen[cs]e|spdx|\(c\)\s*\d{4}|@preserve|@license|@ts-|@jsx|@flow|"
    r"eslint|jshint|prettier-ignore|noqa|type:\s*ignore|pylint:|pyright:|mypy:|pragma|"
    r"nolint|nosec|nosonar|fmt:\s*(on|off|skip)|coding[:=]\s*[-\w.]+|go:\w|\+build|"
    r"%!|\[if\b|/\*!|[#/]\s*(end)?region\b|sourceMappingURL|sourceURL",
    re.IGNORECASE,
)
DOC_PREFIXES = (b"/**", b"///", b"//!")
DOCSTRING_TYPES = frozenset({"string", "concatenated_string"})
BODY_OWNERS = frozenset({"function_definition", "class_definition"})
DEFAULT_EXCLUDES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".ruff_cache",
    "build",
    "dist",
    "target",
})
CHANGED = frozenset({"changed", "would-change"})
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
CYAN = "\033[36m"


@dataclass(frozen=True, slots=True)
class Options:
    diff: bool
    backup: bool
    backup_suffix: str
    dryrun: bool
    inplace: bool
    check: bool
    keep_doc: bool
    max_size: int


@dataclass(frozen=True, slots=True)
class Comment:
    start: int
    end: int
    kind: str


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    replacement: bytes


@dataclass(frozen=True, slots=True)
class Result:
    path: Path
    status: str
    removed: int = 0
    reduced: int = 0
    message: str = ""
    diff: str = ""


_parsers: dict[str, Parser] = {}


def get_parser(lang: str) -> Parser:
    parser = _parsers.get(lang)
    if parser is None:
        spec = LANGUAGES[lang]
        module = importlib.import_module(spec.module)
        parser = Parser(Language(getattr(module, spec.attr)()))
        _parsers[lang] = parser
    return parser


def shebang_language(line: str) -> str | None:
    if not line.startswith("#!"):
        return None
    try:
        parts = shlex.split(line[2:])
    except ValueError:
        parts = line[2:].split()
    if not parts:
        return None
    interpreter = PurePosixPath(parts[0]).name
    if interpreter == "env":
        rest = [p for p in parts[1:] if not p.startswith("-") and "=" not in p]
        if not rest:
            return None
        interpreter = PurePosixPath(rest[0]).name
    for pattern, lang in INTERPRETERS:
        if pattern.fullmatch(interpreter):
            return lang
    return None


def detect_language(path: Path, head: bytes) -> str | None:
    suffix = path.suffix.lower()
    if suffix in EXTENSIONS:
        return EXTENSIONS[suffix]
    if head.startswith(b"#!"):
        first_line = head.split(b"\n", 1)[0].decode("utf-8", "replace").rstrip("\r")
        return shebang_language(first_line)
    return None


def collect_comments(root: Node, kinds: frozenset[str]) -> list[Comment]:
    found: list[Comment] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            continue
        if node.type in kinds:
            found.append(Comment(node.start_byte, node.end_byte, node.type))
            continue
        stack.extend(node.children)
    return found


def is_vital(src: bytes, comment: Comment, keep_doc: bool) -> bool:
    text = src[comment.start : comment.end]
    if comment.start == 0 and text.startswith(b"#!"):
        return True
    if VITAL_RE.search(text.decode("utf-8", "replace")):
        return True
    return keep_doc and text.startswith(DOC_PREFIXES) and text != b"/**/"


def bodyless_docstrings(root: Node) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type in BODY_OWNERS:
            body = node.child_by_field_name("body")
            if body is not None:
                statements = [c for c in body.named_children if c.type != "comment"]
                if (
                    len(statements) == 1
                    and statements[0].type == "expression_statement"
                    and statements[0].named_child_count == 1
                    and statements[0].named_children[0].type in DOCSTRING_TYPES
                ):
                    spans.append((statements[0].start_byte, statements[0].end_byte))
        stack.extend(node.children)
    return spans


def line_aware_ranges(src: bytes, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        line_start = src.rfind(b"\n", 0, start) + 1
        newline = src.find(b"\n", end)
        line_end = len(src) if newline == -1 else newline
        if not src[line_start:start].strip() and not src[end:line_end].strip():
            ranges.append((line_start, min(line_end + 1, len(src))))
        else:
            cut = start
            while cut > line_start and src[cut - 1] in b" \t":
                cut -= 1
            ranges.append((cut, end))
    return ranges


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_edits(
    src: bytes,
    comment_spans: list[tuple[int, int]],
    docstring_spans: list[tuple[int, int]],
) -> list[Edit]:
    edits = [Edit(s, e, b"") for s, e in merge_ranges(line_aware_ranges(src, comment_spans))]
    edits.extend(Edit(s, e, b"pass") for s, e in docstring_spans)
    return sorted(edits, key=lambda ed: ed.start)


def apply_edits(src: bytes, edits: list[Edit]) -> bytes:
    out = bytearray()
    pos = 0
    for edit in edits:
        if edit.start < pos:
            msg = "overlapping edits"
            raise ValueError(msg)
        out += src[pos : edit.start]
        out += edit.replacement
        pos = edit.end
    out += src[pos:]
    return bytes(out)


def signatures(
    root: Node,
    src: bytes,
    kinds: frozenset[str],
    ignored: frozenset[tuple[int, int]],
    substituted: frozenset[tuple[int, int]],
) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            continue
        key = (node.start_byte, node.end_byte)
        if node.type in kinds and key in ignored:
            continue
        if node.type == "expression_statement" and key in substituted:
            out.append(("pass", b"pass"))
            continue
        if node.child_count == 0:
            out.append((node.type, src[node.start_byte : node.end_byte]))
            continue
        stack.extend(reversed(node.children))
    return out


def validate(
    old_tree: Tree,
    src: bytes,
    new_src: bytes,
    parser: Parser,
    kinds: frozenset[str],
    comment_spans: list[tuple[int, int]],
    docstring_spans: list[tuple[int, int]],
) -> str | None:
    new_tree = parser.parse(new_src)
    if new_tree.root_node.has_error and not old_tree.root_node.has_error:
        return "output introduces parse errors"
    before = signatures(
        old_tree.root_node,
        src,
        kinds,
        frozenset(comment_spans),
        frozenset(docstring_spans),
    )
    after = signatures(new_tree.root_node, new_src, kinds, frozenset(), frozenset())
    if before != after:
        return "token stream changed beyond intended edits"
    return None


def process_file(path: Path, opts: Options) -> Result:
    try:
        if path.is_symlink() or not path.is_file():
            return Result(path, "skipped", message="not a regular file")
        if path.stat().st_size > opts.max_size:
            return Result(path, "skipped", message="exceeds max size")
        with path.open("rb") as fh:
            head = fh.read(1024)
        if b"\x00" in head:
            return Result(path, "skipped", message="binary file")
        lang = detect_language(path, head)
        if lang is None:
            return Result(path, "skipped", message="unsupported type")
        spec = LANGUAGES[lang]
        parser = get_parser(lang)
        src = path.read_bytes()
        tree = parser.parse(src)
        comments = collect_comments(tree.root_node, spec.comment_types)
        removable = [c for c in comments if not is_vital(src, c, opts.keep_doc)]
        comment_spans = [(c.start, c.end) for c in removable]
        docstring_spans = bodyless_docstrings(tree.root_node) if lang == "python" else []
        if not comment_spans and not docstring_spans:
            return Result(path, "unchanged")
        new_src = apply_edits(src, build_edits(src, comment_spans, docstring_spans))
        if new_src == src:
            return Result(path, "unchanged")
        problem = validate(
            tree,
            src,
            new_src,
            parser,
            spec.comment_types,
            comment_spans,
            docstring_spans,
        )
        if problem:
            return Result(path, "rejected", message=problem)
        reduced = len(src) - len(new_src)
        diff = ""
        if opts.diff:
            diff = "".join(
                difflib.unified_diff(
                    src.decode("utf-8", "replace").splitlines(True),
                    new_src.decode("utf-8", "replace").splitlines(True),
                    fromfile=str(path),
                    tofile=f"{path} (stripped)",
                )
            )
        if opts.dryrun or opts.check:
            return Result(path, "would-change", len(comment_spans), reduced, diff=diff)
        if opts.inplace:
            target = path
            if opts.backup:
                shutil.copy2(path, path.with_name(path.name + opts.backup_suffix))
        else:
            target = path.with_name(f"{path.stem}.stripped{path.suffix}")
        atomic_write(target, new_src, mode_from=path)
        return Result(path, "changed", len(comment_spans), reduced, diff=diff)
    except ImportError as exc:
        return Result(path, "skipped", message=f"grammar not installed: {exc.name}")
    except Exception as exc:
        return Result(path, "error", message=str(exc))


def iter_files(root: Path, excludes: frozenset[str]) -> Iterator[Path]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in excludes:
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def discover(inputs: list[str], excludes: frozenset[str]) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    seen: set[Path] = set()
    problems: list[str] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_symlink():
            problems.append(f"symlink skipped: {raw}")
            continue
        if not path.exists():
            problems.append(f"not found: {raw}")
            continue
        candidates: Iterator[Path]
        if path.is_file():
            candidates = iter([path])
        elif path.is_dir():
            candidates = iter_files(path, excludes)
        else:
            candidates = iter([])
        for candidate in candidates:
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                files.append(candidate)
    return files, problems


def display_path(path: Path) -> str:
    try:
        return Path(path.resolve()).relative_to(Path.cwd())
    except ValueError:
        return str(path)


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def colorize_diff(diff: str, enabled: bool) -> str:
    if not enabled:
        return diff
    lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if body.startswith(("+++", "---")):
            code = BOLD
        elif body.startswith("@@"):
            code = CYAN
        elif body.startswith("+"):
            code = GREEN
        elif body.startswith("-"):
            code = RED
        else:
            lines.append(line)
            continue
        lines.append(f"{code}{body}{RESET}{ending}")
    return "".join(lines)


def human_size(n: int) -> str:
    sign = "-" if n < 0 else ""
    size = float(abs(n))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            break
        size /= 1024
    if unit == "B":
        return f"{sign}{int(size)} B"
    return f"{sign}{size:.2f} {unit}"


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def print_report(results: list[Result], args: argparse.Namespace, color: bool) -> None:
    changed = sorted(
        (r for r in results if r.status in CHANGED),
        key=lambda r: display_path(r.path),
    )
    if args.diff:
        for res in changed:
            if res.diff:
                sys.stdout.write(colorize_diff(res.diff, color))
    if not args.quiet and changed:
        names = [display_path(r.path) for r in changed]
        numbers = [str(r.reduced) for r in changed]
        name_width = max(len(name) for name in names)
        number_width = max(len(num) for num in numbers)
        for res, name in zip(changed, names):
            code = GREEN if res.reduced >= 0 else RED
            padded = paint(f"{res.reduced:>{number_width}}", code, color)
            print(f"{name:<{name_width}}  {padded}")
    for res in results:
        if res.status in {"rejected", "error"}:
            print(f"{res.status}: {display_path(res.path)}: {res.message}", file=sys.stderr)
        elif args.verbose and res.status == "unchanged":
            print(f"unchanged: {display_path(res.path)}")
        elif args.verbose and res.status == "skipped":
            print(f"skipped: {display_path(res.path)} ({res.message})")


def print_summary(results: list[Result], args: argparse.Namespace, color: bool) -> None:
    if args.quiet:
        return
    counts = Counter(r.status for r in results)
    total = sum(r.reduced for r in results if r.status in CHANGED)
    label = "Space that would be freed" if (args.dryrun or args.check) else "Space freed"
    code = GREEN if total >= 0 else RED
    number = paint(f"{total:,}", code, color)
    human = human_size(total)
    print()
    print(f"{label}: {number} bytes ({human})")
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"files: {summary}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stripcomments",
        description="Strip comments from source files using tree-sitter, safely and in parallel.",
    )
    parser.add_argument("paths", nargs="*", default=["."], metavar="PATH")
    parser.add_argument("--inplace", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dryrun", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--backup-suffix", default=".bak")
    parser.add_argument("--keep-doc", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME")
    parser.add_argument("--max-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if MAX_WORKERS < 1:
        parser.error("--jobs must be >= 1")
    color = use_color(args.color)
    excludes = DEFAULT_EXCLUDES | frozenset(args.exclude)
    files, problems = discover(args.paths, excludes)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not files:
        print("no candidate files found")
        return 2 if problems else 0
    opts = Options(
        diff=args.diff,
        backup=args.backup,
        backup_suffix=args.backup_suffix,
        dryrun=args.dryrun,
        inplace=args.inplace,
        check=args.check,
        keep_doc=args.keep_doc,
        max_size=args.max_size,
    )
    worker = partial(process_file, opts=opts)
    results: list[Result] = []
    with mp.Pool(processes=MAX_WORKERS) as pool:
        for res in pool.imap_unordered(worker, files, chunksize=4):
            results.append(res)
    print_report(results, args, color)
    print_summary(results, args, color)
    counts = Counter(r.status for r in results)
    if counts["error"] or counts["rejected"] or problems:
        return 2
    if args.check and counts["would-change"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
