#!/data/data/com.termux/files/usr/bin/env python
"""Fix the escaped regexes, invalid escape sequences, bytes literals and ``is`` comparisons.
Rewrites the regex patterns and the string literals that should be raw, doubles the invalid escapes, fixes the non-ASCII bytes and the ``is`` comparisons with literals (``--no-is`` skips that); ``--check`` makes it a CI gate, ``--diff``/``--backup``/``--dry-run`` control the output.
Usage: pyenhance [options] [paths ...] Positional arguments: paths ...
Files or directories (default: current directory, recursive) Options: -s, --select --no-is Do not rewrite `is` comparisons with literals -n, --dry-run Report only, write nothing --check Report only; exit 1 if any file needs fixes (for CI / pre-commit) --diff Print a unified diff for each changed file --backup Keep a .bak copy of each changed file -e, --exclude Directory name to skip (repeatable) Examples: $ pyenhance --check src/ $ pyenhance -s escape --diff src/ Command-line script: run ``python pyenhance.py`` or the ``pyenhance`` command.
The module is importable as well."""

import argparse
import ast
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
import difflib
from functools import partial
import io
import multiprocessing as mp
from pathlib import Path
import re
import sys
import tokenize
from typing import TYPE_CHECKING, NamedTuple
import warnings

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


PYTHON_SUFFIXES: frozenset[str] = frozenset({".py", ".pyw", ".pyi"})
ALL_FIXES: frozenset[str] = frozenset({"regex", "escape", "bytes", "is"})
RE_FUNCS: frozenset[str] = frozenset({
    "compile",
    "match",
    "search",
    "fullmatch",
    "sub",
    "subn",
    "split",
    "findall",
    "finditer",
})
PATTERN_NAME_RE = re.compile(r"pattern|regex|regexp|_re$|^re_", re.IGNORECASE)
LITERAL_RE = re.compile(r"([A-Za-z]*)('''|\"\"\"|'|\")(.*)\2", re.DOTALL)
IS_OP_RE = re.compile(rb"\bis\s+not\b|\bis\b")
STR_ESCAPES: frozenset[str] = frozenset("\n\r\\'\"abfnrtv01234567xNuU")
BYTES_ESCAPES: frozenset[str] = frozenset("\n\r\\'\"abfnrtv01234567x")


@dataclass(frozen=True, slots=True)
class Config:
    fixes: frozenset[str] = ALL_FIXES
    write: bool = True
    show_diff: bool = False
    backup: bool = False

    def enabled(self, name: str) -> bool:
        return name in self.fixes


class Edit(NamedTuple):
    start: int
    end: int
    replacement: bytes


@dataclass(slots=True)
class FixResult:
    text: str
    counts: Counter[str]
    warnings: list[str]


@dataclass(slots=True)
class FileReport:
    path: Path
    counts: Counter[str] = field(default_factory=Counter)
    changed: bool = False
    warnings: list[str] = field(default_factory=list)
    diff: str = ""
    error: str | None = None
    skipped: bool = False


class Positions:
    def __init__(self, text: str) -> None:
        self.lines = io.StringIO(text).readlines()
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line.encode("utf-8")))

    def from_tokenize(self, row: int, col: int) -> int:
        return self.starts[row - 1] + len(self.lines[row - 1][:col].encode("utf-8"))

    def from_ast(self, row: int, col: int) -> int:
        return self.starts[row - 1] + col


def splice(src: bytes, edits: Sequence[Edit]) -> bytes:
    parts: list[bytes] = []
    cursor = 0
    for edit in sorted(edits):
        if edit.start < cursor:
            msg = "overlapping edits"
            raise ValueError(msg)
        parts.append(src[cursor : edit.start])
        parts.append(edit.replacement)
        cursor = edit.end
    parts.append(src[cursor:])
    return b"".join(parts)


def iter_string_tokens(src: bytes, text: str, pos: Positions) -> Iterator[tuple[int, int, str, str]]:
    fmiddle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.STRING:
            kind = "string"
        elif fmiddle is not None and tok.type == fmiddle:
            kind = "fmiddle"
        else:
            continue
        start = pos.from_tokenize(*tok.start)
        end = pos.from_tokenize(*tok.end)
        yield start, end, src[start:end].decode("utf-8"), kind


def split_literal(tok: str) -> tuple[str, str, str] | None:
    match = LITERAL_RE.fullmatch(tok)
    return None if match is None else (match.group(1), match.group(2), match.group(3))


def parse(text: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(text)


def compile_check(text: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile(text, "<pyenhance>", "exec", dont_inherit=True)


def compile_warnings(text: str) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compile(text, "<pyenhance>", "exec", dont_inherit=True)
    return [str(w.message) for w in caught if issubclass(w.category, SyntaxWarning)]


class _NormalizeIs(ast.NodeTransformer):
    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        node.ops = [
            ast.Eq() if isinstance(op, ast.Is) else ast.NotEq() if isinstance(op, ast.IsNot) else op for op in node.ops
        ]
        return node


def fingerprint(text: str) -> str:
    return ast.dump(_NormalizeIs().visit(parse(text)))


def regex_constant_spans(tree: ast.AST, pos: Positions) -> set[tuple[int, int]]:
    re_modules: set[str] = {"re"}
    re_funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            re_modules.update(a.asname or a.name for a in node.names if a.name == "re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            re_funcs.update(a.asname or a.name for a in node.names if a.name in RE_FUNCS)
    candidates: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_re_call = (
                isinstance(func, ast.Attribute)
                and func.attr in RE_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id in re_modules
            ) or (isinstance(func, ast.Name) and func.id in re_funcs)
            if is_re_call:
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
                if arg is not None:
                    candidates.append(arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                isinstance(t, ast.Name) and PATTERN_NAME_RE.search(t.id) for t in targets
            ):
                candidates.append(node.value)
    spans: set[tuple[int, int]] = set()
    for arg in candidates:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (str, bytes)):
            spans.add((
                pos.from_ast(arg.lineno, arg.col_offset),
                pos.from_ast(arg.end_lineno, arg.end_col_offset),
            ))
    return spans


def to_raw_literal(tok: str) -> str | None:
    parts = split_literal(tok)
    if parts is None:
        return None
    prefix, quote, _ = parts
    lowered = prefix.lower()
    if set(lowered) - {"b", "u"}:
        return None
    is_bytes = "b" in lowered
    try:
        value = ast.literal_eval(tok)
    except (ValueError, SyntaxError):
        return None
    if is_bytes:
        if not value.isascii():
            return None
        body = value.decode("ascii")
    else:
        body = value
    if quote in body or any(ord(c) < 32 or ord(c) == 127 for c in body):
        return None
    candidate = ("rb" if is_bytes else "r") + quote + body + quote
    try:
        same = ast.literal_eval(candidate) == value
    except (ValueError, SyntaxError):
        return None
    return candidate if same else None


def double_invalid_escapes(body: str, valid: frozenset[str]) -> str:
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            if body[i + 1] in valid:
                out.append(body[i : i + 2])
                i += 2
            else:
                out.append("\\\\")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def string_edits(src: bytes, text: str, tree: ast.AST, pos: Positions, cfg: Config) -> tuple[list[Edit], Counter[str]]:
    regex_spans = regex_constant_spans(tree, pos) if cfg.enabled("regex") else set()
    edits: list[Edit] = []
    counts: Counter[str] = Counter()
    for start, end, source, kind in iter_string_tokens(src, text, pos):
        if kind == "string":
            parts = split_literal(source)
            if parts is None:
                continue
            prefix, quote, body = parts
            lowered = prefix.lower()
            if (start, end) in regex_spans and "\\" in body:
                raw = to_raw_literal(source)
                if raw is not None:
                    edits.append(Edit(start, end, raw.encode("utf-8")))
                    counts["regex"] += 1
                    continue
            if cfg.enabled("escape") and "r" not in lowered and "\\" in body:
                valid = BYTES_ESCAPES if "b" in lowered else STR_ESCAPES
                fixed = double_invalid_escapes(body, valid)
                if fixed != body:
                    edits.append(Edit(start, end, (prefix + quote + fixed + quote).encode("utf-8")))
                    counts["escape"] += 1
        elif cfg.enabled("escape") and "\\" in source:
            fixed = double_invalid_escapes(source, STR_ESCAPES)
            if fixed != source:
                edits.append(Edit(start, end, fixed.encode("utf-8")))
                counts["escape"] += 1
    return edits, counts


def fix_nonascii_bytes(text: str) -> tuple[str, int]:
    src = text.encode("utf-8")
    pos = Positions(text)
    edits: list[Edit] = []
    for start, end, source, kind in iter_string_tokens(src, text, pos):
        if kind != "string":
            continue
        parts = split_literal(source)
        if parts is None:
            continue
        prefix, quote, body = parts
        if "b" not in prefix.lower() or body.isascii():
            continue
        escaped = "".join(
            ch if ch.isascii() else "".join(f"\\x{byte:02x}" for byte in ch.encode("utf-8")) for ch in body
        )
        edits.append(Edit(start, end, (prefix + quote + escaped + quote).encode("utf-8")))
    return splice(src, edits).decode("utf-8"), len(edits)


def is_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) in (str, bytes, int, float, complex)


def is_edits(src: bytes, tree: ast.AST, pos: Positions) -> tuple[list[Edit], Counter[str]]:
    edits: list[Edit] = []
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Is, ast.IsNot)) and (is_literal(left) or is_literal(right)):
                seg_start = pos.from_ast(left.end_lineno, left.end_col_offset)
                seg_end = pos.from_ast(right.lineno, right.col_offset)
                match = IS_OP_RE.search(src[seg_start:seg_end])
                if match:
                    replacement = b"==" if isinstance(op, ast.Is) else b"!="
                    edits.append(Edit(seg_start + match.start(), seg_start + match.end(), replacement))
                    counts["is"] += 1
            left = right
    return edits, counts


def fix_source(original: str, cfg: Config) -> FixResult:
    counts: Counter[str] = Counter()
    text = original
    if cfg.enabled("bytes"):
        text, n = fix_nonascii_bytes(text)
        counts["bytes"] += n
    tree = parse(text)
    pos = Positions(text)
    src = text.encode("utf-8")
    edits: list[Edit] = []
    e, c = string_edits(src, text, tree, pos, cfg)
    edits += e
    counts.update(c)
    if cfg.enabled("is"):
        e, c = is_edits(src, tree, pos)
        edits += e
        counts.update(c)
    new_text = splice(src, edits).decode("utf-8")
    if new_text == original:
        return FixResult(original, Counter(), [])
    compile_check(new_text)
    if text == original and fingerprint(original) != fingerprint(new_text):
        msg = "AST changed beyond the intended fixes; refusing to write"
        raise ValueError(msg)
    clean = Counter({k: v for k, v in counts.items() if v})
    return FixResult(new_text, clean, compile_warnings(new_text))


def looks_like_python(text: str) -> bool:
    first = text.split("\n", 1)[0]
    if first.startswith("#!") and "python" in first:
        return True
    try:
        parse(text)
    except Exception:
        return False
    return True


def process_file(path: Path, cfg: Config) -> FileReport:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return FileReport(path, error=f"read failed: {exc}")
    try:
        encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
        original = raw.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError):
        return FileReport(path, skipped=True)
    if not path.suffix and not looks_like_python(original):
        return FileReport(path, skipped=True)
    try:
        result = fix_source(original, cfg)
    except Exception as exc:
        return FileReport(path, error=f"{type(exc).__name__}: {exc}")
    if result.text == original:
        return FileReport(path, warnings=result.warnings)
    report = FileReport(path, counts=result.counts, changed=True, warnings=result.warnings)
    if cfg.show_diff:
        report.diff = "".join(
            difflib.unified_diff(
                original.splitlines(True),
                result.text.splitlines(True),
                fromfile=str(path),
                tofile=f"{path} (fixed)",
            )
        )
    if cfg.write:
        try:
            atomic_write(path, result.text.encode(encoding), cfg.backup)
        except OSError as exc:
            return FileReport(path, error=f"write failed: {exc}")
    return report


def discover(inputs: Sequence[str], excludes: set[str]) -> list[Path]:
    roots = [Path(p) for p in inputs] if inputs else [Path.cwd()]
    skip = SKIP_DIRS | excludes
    seen: set[Path] = set()
    files: list[Path] = []

    def add(path: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            files.append(path)

    for root in roots:
        if root.is_file():
            add(root)
        elif root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if any(part in skip for part in path.relative_to(root).parts[:-1]):
                    continue
                if not path.suffix or path.suffix in PYTHON_SUFFIXES:
                    add(path)
        else:
            logger.error(f"Not found: {root}")
    return sorted(files)


def iter_results(files: list[Path], cfg: Config) -> Iterator[FileReport]:
    worker = partial(process_file, cfg=cfg)
    try:
        pool = mp.Pool(MAX_WORKERS)
    except OSError as exc:
        logger.warning(f"multiprocessing unavailable ({exc}); running sequentially")
        yield from map(worker, files)
        return
    with pool:
        yield from pool.imap_unordered(worker, files, chunksize=max(1, len(files) // (MAX_WORKERS * 4)))


def display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyenhance",
        description="Fix escaped regexes, invalid escape sequences, bytes literals and `is` literal comparisons.",
    )
    parser.add_argument("paths", nargs="*", help="files or directories (default: current directory, recursive)")
    parser.add_argument(
        "-s",
        "--select",
        nargs="+",
        choices=sorted(ALL_FIXES),
        metavar="FIX",
        help=f"fixes to apply (choices: {', '.join(sorted(ALL_FIXES))}; default: all)",
    )
    parser.add_argument("--no-is", action="store_true", help="do not rewrite `is` comparisons with literals")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument(
        "--check", action="store_true", help="report only; exit 1 if any file needs fixes (for CI / pre-commit)"
    )
    parser.add_argument("--diff", action="store_true", help="print a unified diff for each changed file")
    parser.add_argument("--backup", action="store_true", help="keep a .bak copy of each changed file")
    parser.add_argument(
        "-e", "--exclude", action="append", default=[], metavar="NAME", help="directory name to skip (repeatable)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level: <8}</level> | {message}")
    args = build_parser().parse_args(argv)
    fixes = frozenset(args.select) if args.select else ALL_FIXES
    if args.no_is:
        fixes -= {"is"}
    cfg = Config(
        fixes=fixes,
        write=not (args.dry_run or args.check),
        show_diff=args.diff,
        backup=args.backup,
    )
    files = discover(args.paths, set(args.exclude))
    if not files:
        logger.warning("No Python files found")
        return 0
    totals: Counter[str] = Counter()
    changed_files = 0
    errors = 0
    for report in iter_results(files, cfg):
        if report.skipped:
            continue
        name = display(report.path)
        if report.error:
            errors += 1
            logger.error(f"{name}: {report.error}")
            continue
        for note in report.warnings[:3]:
            logger.warning(f"{name}: remaining warning: {note}")
        if not report.changed:
            continue
        changed_files += 1
        totals.update(report.counts)
        summary = " ".join(f"{k}:{v}" for k, v in sorted(report.counts.items()))
        print(f"{name} {summary}", flush=True)
        if report.diff:
            print(report.diff, end="")
    mode = "" if cfg.write else " (dry run, nothing written)"
    print(f"{changed_files} file(s) changed, {sum(totals.values())} fix(es){mode}")
    for kind, count in sorted(totals.items()):
        print(f"  {kind}: {count}")
    if errors:
        return 2
    if args.check and changed_files:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
