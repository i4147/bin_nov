#!/data/data/com.termux/files/usr/bin/env python
"""
reindent.py — heuristic recovery of lost Python indentation.

Python is indentation-sensitive. When leading whitespace is stripped, exact
recovery is impossible in general (the same unindented text can correspond
to several valid programs). This tool applies the heuristics that match
typical real-world code, validates with ast.parse(), and if the guess fails,
iteratively repairs individual lines.

Usage:
    python reindent.py INPUT.py [-o OUTPUT.py] [--indent 4] [--stdout] [--check]

If -o is omitted, INPUT.py is overwritten.
"""

from __future__ import annotations
import argparse
import ast
import io
from pathlib import Path
import re
import sys
import tokenize
from typing import Any, Sequence


_CLOSING: set[str] = {"return", "raise", "break", "continue", "pass"}
_DEDENT_KW: set[str] = {"else", "elif", "except", "finally", "case"}
_BLOCK_KW: set[str] = {
    "if",
    "for",
    "while",
    "with",
    "try",
    "def",
    "class",
    "async",
    "match",
    "case",
}

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _first_word(line: str) -> str:
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", line)
    return m.group(1) if m else ""


def _line_opens_block(line: str) -> bool:
    """True iff the last significant token of the logical line is a colon."""
    s = line.rstrip()
    if not s.endswith(":"):
        return False
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(s + "\n").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return False
    sig = [
        t
        for t in toks
        if t.type
        not in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.ENDMARKER,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENCODING,
        )
    ]
    return bool(sig) and sig[-1].string == ":"


def _scan_brackets(line: str, depth: int, triple: str | None) -> tuple[int, str | None]:
    """Update bracket / triple-quote tracking by scanning one physical line."""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if triple:
            if line.startswith(triple, i):
                i += 3
                triple = None
            else:
                i += 1
            continue
        if ch == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            triple = line[i : i + 3]
            i += 3
            continue
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch in "\"'":
            q = ch
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == q:
                    break
                i += 1
            i += 1
            continue
        i += 1
    return depth, triple


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


# ---------------------------------------------------------------------------
# indentation strategies
# ---------------------------------------------------------------------------


def _reindent(src: str, unit: str, aggressive: bool) -> str:
    """Single pass over the file using the standard heuristics."""
    lines = src.split("\n")
    out: list[str] = []
    depth = 0
    bracket = 0
    triple: str | None = None
    prev_closed = False
    prev_augassign = False
    block_stack: list[tuple[str, int]] = []  # (kind, indent_level)

    for raw in lines:
        stripped = raw.rstrip()

        if not stripped:
            out.append("")
            continue

        # Continuation of a multi-line statement.
        if bracket > 0 or triple:
            out.append(unit * depth + stripped)
            bracket, triple = _scan_brackets(stripped, bracket, triple)
            continue

        first = _first_word(stripped)

        # 1. Explicit dedent keywords.
        if first in _DEDENT_KW:
            depth = max(0, depth - 1)
            if block_stack:
                block_stack.pop()

        # 2. Closing statements end the current block.
        elif prev_closed:
            depth = max(0, depth - 1)
            while block_stack and block_stack[-1][1] >= depth:
                block_stack.pop()

        # 3. Aggressive: extra dedents where the conservative pass over-indents.
        elif aggressive and block_stack:
            top_kind, top_indent = block_stack[-1]
            # New block-opener after an else/except/finally body → sibling.
            if top_kind in ("else", "except", "finally") and first in _BLOCK_KW:
                depth = top_indent
                block_stack.pop()
            # Statement after an augmented assignment (loop increment) → maybe exit.
            elif prev_augassign and first not in (_DEDENT_KW | _BLOCK_KW) and depth > 0:
                depth -= 1
                while block_stack and block_stack[-1][1] >= depth:
                    block_stack.pop()

        out.append(unit * depth + stripped)

        if _line_opens_block(stripped):
            kind = "if" if first == "elif" else first
            block_stack.append((kind, depth))
            depth += 1

        prev_closed = first in _CLOSING and bracket == 0
        prev_augassign = bool(re.search(r"\b\w+\s*(?:\+=|-=|\*=|/=|//=|%=|&=|\|=|\^=)", stripped))
        bracket, triple = _scan_brackets(stripped, 0, None)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# AST-guided repair (last resort)
# ---------------------------------------------------------------------------


def _repair_with_ast(text: str, unit: str) -> str:
    """Adjust individual line indents based on ast.parse() error messages."""
    lines = text.split("\n")
    indents = [len(line) - len(line.lstrip()) for line in lines]
    step = len(unit)

    for _ in range(500):
        rebuilt = "\n".join(
            ((" " * indents[i]) + lines[i].lstrip()) if lines[i].strip() else "" for i in range(len(lines))
        )
        try:
            ast.parse(rebuilt)
            return rebuilt
        except SyntaxError as exc:
            idx = (exc.lineno or 1) - 1
            msg = exc.msg or ""
            if not (0 <= idx < len(lines)) or not lines[idx].strip():
                return rebuilt

            if "unexpected indent" in msg or "unexpected unindent" in msg:
                if indents[idx] >= step:
                    indents[idx] -= step
                    continue
                return rebuilt

            if "expected an indented block" in msg:
                for j in range(idx - 1, -1, -1):
                    if lines[j].strip() and _line_opens_block(lines[j]):
                        indents[idx] = indents[j] + step
                        break
                else:
                    return rebuilt
                continue

            return rebuilt

    return rebuilt


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def reindent(src: str, unit: str = "    ") -> str:
    """Return a version of `src` with indentation heuristically restored."""
    # Try conservative first, then aggressive.
    for aggressive in (False, True):
        attempt = _reindent(src, unit, aggressive=aggressive)
        if _parses(attempt):
            return attempt

    # Fallback: repair the conservative pass with AST guidance.
    base = _reindent(src, unit, aggressive=False)
    return _repair_with_ast(base, unit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Heuristically restore lost indentation in Python.")
    ap.add_argument("input", type=Path, help="input .py file")
    ap.add_argument("-o", "--output", type=Path, default=None, help="output path (default: overwrite input)")
    ap.add_argument("--indent", type=int, default=4, help="spaces per indent level (default: 4)")
    ap.add_argument("--stdout", action="store_true", help="print the result to stdout instead of writing")
    ap.add_argument("--check", action="store_true", help="only report whether the result parses")
    args = ap.parse_args(argv)

    src = args.input.read_text(encoding="utf-8")
    result = reindent(src, unit=" " * max(1, args.indent))

    if args.check:
        try:
            ast.parse(result)
            print(f"{args.input}: OK", file=sys.stderr)
            return 0
        except SyntaxError as exc:
            print(f"{args.input}: still invalid: {exc}", file=sys.stderr)
            return 1

    if args.stdout:
        sys.stdout.write(result)
    else:
        dest = args.output or args.input
        dest.write_text(result, encoding="utf-8")
        print(f"Wrote {dest}", file=sys.stderr)

    try:
        ast.parse(result)
        return 0
    except SyntaxError as exc:
        print(f"WARNING: output still does not parse: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
