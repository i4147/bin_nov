#!/data/data/com.termux/files/usr/bin/env python
"""Strip the docstrings from Python files while keeping the module docstrings.
Removes the class and function docstrings with ast-driven source rewriting, validates that the result still parses and processes the tree in a pool.
Usage: brmc [paths ...] Positional arguments: targets ...
Files or directories to process (default: current directory, recursive).
Examples: $ brmc src/ Command-line script: run ``python brmc.py`` or the ``brmc`` command.
The module is importable as well."""

import argparse
import ast
import multiprocessing as mp
from pathlib import Path

from dh import get_pyfiles


def _first_statement_is_docstring(tree: ast.Module) -> bool:
    if not tree.body:
        return False
    node = tree.body[0]
    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


def _remove_docstrings_from_source(source: str) -> tuple[str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    to_remove: list[tuple[int, int, int, int]] = []
    preserve_module = _first_statement_is_docstring(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        val = getattr(node, "value", None)
        if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
            continue
        if preserve_module and tree.body and node is tree.body[0]:
            continue
        if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
            to_remove.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    if not to_remove:
        return source, 0
    lines = source.splitlines(keepends=True)
    to_remove.sort(key=lambda r: (r[0], r[1]), reverse=True)
    for sline, scol, eline, ecol in to_remove:
        sidx = sline - 1
        eidx = eline - 1
        if sidx < 0 or eidx >= len(lines):
            continue
        if sidx == eidx:
            line = lines[sidx]
            lines[sidx] = line[:scol] + "" + line[ecol:]
        else:
            first = lines[sidx]
            last = lines[eidx]
            lines[sidx] = first[:scol]
            lines[eidx] = last[ecol:]
            for mid in range(sidx + 1, eidx):
                lines[mid] = ""
    new_source = "".join(lines)
    return new_source, len(to_remove)


def _validate_syntax(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False


def _collect_py_files(paths: list[Path], *, recursive: bool = True) -> list[Path]:
    py_files: list[Path] = []
    for target in paths:
        if target.is_file():
            if target.suffix == ".py":
                py_files.append(target)
        elif target.is_dir():
            pattern = "**/*.py" if recursive else "*.py"
            py_files.extend(target.glob(pattern))
    return sorted({p.resolve() for p in py_files})


def process_file(path: Path, cwd: Path) -> tuple[str, int] | None:
    rel = str(path.relative_to(cwd))
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text(encoding="latin-1")
    except Exception:
        return None
    new_source, doc_count = _remove_docstrings_from_source(source)
    if new_source == source:
        return None
    if not _validate_syntax(new_source):
        return None
    try:
        path.write_text(new_source, encoding="utf-8")
    except Exception:
        return None
    return rel, doc_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Strip docstrings from Python files (preserves module docstrings).")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Files or directories to process (default: current directory, recursive).",
    )
    args = parser.parse_args()
    cwd = Path.cwd()
    if args.targets:
        targets = [Path(t).resolve() for t in args.targets]
        py_files = _collect_py_files(targets, recursive=True)
    else:
        py_files = get_pyfiles(cwd)
        py_files = sorted(set(py_files))
    if not py_files:
        return
    changed: list[tuple[str, int]] = []
    with mp.Pool(processes=8) as pool:
        async_results = [pool.apply_async(process_file, (p, cwd)) for p in py_files]
        for async_res in async_results:
            res = async_res.get()
            if res is not None:
                changed.append(res)
    for rel, doc_count in sorted(changed, key=lambda x: x[0]):
        print(rel)
        if doc_count > 0:
            print(f"  docstrings removed: {doc_count}")


if __name__ == "__main__":
    raise SystemExit(main())
