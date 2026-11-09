#!/data/data/com.termux/files/usr/bin/env python
"""Split Python sources into ``output/classes``, ``output/functions`` and ``output/const`` files.
Parses each module with :mod:`ast`, writes every top-level class, function and constant into its own file under ``output/`` and skips ``test``/``examples`` directories plus the output tree itself.
The files are processed in a worker pool.
Usage: gext [paths ...] Examples: $ gext project/ Runs as a script (``python gext.py``) in the directory it should act on; the module can also be imported."""

import ast
import multiprocessing as mp
import os
from pathlib import Path
import sys
from typing import Any, Iterable

from dh import MAX_WORKERS


OUTPUT_DIR: str = "output"
CLASSES_DIR = Path(OUTPUT_DIR).joinpath("classes")
FUNCTIONS_DIR = Path(OUTPUT_DIR).joinpath("functions")
CONST_DIR = Path(OUTPUT_DIR).joinpath("const")
EXCLUDE_DIRS: set[str] = {"test", "tests", "examples", "output"}


def is_python_script(path: str) -> bool:
    if path.endswith(".py"):
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline()
        return line.startswith("#!") and "python" in line.lower()
    except Exception:
        return False


def collect_files(inputs: list[str]) -> list[str]:
    files: list[str] = []
    for inp in inputs:
        if Path(inp).is_file():
            if is_python_script(inp):
                files.append(inp)
        elif Path(inp).is_dir():
            for root, dirs, fnames in os.walk(inp):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for fname in fnames:
                    p = Path(root).joinpath(fname)
                    if is_python_script(p):
                        files.append(p)
    return files


def mark_parents(node: ast.AST, parent=None) -> None:
    for child in ast.iter_child_nodes(node):
        child._parent = node
        mark_parents(child, node)


def is_constant_name(name: str) -> bool:
    return name.isupper()


def extract_from_file(path: str):
    classes: dict[str, str] = {}
    funcs: dict[str, str] = {}
    consts: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return path, classes, funcs, consts
    mark_parents(tree)
    for node in ast.walk(tree):
        parent = getattr(node, "_parent", None)
        if isinstance(node, ast.ClassDef):
            src = ast.get_source_segment(source, node)
            if src:
                classes[node.name] = src
        elif isinstance(node, ast.FunctionDef):
            if isinstance(parent, ast.ClassDef):
                continue
            src = ast.get_source_segment(source, node)
            if src:
                funcs[node.name] = src
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if not isinstance(parent, ast.Module):
                continue
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
            else:
                continue
            if not is_constant_name(name):
                continue
            src = ast.get_source_segment(source, node)
            if src:
                consts[name] = src
    return path, classes, funcs, consts


def write_entity(directory: str, name: str, src: str) -> None:
    safe = name.replace(os.sep, "_").replace("/", "_")
    fpath = Path(directory).joinpath(safe + ".py")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(src.rstrip() + "\n")


def main() -> None:
    inputs = sys.argv[1:]
    if not inputs:
        inputs = ["."]
    files = collect_files(inputs)
    if not files:
        print("No Python files found.")
        return
    os.makedirs(CLASSES_DIR, exist_ok=True)
    os.makedirs(FUNCTIONS_DIR, exist_ok=True)
    os.makedirs(CONST_DIR, exist_ok=True)
    all_classes: dict[str, str] = {}
    all_funcs: dict[str, str] = {}
    all_consts: dict[str, str] = {}
    with mp.Pool(MAX_WORKERS) as pool:
        for _path, classes, funcs, consts in pool.imap_unordered(extract_from_file, files):
            all_classes.update(classes)
            all_funcs.update(funcs)
            all_consts.update(consts)
    for name, src in all_classes.items():
        write_entity(CLASSES_DIR, name, src)
    for name, src in all_funcs.items():
        write_entity(FUNCTIONS_DIR, name, src)
    for name, src in all_consts.items():
        write_entity(CONST_DIR, name, src)
    print(f"Scanned files : {len(files)}")
    print(f"Classes       : {len(all_classes)}")
    print(f"Functions     : {len(all_funcs)}")
    print(f"Constants     : {len(all_consts)}")
    print("Outputs saved to:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
