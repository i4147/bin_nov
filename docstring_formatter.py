#!/data/data/com.termux/files/usr/bin/env python
"""Reflow the module docstring bodies of Python files to 80 columns.
Splits the docstring body into sentences and list items, re-wraps the prose to ``MAX_LINE`` and writes the file back only when the text actually changes.
String prefixes are preserved and only module docstrings are rewritten, not the function docstrings.
Usage: docstring_formatter [paths ...] Examples: $ docstring_formatter pkg/ Runs as a script (``python docstring_formatter.py``) in the directory it should act on; the module can also be imported.
"""

import ast
from pathlib import Path
import re
from typing import Any

from dh import SKIP_DIRS

_BEFORE_MARKER = re.compile(r"[ \t\r\n]+(?=(?:\d+[.)]|\(\d+\)|[-*\u2022]|\*\*[^*\n]+\*\*)\s)")
_COMMA_SPLIT = re.compile(r",[ \t]+")
_SEMICOLON_SPLIT = re.compile(r";[ \t]+")
MAX_LINE: int = 80
_STRING_HEAD = re.compile(r'^([rRbBuUfF]*)("""|\'\'\'|"|\')')


def reformat_docstring_body(text: str) -> str:
    if not text.strip():
        return text
    lead_match = re.match(r"^([ \t]*(?:\r?\n[ \t]*)*)", text)
    leading = lead_match.group(1) if lead_match else ""
    trail_match = re.search(r"([ \t]*(?:\r?\n[ \t]*)*)$", text)
    trailing = trail_match.group(1) if trail_match else ""
    indent = leading.rsplit("\n", 1)[-1] if "\n" in leading else ""
    core_end = len(text) - len(trailing) if trailing else len(text)
    core = text[len(leading) : core_end]
    core = _BEFORE_MARKER.sub("\n" + indent, core)
    core = _COMMA_SPLIT.sub(",\n" + indent, core)
    if MAX_LINE > 0:
        rebuilt = []
        for line in core.split("\n"):
            if len(line) > MAX_LINE and ";" in line:
                line = _SEMICOLON_SPLIT.sub(";\n" + indent, line)
            rebuilt.append(line)
        core = "\n".join(rebuilt)
    return leading + core + trailing


def _module_docstring_node(tree: ast.Module) -> ast.Constant | None:
    if not tree.body:
        return None
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first.value
    return None


def _offset(lines: list[str], lineno: int, col: int) -> int:
    return sum(len(line) for line in lines[: lineno - 1]) + col


def process_file(path: Path) -> bool:
    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    node = _module_docstring_node(tree)
    if node is None:
        return False
    lines = source.splitlines(keepends=True)
    start = _offset(lines, node.lineno, node.col_offset)
    end = _offset(lines, node.end_lineno, node.end_col_offset)
    raw = source[start:end]
    head = _STRING_HEAD.match(raw)
    if not head or not raw.endswith(head.group(2)):
        return False
    prefix, quote = head.group(1), head.group(2)
    inner = raw[len(prefix) + len(quote) : -len(quote)]
    new_inner = reformat_docstring_body(inner)
    if new_inner == inner:
        return False
    if len(quote) == 1:
        if '"""' in new_inner or new_inner.endswith('"'):
            return False
        new_raw = f'{prefix}"""{new_inner}"""'
    else:
        new_raw = prefix + quote + new_inner + quote
    path.write_bytes((source[:start] + new_raw + source[end:]).encode("utf-8"))
    return True


def iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file():
            yield path


def main(root: Path | None = None) -> int:
    root = (root or Path.cwd()).resolve()
    changed = 0
    for path in iter_python_files(root):
        if process_file(path):
            print(f"formatted: {path.relative_to(root)}")
            changed += 1
    print(f"{changed} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
