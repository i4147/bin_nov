#!/data/data/com.termux/files/usr/bin/env python
"""Split Python sources into per-object class, function and constant files.

Parses each input module with libcst and writes one file per top-level class, function or constant under output/classes, output/functions and output/const.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import libcst as cst
import libcst.matchers as m
from dh import MAX_WORKERS

OUTPUT_DIR: str = "output"
CLASSES_DIR: Path = Path(OUTPUT_DIR).joinpath("classes")
FUNCTIONS_DIR: Path = Path(OUTPUT_DIR).joinpath("functions")
CONST_DIR: Path = Path(OUTPUT_DIR).joinpath("const")
EXCLUDE_DIRS: set[str] = {"test", "tests", "examples", "output"}


def is_python_script(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline()
        return line.startswith("#!") and "python" in line.lower()
    except Exception:
        return False


def collect_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_file():
            if is_python_script(p):
                files.append(p)
        elif p.is_dir():
            for root, dirs, fnames in os.walk(p):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for fname in fnames:
                    candidate = Path(root).joinpath(fname)
                    if is_python_script(candidate):
                        files.append(candidate)
    return files


def is_constant_name(name: str) -> bool:
    return name.isupper()


class EntityExtractor(cst.CSTVisitor):
    """Extracts top-level classes, functions, and uppercase module constants."""

    def __init__(self, module: cst.Module) -> None:
        self.module = module
        self.classes: dict[str, str] = {}
        self.funcs: dict[str, str] = {}
        self.consts: dict[str, str] = {}
        self._class_depth: int = 0
        self._func_depth: int = 0

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self._class_depth == 0 and self._func_depth == 0:
            self.classes[node.name.value] = self.module.code_for_node(node)
        self._class_depth += 1
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._class_depth -= 1

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        # Match both `def` and `async def` (both are cst.FunctionDef in LibCST)
        if self._class_depth == 0 and self._func_depth == 0:
            self.funcs[node.name.value] = self.module.code_for_node(node)
        self._func_depth += 1
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._func_depth -= 1

    def visit_Assign(self, node: cst.Assign) -> bool:
        if self._class_depth == 0 and self._func_depth == 0:
            if len(node.targets) == 1 and m.matches(node.targets[0].target, m.Name()):
                assert isinstance(node.targets[0].target, cst.Name)
                name = node.targets[0].target.value
                if is_constant_name(name):
                    self.consts[name] = self.module.code_for_node(node)
        return False

    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
        if self._class_depth == 0 and self._func_depth == 0:
            if m.matches(node.target, m.Name()):
                assert isinstance(node.target, cst.Name)
                name = node.target.value
                if is_constant_name(name):
                    self.consts[name] = self.module.code_for_node(node)
        return False


def extract_from_file(
    path: Path,
) -> tuple[Path, dict[str, str], dict[str, str], dict[str, str]]:
    classes: dict[str, str] = {}
    funcs: dict[str, str] = {}
    consts: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = cst.parse_module(source)
    except Exception:
        return path, classes, funcs, consts

    extractor = EntityExtractor(tree)
    tree.visit(extractor)
    return path, extractor.classes, extractor.funcs, extractor.consts


def write_entity(directory: Path, name: str, src: str) -> None:
    safe = name.replace(os.sep, "_").replace("/", "_")
    fpath = directory.joinpath(f"{safe}.py")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(src.rstrip() + "\n")


def main() -> None:
    inputs = sys.argv[1:] or ["."]
    files = collect_files(inputs)
    if not files:
        print("No Python files found.")
        return

    CLASSES_DIR.mkdir(parents=True, exist_ok=True)
    FUNCTIONS_DIR.mkdir(parents=True, exist_ok=True)
    CONST_DIR.mkdir(parents=True, exist_ok=True)

    all_classes: dict[str, str] = {}
    all_funcs: dict[str, str] = {}
    all_consts: dict[str, str] = {}

    with mp.Pool(MAX_WORKERS) as pool:
        for _path, classes, funcs, consts in pool.imap_unordered(
            extract_from_file, files
        ):
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
    raise SystemExit(main())
