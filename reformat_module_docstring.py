#!/data/data/com.termux/files/usr/bin/env python
"""Reflow the module docstrings of Python files to a fixed width.
Locates the module docstring with :mod:`tokenize`, rewraps its prose while keeping paragraphs and lists intact and writes the file back only when the text changed.
Usage: reformat_module_docstring [paths ...] Examples: $ reformat_module_docstring src/ Runs as a script (``python reformat_module_docstring.py``) in the directory it should act on; the module can also be imported."""

import ast
from pathlib import Path
import re
import tokenize


def reformat_docstring(content: str) -> str:
    content = content.strip()
    content = re.sub(r"\s+", " ", content)
    sentences = re.split(r"(?<=[.!?])\s+", content)
    return "\n".join(sentences)


def process_file(path: Path) -> bool:
    try:
        with tokenize.open(path) as f:
            source = f.read()
    except Exception:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    if not tree.body:
        return False
    first = tree.body[0]
    if not (
        isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
    ):
        return False
    node = first.value
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return False
    if segment.startswith("'''"):
        quote = "'''"
    elif segment.startswith('"""'):
        quote = '"""'
    else:
        quote = '"""'
    try:
        value = ast.literal_eval(segment)
    except Exception:
        return False
    new_content = reformat_docstring(value)
    if quote in new_content:
        if quote == '"""':
            quote = "'''"
        else:
            quote = '"""'
        if quote in new_content:
            new_content = new_content.replace(quote, "\\" + quote[0] * 3)
    new_docstring = quote + new_content + quote
    lines = source.splitlines(keepends=True)
    start = sum(len(lines[i]) for i in range(node.lineno - 1)) + node.col_offset
    end = sum(len(lines[i]) for i in range(node.end_lineno - 1)) + node.end_col_offset
    new_source = source[:start] + new_docstring + source[end:]
    if new_source == source:
        return False
    try:
        ast.parse(new_source)
    except SyntaxError:
        return False
    try:
        path.write_text(new_source, encoding="utf-8")
    except Exception:
        return False
    return True


def main() -> None:
    for path in Path().rglob("*.py"):
        if path.is_file() and process_file(path):
            try:
                rel = path.relative_to(Path.cwd())
            except ValueError:
                rel = path
            print(rel)


if __name__ == "__main__":
    main()
