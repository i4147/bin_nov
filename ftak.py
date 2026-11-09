#!/data/data/com.termux/files/usr/bin/env python
"""List functions and classes that contain nothing but a docstring.
Parses every ``.py`` file below the current directory and prints ``file:line: kind name`` for each definition whose body is a single string literal, i.e.
a placeholder that was never implemented.
Usage: ftak Examples: $ ftak Runs as a script (``python ftak.py``) in the directory it should act on; the module can also be imported."""

import ast
from pathlib import Path
from typing import Iterator


def walk_py_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if path.is_file():
            yield path


def is_docstring_only(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> bool:
    body = node.body
    if len(body) != 1:
        return False
    stmt = body[0]
    if not isinstance(stmt, ast.Expr):
        return False
    value = stmt.value
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def scan_file(path: Path) -> list[tuple[str, str, int]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    findings: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async function"
        elif isinstance(node, ast.FunctionDef):
            kind = "function"
        else:
            continue
        if is_docstring_only(node):
            findings.append((kind, node.name, node.lineno))
    return findings


def main() -> None:
    root = Path.cwd()
    for path in walk_py_files(root):
        findings = scan_file(path)
        if not findings:
            continue
        rel = path.relative_to(root)
        for kind, name, lineno in findings:
            print(f"{rel}:{lineno}: {kind} {name}")


if __name__ == "__main__":
    main()
