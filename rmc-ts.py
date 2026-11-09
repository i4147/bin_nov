#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python files in place using the tree-sitter parser."""

import argparse
import ast
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
import functools
import itertools
import keyword
import os
from pathlib import Path
import re
import textwrap
from typing import NamedTuple

from dh import MAX_WORKERS, SKIP_DIRS, atomic_write
from rich.console import Console
from rich.text import Text
from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspy


Edit = tuple[int, int, bytes]
DIRECTIVE = re.compile(
    r"(?:^|#)\s*(?:noqa\b|type\s*:|pylint:|pyright:|pyre-|pytype:|mypy:|ruff:|flake8:|"
    r"isort:|fmt:|yapf:|black:|autopep8:|pragma\b|nosec\b|nocover\b|skipcq|noinspection|"
    r"coding[:=]|-\*-|vim?:|ex:|region\b|endregion\b|cython:|lint:|ty:)",
    re.IGNORECASE,
)
PUNCT = re.compile(r"[=()\[\]{}:]")
WORD = re.compile(r"\w+")
HASH_PREFIX = re.compile(r"^[ \t]*#[ ]?")
COMMENT_LINE = re.compile(rb"^[ \t]*#")
SEMI = re.compile(rb"[ \t]*;[ \t]*")
NEWLINE = re.compile(rb"\r?\n?")
STRINGS = {"string", "template_string"}
STOP = STRINGS | {"comment"}
DEFS = {"function_definition", "class_definition"}


@functools.cache
def ts_parser() -> Parser:
    return Parser(Language(tspy.language()))


@dataclass(frozen=True)
class Options:
    full: bool = False
    docs: bool = False
    all: bool = False


class Result(NamedTuple):
    path: str
    delta: int
    error: str | None


def is_trivial_expr(expr: ast.expr) -> bool:
    match expr:
        case ast.Name() | ast.Constant() | ast.Attribute():
            return True
        case ast.Tuple(elts=elts) | ast.List(elts=elts):
            return all(is_trivial_expr(e) for e in elts)
        case ast.UnaryOp(operand=operand):
            return is_trivial_expr(operand)
        case ast.BoolOp(values=values):
            return all(is_trivial_expr(v) for v in values)
        case _:
            return False


def is_trivial(stmt: ast.stmt) -> bool:
    match stmt:
        case ast.Expr(value=value):
            return is_trivial_expr(value)
        case ast.AnnAssign(value=None):
            return True
        case _:
            return False


def candidates(body: str, first: str) -> list[str]:
    if body.startswith("@"):
        return [body, body + "\ndef _(): pass"]
    if not body.endswith(":"):
        return [body]
    pad = body + "\n    pass"
    wrap = {
        "else": "if 1:\n    pass\n" + pad,
        "elif": "if 1:\n    pass\n" + pad,
        "except": "try:\n    pass\n" + pad,
        "finally": "try:\n    pass\n" + pad,
        "try": pad + "\nfinally:\n    pass",
    }
    return [body, pad, wrap.get(first, body)]


@functools.lru_cache(maxsize=1024)
def is_code(body: str) -> bool:
    word = WORD.match(body)
    first = word.group() if word else ""
    if not (PUNCT.search(body) or body.startswith("@") or keyword.iskeyword(first)):
        return False
    for cand in candidates(body, first):
        try:
            tree = ast.parse(cand)
        except (SyntaxError, ValueError):
            continue
        return any(not is_trivial(s) for s in tree.body)
    return False


def iter_nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if node.type not in STOP:
            stack.extend(node.children)


def is_plain(src: bytes, pos: int) -> bool:
    prefix = re.split(rb"['\"]", src[pos : pos + 4].lower(), maxsplit=1)[0]
    return not set(prefix) & set(b"fbt")


def code_block_rows(lines: list[bytes]) -> set[int]:
    rows: set[int] = set()
    groups = itertools.groupby(range(len(lines)), key=lambda i: bool(COMMENT_LINE.match(lines[i])))
    for is_comment, group in groups:
        idx = list(group)
        if is_comment and len(idx) > 1:
            text = "\n".join(HASH_PREFIX.sub("", lines[i].decode("utf-8", "replace").rstrip("\r")) for i in idx)
            if is_code(textwrap.dedent(text).strip("\n")):
                rows.update(idx)
    return rows


def blank_edits(lines: list[bytes], protected: set[int]) -> Iterator[Edit]:
    pos = 0
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if i not in protected and not line.strip() and not (i == last and not line):
            yield pos, pos + len(line) + 1, b""
        pos += len(line) + 1


def apply_edits(src: bytes, edits: list[Edit]) -> bytes:
    out: list[bytes] = []
    pos = 0
    for start, end, text in sorted(edits):
        if start < pos:
            continue
        out.extend((src[pos:start], text))
        pos = end
    out.append(src[pos:])
    return b"".join(out)


def parses(src: bytes) -> bool:
    try:
        ast.parse(src)
    except Exception:
        return False
    return True


def is_valid(new: bytes, old: bytes) -> bool:
    if parses(new):
        return True
    return not parses(old) and not ts_parser().parse(new).root_node.has_error


class Stripper:
    def __init__(self, opts: Options) -> None:
        self.opts = opts

    def run(self, path: str) -> Result:
        try:
            real = Path(path).resolve()
            src = Path(real).read_bytes()
            new = self.strip(src)
            if new == src:
                return Result(path, 0, None)
            if not is_valid(new, src):
                return Result(path, 0, "result failed validation, file untouched")
            atomic_write(real, new)
            return Result(path, len(src) - len(new), None)
        except Exception as e:
            return Result(path, 0, f"{type(e).__name__}: {e}")

    def strip(self, src: bytes) -> bytes:
        root = ts_parser().parse(src).root_node
        if root.has_error:
            msg = "syntax error (tree-sitter)"
            raise ValueError(msg)
        lines = src.split(b"\n")
        blocks = code_block_rows(lines) if self.opts.full else set()
        edits: list[Edit] = []
        protected: set[int] = set()
        if self.opts.all:
            self.docstring_edit(root, src, edits, is_module=True)
        for node in iter_nodes(root):
            if node.type == "comment":
                self.comment_edit(src, node, blocks, edits)
            elif node.type in STRINGS:
                protected.update(range(node.start_point[0] + 1, node.end_point[0] + 1))
            elif self.opts.docs and node.type in DEFS:
                body = node.child_by_field_name("body")
                if body is not None:
                    self.docstring_edit(body, src, edits, is_module=False)
        edits.extend(blank_edits(lines, protected))
        return apply_edits(src, edits)

    def keep(self, text: str, row: int, blocks: set[int]) -> bool:
        if self.opts.all:
            return False
        if (row == 0 and text.startswith("#!")) or row in blocks:
            return True
        return bool(DIRECTIVE.search(text)) or is_code(text.lstrip("#").strip())

    def comment_edit(self, src: bytes, node: Node, blocks: set[int], edits: list[Edit]) -> None:
        start = node.start_byte
        line_start = start - node.start_point[1]
        prefix = src[line_start:start]
        inline = bool(prefix.strip())
        if not (inline or self.opts.full):
            return
        text = src[start : node.end_byte].rstrip(b"\r")
        if self.keep(text.decode("utf-8", "replace"), node.start_point[0], blocks):
            return
        end = start + len(text)
        if inline:
            edits.append((line_start + len(prefix.rstrip()), end, b""))
        else:
            newline = NEWLINE.match(src, end)
            edits.append((line_start, newline.end() if newline else end, b""))

    def docstring_edit(self, block: Node, src: bytes, edits: list[Edit], is_module: bool) -> None:
        stmts = [n for n in block.named_children if n.type != "comment"]
        if not stmts:
            return
        first = stmts[0]
        if first.type != "expression_statement" or first.named_child_count != 1:
            return
        literal = first.named_children[0]
        if literal.type not in ("string", "concatenated_string") or not is_plain(src, literal.start_byte):
            return
        start, end = first.start_byte, first.end_byte
        if len(stmts) == 1 and not is_module:
            edits.append((start, end, b"pass"))
            return
        line_start = start - first.start_point[1]
        line_end = src.find(b"\n", end)
        if line_end < 0:
            line_end = len(src)
        if not src[line_start:start].strip() and not src[end:line_end].strip():
            edits.append((line_start, line_end + 1, b""))
        else:
            semi = SEMI.match(src, end)
            edits.append((start, semi.end() if semi else end, b""))


class Walker:
    def __init__(self, roots: list[str], err: Console) -> None:
        self.roots = roots
        self.err = err
        self.seen: set[str] = set()

    def __iter__(self) -> Iterator[str]:
        for root in self.roots:
            if Path(root).is_dir():
                for folder, dirs, names in os.walk(root):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".dist-info"))
                    for name in sorted(names):
                        path = Path(folder).joinpath(name)
                        if name.endswith(".py") and not Path(path).is_symlink() and self.fresh(path):
                            yield path
            elif Path(root).is_file():
                if self.fresh(root):
                    yield root
            else:
                self.err.print(Text.assemble(("warning", "yellow"), f": {root}: not found"))

    def fresh(self, path: str) -> bool:
        real = Path(path).resolve()
        if real in self.seen:
            return False
        self.seen.add(real)
        return True


class App:
    def __init__(self, argv: list[str] | None = None) -> None:
        args = self.build_parser().parse_args(argv)
        self.paths: list[str] = args.paths
        self.workers: int = MAX_WORKERS
        self.stripper = Stripper(Options(full=args.c or args.a, docs=args.d or args.a, all=args.a))
        self.out = Console()
        self.err = Console(stderr=True)

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser(description="Strip comments/docstrings from Python files in place.")
        ap.add_argument("paths", nargs="*", default=[os.getcwd()], help="files or directories")
        ap.add_argument("-c", action="store_true", help="strip all comments, not just inline ones")
        ap.add_argument("-d", action="store_true", help="strip all docstrings (module docstring kept)")
        ap.add_argument(
            "-a",
            action="store_true",
            help="strip everything, even shebang/module docstring/directives/commented-out code",
        )
        return ap

    def results(self) -> Iterator[Result]:
        paths = Walker(self.paths, self.err)
        if self.workers > 1:
            yield from self.parallel(paths)
        else:
            for path in paths:
                yield self.stripper.run(path)

    def parallel(self, paths: Walker) -> Iterator[Result]:
        window = 4 * self.workers
        pool = ProcessPoolExecutor(max_workers=self.workers)
        pending: deque[Future[Result]] = deque()
        try:
            for path in paths:
                pending.append(pool.submit(self.stripper.run, path))
                if len(pending) >= window:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            pool.shutdown(cancel_futures=True)

    def run(self) -> int:
        count = changed = total = 0
        errors: list[Result] = []
        try:
            for count, res in enumerate(self.results(), 1):
                if res.error:
                    errors.append(res)
                    tail = Text("error", style="cyan")
                else:
                    total += res.delta
                    changed += res.delta > 0
                    tail = Text(f"{res.delta:,}", style="green" if res.delta else "dim")
                self.out.print(Text(f"[{count}]   {res.path.split('/')[-1]}   ") + tail)
        except KeyboardInterrupt:
            self.err.print(Text.assemble(("warning", "yellow"), ": interrupted by user, exiting gracefully"))
        if not count:
            self.err.print(Text.assemble(("warning", "yellow"), ": no files"))
            return 1
        self.out.print(f"\n{changed}/{count} files changed, [green]{total:,}[/] bytes reduced")
        for res in errors:
            self.err.print(Text.assemble(("error", "cyan"), f": {res.path}: {res.error}"))
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(App().run())
