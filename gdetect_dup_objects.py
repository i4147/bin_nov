#!/data/data/com.termux/files/usr/bin/env python
"""Detect duplicate top-level objects across Python files, ignoring annotations and formatting.
Canonicalizes each function, class and constant (annotations stripped, docstrings and whitespace normalised), hashes it and prints the objects that appear in more than one file.
Works on the files below the current directory and uses a process pool for the parsing.
Usage: gdetect_dup_objects Examples: $ gdetect_dup_objects Runs as a script (``python gdetect_dup_objects.py``) in the directory it should act on; the module can also be imported."""

import ast
import json
import multiprocessing as mp
from pathlib import Path
from typing import Any


class TypeAnnotationChecker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.has_annotations = False

    def visit_FunctionDef(self, node: ast.AST) -> None:
        if node.returns:
            self.has_annotations = True
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.annotation:
                self.has_annotations = True
        if node.args.vararg and node.args.vararg.annotation:
            self.has_annotations = True
        if node.args.kwarg and node.args.kwarg.annotation:
            self.has_annotations = True
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AST) -> None:
        self.visit_FunctionDef(node)

    def visit_AnnAssign(self, node: ast.AST) -> None:
        self.has_annotations = True
        self.generic_visit(node)


class AnnotationStripper(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.AST):
        node.returns = None
        for arg in node.args.args + node.args.kwonlyargs:
            arg.annotation = None
        if node.args.vararg:
            node.args.vararg.annotation = None
        if node.args.kwarg:
            node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AST):
        return self.visit_FunctionDef(node)

    def visit_AnnAssign(self, node: ast.AST):
        if node.value is not None:
            return ast.Assign(targets=[node.target], value=node.value)
        return None


def get_canonical_structure(node: ast.AST) -> str:
    stripped_node = AnnotationStripper().visit(ast.parse(ast.unparse(node)))
    return ast.unparse(stripped_node)


def check_annotations(node: ast.AST) -> bool:
    checker = TypeAnnotationChecker()
    checker.visit(node)
    return checker.has_annotations


def extract_objects_from_file(file_path: Path) -> list[dict[str, Any]]:
    results = []
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, PermissionError):
        return results
    for node in ast.iter_child_nodes(tree):
        obj_type = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            obj_type = "function"
        elif isinstance(node, ast.ClassDef):
            obj_type = "class"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            is_constant = any(isinstance(t, ast.Name) and t.id.isupper() for t in targets)
            if is_constant:
                obj_type = "constant"
        if obj_type:
            canonical_code = get_canonical_structure(node)
            has_type_hints = check_annotations(node)
            results.append({
                "type": obj_type,
                "name": getattr(node, "name", ast.unparse(node)),
                "file": str(file_path),
                "line": node.lineno,
                "canonical_code": canonical_code,
                "has_annotations": has_type_hints,
            })
    return results


def main() -> None:
    current_dir = Path.cwd()
    output_dir = current_dir / "output"
    output_dir.mkdir(exist_ok=True)
    python_files = [p for p in current_dir.rglob("*.py") if output_dir not in p.parents]
    print(f"Scanning {len(python_files)} Python file(s) across CPU cores...")
    with mp.Pool(mp.cpu_count()) as pool:
        file_results = pool.map(extract_objects_from_file, python_files)
    all_objects = [obj for sublist in file_results for obj in sublist]
    grouped_objects: dict[str, list[dict[str, Any]]] = {}
    for obj in all_objects:
        key = (obj["type"], obj["canonical_code"])
        grouped_objects.setdefault(key, []).append(obj)
    categorized_duplicates: dict[str, list[dict[str, Any]]] = {
        "function": [],
        "class": [],
        "constant": [],
    }
    total_duplicates = 0
    for (obj_type, canonical_code), instances in grouped_objects.items():
        if len(instances) > 1:
            total_duplicates += 1
            has_ann_set = {inst["has_annotations"] for inst in instances}
            differ_in_annotations = len(has_ann_set) > 1
            duplicate_entry = {
                "canonical_code": canonical_code,
                "count": len(instances),
                "differ_in_type_annotations": differ_in_annotations,
                "instances": [
                    {
                        "file": inst["file"],
                        "line": inst["line"],
                        "has_annotations": inst["has_annotations"],
                    }
                    for inst in instances
                ],
            }
            categorized_duplicates[obj_type].append(duplicate_entry)
    for obj_type, duplicates in categorized_duplicates.items():
        duplicates.sort(key=lambda x: x["count"], reverse=True)
        output_file = output_dir / f"duplicate_{obj_type}s.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(duplicates, f, indent=2)
    print("\nScan completed!")
    print(f"- Total unique duplicated structures found: {total_duplicates}")
    for obj_type, items in categorized_duplicates.items():
        annotation_diff_count = sum(1 for item in items if item["differ_in_type_annotations"])
        print(
            f"- {obj_type.capitalize()}s: {len(items)} duplicates "
            f"({annotation_diff_count} differ in type annotations) -> saved to output/duplicate_{obj_type}s.json"
        )


if __name__ == "__main__":
    main()
