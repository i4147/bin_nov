#!/data/data/com.termux/files/usr/bin/env python
"""Format TOML files in place (taplo-style)."""

import argparse
from collections import Counter
from dataclasses import dataclass
import functools
import multiprocessing as mp
import os
from pathlib import Path
import re
import sys
from typing import Iterator, Sequence

from dh import SKIP_DIRS, WORKERS, atomic_write


try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
COLUMN_WIDTH = 80
INDENT = "  "
ALLOWED_BLANK_LINES = 2
CHUNK_MAX = 32
WS_RE = re.compile(r"[ \t]*")
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
BASIC_STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
LITERAL_STRING_RE = re.compile(r"'[^'\n]*'")
ML_BASIC_STOP_RE = re.compile(r'[\\"]')
ML_LITERAL_STOP_RE = re.compile(r"'")
DATETIME_SPACE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?")
ATOM_RE = re.compile(r"[^\s,\]\[{}#\"']+")


class ParseError(Exception):
    pass


class FormatError(Exception):
    pass


@dataclass(slots=True)
class Scalar:
    raw: str


@dataclass(slots=True)
class Elem:
    value: "Value"
    leading: list
    trailing: str | None = None


@dataclass(slots=True)
class Array:
    elems: list
    open_comment: str | None
    dangling: list


@dataclass(slots=True)
class Inline:
    entries: list


Value = Scalar | Array | Inline


@dataclass(slots=True)
class Comment:
    text: str


@dataclass(slots=True)
class Header:
    key: str
    double: bool
    comment: str | None


@dataclass(slots=True)
class Entry:
    key: str
    value: Value
    comment: str | None


class Parser:
    def __init__(self, source: str) -> None:
        self.s = source
        self.n = len(source)
        self.i = 0
        self.comments: list[str] = []

    def error(self, message: str):
        line = self.s.count("\n", 0, self.i) + 1
        msg = f"line {line}: {message}"
        raise ParseError(msg)

    def peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def skip_ws(self) -> None:
        self.i = WS_RE.match(self.s, self.i).end()

    def read_comment(self) -> str:
        end = self.s.find("\n", self.i)
        if end < 0:
            end = self.n
        text = self.s[self.i : end].rstrip(" \t")
        self.i = end
        self.comments.append(text)
        return text

    def finish_line(self) -> str | None:
        self.skip_ws()
        comment = None
        if self.peek() == "#":
            comment = self.read_comment()
        if self.i < self.n:
            if self.s[self.i] != "\n":
                self.error("unexpected characters after value")
            self.i += 1
        return comment

    def parse(self) -> list:
        items: list = []
        while True:
            self.skip_ws()
            if self.i >= self.n:
                return items
            c = self.s[self.i]
            if c == "\n":
                self.i += 1
                items.append(None)
            elif c == "#":
                items.append(Comment(self.read_comment()))
                self.finish_line()
            elif c == "[":
                items.append(self.parse_header())
            else:
                items.append(self.parse_entry())

    def parse_header(self) -> Header:
        double = self.s.startswith("[[", self.i)
        self.i += 2 if double else 1
        key = self.parse_key()
        closer = "]]" if double else "]"
        if not self.s.startswith(closer, self.i):
            self.error(f"expected {closer!r}")
        self.i += len(closer)
        return Header(key, double, self.finish_line())

    def parse_entry(self) -> Entry:
        key = self.parse_key()
        if self.peek() != "=":
            self.error("expected '='")
        self.i += 1
        self.skip_ws()
        value = self.parse_value()
        return Entry(key, value, self.finish_line())

    def parse_key(self) -> str:
        parts: list[str] = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c in {'"', "'"}:
                parts.append(self.scan_string())
            else:
                match = BARE_KEY_RE.match(self.s, self.i)
                if match is None:
                    self.error("expected a key")
                parts.append(match.group())
                self.i = match.end()
            self.skip_ws()
            if self.peek() == ".":
                self.i += 1
                continue
            return ".".join(parts)

    def scan_string(self) -> str:
        s, start = self.s, self.i
        quote = s[start]
        if s.startswith(quote * 3, start):
            end = self.scan_multiline(start, quote)
        else:
            match = (BASIC_STRING_RE if quote == '"' else LITERAL_STRING_RE).match(s, start)
            if match is None:
                self.error("unterminated string")
            end = match.end()
        self.i = end
        return s[start:end]

    def scan_multiline(self, start: int, quote: str) -> int:
        s, n = self.s, self.n
        stop = ML_BASIC_STOP_RE if quote == '"' else ML_LITERAL_STOP_RE
        j = start + 3
        while True:
            match = stop.search(s, j)
            if match is None:
                self.error("unterminated multi-line string")
            p = match.start()
            if s[p] == "\\":
                j = p + 2
                continue
            if s.startswith(quote * 3, p):
                end = p + 3
                extra = 0
                while extra < 2 and end < n and s[end] == quote:
                    end += 1
                    extra += 1
                return end
            j = p + 1

    def parse_value(self) -> Value:
        c = self.peek()
        if c == "":
            self.error("expected a value")
        if c in {'"', "'"}:
            return Scalar(self.scan_string())
        if c == "[":
            return self.parse_array()
        if c == "{":
            return self.parse_inline()
        match = DATETIME_SPACE_RE.match(self.s, self.i) or ATOM_RE.match(self.s, self.i)
        if match is None:
            self.error("invalid value")
        self.i = match.end()
        return Scalar(match.group())

    def parse_array(self) -> Array:
        s = self.s
        self.i += 1
        elems: list[Elem] = []
        open_comment: str | None = None
        trivia: list = []
        slot = "open"
        line_has_content = True
        while True:
            if self.i >= self.n:
                self.error("unterminated array")
            c = s[self.i]
            if c in {" ", "\t"}:
                self.i += 1
            elif c == "\n":
                self.i += 1
                if not line_has_content:
                    trivia.append(None)
                line_has_content = False
                slot = None
            elif c == "#":
                text = self.read_comment()
                if slot == "open":
                    open_comment = text
                elif slot == "elem":
                    elems[-1].trailing = text
                else:
                    trivia.append(text)
                line_has_content = True
                slot = None
            elif c == ",":
                self.i += 1
                line_has_content = True
            elif c == "]":
                self.i += 1
                return Array(elems, open_comment, trivia)
            else:
                value = self.parse_value()
                elems.append(Elem(value, trivia))
                trivia = []
                slot = "elem"
                line_has_content = True

    def parse_inline(self) -> Inline:
        self.i += 1
        entries: list = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c == "":
                self.error("unterminated inline table")
            if c == "}":
                self.i += 1
                return Inline(entries)
            key = self.parse_key()
            if self.peek() != "=":
                self.error("expected '=' in inline table")
            self.i += 1
            self.skip_ws()
            entries.append((key, self.parse_value()))
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "}":
                self.error("expected ',' or '}' in inline table")


@dataclass(slots=True)
class Line:
    text: str
    comment: str | None = None
    scope: int = 0


def flat_text(value: Value) -> str | None:
    if isinstance(value, Scalar):
        return None if "\n" in value.raw else value.raw
    if isinstance(value, Array):
        if value.open_comment is not None or any(t is not None for t in value.dangling):
            return None
        parts = []
        for elem in value.elems:
            if elem.trailing is not None or any(t is not None for t in elem.leading):
                return None
            text = flat_text(elem.value)
            if text is None:
                return None
            parts.append(text)
        return "[" + ", ".join(parts) + "]"
    parts = []
    for key, inner in value.entries:
        text = flat_text(inner)
        if text is None:
            return None
        parts.append(f"{key} = {text}")
    return "{ " + ", ".join(parts) + " }" if parts else "{}"


class Printer:
    def __init__(self) -> None:
        self.scopes = 0

    def new_scope(self) -> int:
        self.scopes += 1
        return self.scopes

    @staticmethod
    def trivia_lines(items: list, pad: str, keep_trailing_blank: bool) -> list[Line]:
        out: list[Line] = []
        blanks = 0
        for item in items:
            if item is None:
                blanks += 1
                continue
            out.extend(Line("") for _ in range(min(blanks, ALLOWED_BLANK_LINES)))
            blanks = 0
            out.append(Line(pad + item))
        if keep_trailing_blank:
            out.extend(Line("") for _ in range(min(blanks, ALLOWED_BLANK_LINES)))
        return out

    def value(self, value: Value, depth: int, col: int, tail: int, expand: bool) -> list[Line]:
        if isinstance(value, Scalar):
            return [Line(value.raw)]
        if isinstance(value, Array):
            return self.array(value, depth, col, tail, expand)
        return self.inline(value, depth)

    def array(self, arr: Array, depth: int, col: int, tail: int, expand: bool) -> list[Line]:
        flat = flat_text(arr)
        if flat is not None and (not expand or col + len(flat) + tail <= COLUMN_WIDTH):
            return [Line(flat)]
        scope = self.new_scope()
        pad = INDENT * (depth + 1)
        lines = [Line("[", arr.open_comment, scope)]
        for elem in arr.elems:
            lead = self.trivia_lines(elem.leading, pad, True)
            if len(lines) == 1:
                while lead and not lead[0].text:
                    lead.pop(0)
            lines.extend(lead)
            sub = self.value(elem.value, depth + 1, len(pad), 1, True)
            sub[0].text = pad + sub[0].text
            sub[-1].text += ","
            sub[-1].comment = elem.trailing
            sub[-1].scope = scope
            lines.extend(sub)
        lines.extend(self.trivia_lines(arr.dangling, pad, False))
        lines.append(Line(INDENT * depth + "]"))
        return lines

    def inline(self, table: Inline, depth: int) -> list[Line]:
        if not table.entries:
            return [Line("{}")]
        acc = [Line("{ ")]
        last = len(table.entries) - 1
        for index, (key, inner) in enumerate(table.entries):
            sub = self.value(inner, depth, 0, 0, False)
            acc[-1].text += f"{key} = " + sub[0].text
            if sub[0].comment is not None:
                acc[-1].comment = sub[0].comment
            acc.extend(sub[1:])
            if index != last:
                acc[-1].text += ", "
        acc[-1].text += " }"
        return acc

    def header(self, header: Header) -> Line:
        text = f"[[{header.key}]]" if header.double else f"[{header.key}]"
        return Line(text, header.comment, self.new_scope())

    def entry(self, entry: Entry) -> list[Line]:
        prefix = f"{entry.key} = "
        lines = self.value(entry.value, 0, len(prefix), 0, True)
        lines[0].text = prefix + lines[0].text
        if entry.comment is not None:
            lines[-1].comment = entry.comment
        return lines


def last_physical_width(text: str) -> int:
    return len(text.rsplit("\n", 1)[-1])


def align_comments(lines: list[Line]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].comment is None:
            out.append(lines[i].text)
            i += 1
            continue
        j = i
        while j < n and lines[j].comment is not None and lines[j].scope == lines[i].scope:
            j += 1
        width = max(last_physical_width(line.text) for line in lines[i:j])
        for line in lines[i:j]:
            padding = width - last_physical_width(line.text)
            out.append(f"{line.text}{' ' * padding} {line.comment}")
        i = j
    return out


def render(items: list) -> str:
    printer = Printer()
    lines: list[Line] = []
    blanks = 0
    for item in items:
        if item is None:
            blanks += 1
            continue
        if lines:
            lines.extend(Line("") for _ in range(min(blanks, ALLOWED_BLANK_LINES)))
        blanks = 0
        if isinstance(item, Comment):
            lines.append(Line(item.text))
        elif isinstance(item, Header):
            lines.append(printer.header(item))
        else:
            lines.extend(printer.entry(item))
    return "\n".join(align_comments(lines)) + "\n"


def same_data(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same_data(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same_data(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def format_text(text: str) -> str:
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"invalid TOML: {exc}"
        raise FormatError(msg) from None
    parser = Parser(text)
    try:
        items = parser.parse()
    except ParseError as exc:
        msg = f"unsupported syntax: {exc}"
        raise FormatError(msg) from None
    output = render(items)
    try:
        reparsed = tomllib.loads(output)
    except tomllib.TOMLDecodeError as exc:
        msg = f"formatter produced invalid TOML, file left untouched: {exc}"
        raise FormatError(msg) from None
    if not same_data(original, reparsed):
        msg = "formatter would change the data, file left untouched"
        raise FormatError(msg)
    checker = Parser(output)
    try:
        checker.parse()
    except ParseError as exc:
        msg = f"formatter output is not re-parseable: {exc}"
        raise FormatError(msg) from None
    if Counter(parser.comments) != Counter(checker.comments):
        msg = "formatter would lose or alter comments, file left untouched"
        raise FormatError(msg)
    return output


@dataclass(frozen=True, slots=True)
class Result:
    path: str
    status: str
    message: str = ""


def _process_file(path_str: str, check: bool) -> Result:
    path = Path(path_str)
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read()
    except OSError as exc:
        return Result(path_str, "error", f"read error: {exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Result(path_str, "error", f"not valid UTF-8: {exc}")
    has_bom = text.startswith("\ufeff")
    if has_bom:
        text = text[1:]
    crlf_count = text.count("\r\n")
    use_crlf = crlf_count > text.count("\n") - crlf_count
    text = text.replace("\r\n", "\n")
    if not text.strip():
        return Result(path_str, "unchanged")
    try:
        formatted = format_text(text)
    except FormatError as exc:
        return Result(path_str, "error", str(exc))
    if use_crlf:
        formatted = formatted.replace("\n", "\r\n")
    new_bytes = (("\ufeff" if has_bom else "") + formatted).encode("utf-8")
    if new_bytes == raw:
        return Result(path_str, "unchanged")
    if check:
        return Result(path_str, "changed")
    try:
        after = os.stat(path)
    except OSError as exc:
        return Result(path_str, "error", f"write error: {exc}")
    if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
        return Result(path_str, "error", "file changed while processing; left untouched")
    try:
        atomic_write(path, new_bytes, before.st_mode & 0o7777)
    except OSError as exc:
        return Result(path_str, "error", f"write error: {exc}")
    return Result(path_str, "changed")


def process_file(path_str: str, check: bool) -> Result:
    try:
        return _process_file(path_str, check)
    except Exception as exc:
        return Result(path_str, "error", f"unexpected error: {type(exc).__name__}: {exc}")


def walk_toml(root: str) -> Iterator[str]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            print(f"warning: cannot scan {directory}: {exc}", file=sys.stderr)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS:
                        stack.append(entry.path)
                elif entry.name.lower().endswith(".toml") and entry.is_file(follow_symlinks=False):
                    yield entry.path
            except OSError as exc:
                print(f"warning: cannot inspect {entry.path}: {exc}", file=sys.stderr)


def collect_files(paths: Sequence[Path]) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for given in paths:
        if Path(given).is_dir():
            candidates: Sequence[str] | Iterator[str] = walk_toml(Path(given).resolve())
        elif Path(given).is_file():
            candidates = (Path(given).resolve(),)
        else:
            print(f"warning: skipping non-existent path: {given}", file=sys.stderr)
            continue
        for candidate in candidates:
            key = str(Path(candidate)).lower() if os.name == "nt" else str(Path(candidate))
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return sorted(found)


def display_path(path: str) -> str:
    try:
        return Path(path).relative_to(Path())
    except ValueError:
        return path


def iter_results(files: list[str], check: bool) -> Iterator[Result]:
    worker = functools.partial(process_file, check=check)
    if len(files) == 1:
        yield worker(files[0])
        return
    chunk = max(1, min(CHUNK_MAX, len(files) // (WORKERS * 4)))
    with mp.Pool(processes=WORKERS) as pool:
        yield from pool.imap_unordered(worker, files, chunksize=chunk)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tomlfmt",
        description="Format TOML files in place (taplo-style). Default: current directory, recursively.",
    )
    parser.add_argument("paths", nargs="*", type=Path, metavar="PATH", help="files and/or directories")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 3 if any file would change",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="print only errors and the summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = args.paths or [Path.cwd()]
    files = collect_files(paths)
    if not files:
        print("No TOML files found.", file=sys.stderr)
        return 1
    verb = "would format" if args.check else "formatted"
    total = changed = errors = 0
    try:
        for result in iter_results(files, args.check):
            total += 1
            if result.status == "error":
                errors += 1
                print(f"{display_path(result.path)}: {result.message}", file=sys.stderr)
            elif result.status == "changed":
                changed += 1
                if not args.quiet:
                    print(f"{display_path(result.path)}  {verb}")
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    summary = f"\nProcessed {total} file(s): {verb} {changed}, {total - changed - errors} unchanged, {errors} error(s)."
    print(summary, file=sys.stderr if errors else sys.stdout)
    if errors:
        return 2
    return 3 if args.check and changed else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
