#!/data/data/com.termux/files/usr/bin/env python
"""Find and consolidate the duplicate functions, classes and constants of a tree.
Hashes the top-level definitions with ast, groups the identical ones and with ``-m`` writes them into ``dh.py``, removing the copies from the sources and adding the imports they need.
Usage: find_dup_func_class_const [-m] Options: -m, --move Consolidate duplicate code blocks into dh.py and update files with required imports.
Examples: $ find_dup_func_class_const -m Command-line script: run ``python find_dup_func_class_const.py`` or the ``find_dup_func_class_const`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
import concurrent.futures
import os
from pathlib import Path
import sys


def parse_file_definitions(path: Path) -> dict:
    definitions = {}
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node_key = ("function", node.name, ast.unparse(node))
            definitions[node_key] = {
                "name": node.name,
                "type": "function",
                "node": node,
            }
        elif isinstance(node, ast.ClassDef):
            node_key = ("class", node.name, ast.unparse(node))
            definitions[node_key] = {"name": node.name, "type": "class", "node": node}
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            node_key = ("constant", name, ast.unparse(node))
            definitions[node_key] = {"name": name, "type": "constant", "node": node}
    return {node_key: (str(path), data) for node_key, data in definitions.items()}


def modify_affected_file(path_str: str, obj_name: str, obj_type: str, raw_obj_code: str) -> str:
    path = Path(path_str)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    new_body = []
    removed = False
    for node in tree.body:
        if obj_type in ("function", "class") and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if node.name == obj_name and ast.unparse(node) == raw_obj_code:
                removed = True
                continue
        elif obj_type == "constant" and isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == obj_name and ast.unparse(node) == raw_obj_code:
                    removed = True
                    continue
        new_body.append(node)
    if not removed:
        return source
    import_node = ast.ImportFrom(module="dh", names=[ast.alias(name=obj_name, asname=None)], level=0)
    new_body.insert(0, import_node)
    tree.body = new_body
    modified_source = ast.unparse(tree)
    ast.parse(modified_source)
    return modified_source


def main():
    parser = argparse.ArgumentParser(
        description="Recursively find and consolidate duplicate functions, classes, and constants."
    )
    parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Consolidate duplicate code blocks into dh.py and update files with required imports.",
    )
    args = parser.parse_args()
    current_dir = Path()
    dh_path = current_dir / "dh.py"
    script_path = Path(__file__).resolve()
    py_files = [f for f in current_dir.rglob("*.py") if f.resolve() != script_path and f.resolve() != dh_path.resolve()]
    if not py_files:
        print("🔍 No Python files found to scan.")
        return
    print(f"🔍 Scanning {len(py_files)} files concurrently...")
    global_registry = defaultdict(list)
    cpu_cores = os.cpu_count() or 1
    with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_cores) as executor:
        futures = {executor.submit(parse_file_definitions, f): f for f in py_files}
        for future in concurrent.futures.as_completed(futures):
            file_defs = future.result()
            for node_key, (path_str, data) in file_defs.items():
                global_registry[node_key].append((path_str, data))
    duplicates = {k: v for k, v in global_registry.items() if len(v) > 1}
    if not duplicates:
        print("🎉 Success! No repeated functions, classes, or constants were detected.")
        return
    print(f"⚠️  Detected {len(duplicates)} repeated structural definitions:\n")
    dh_additions = []
    files_to_update = defaultdict(list)
    for (obj_type, obj_name, raw_code), occurrences in duplicates.items():
        files_listed = [occ[0] for occ in occurrences]
        print(f"[{obj_type.upper()}] '{obj_name}' is repeated in {len(files_listed)} files:")
        for f in files_listed:
            print(f"   -> {f}")
        print()
        if args.move:
            dh_additions.append(raw_code)
            for f_str, _ in occurrences:
                files_to_update[f_str].append((obj_name, obj_type, raw_code))
    if args.move:
        print("🛠️  Processing Consolidation (-m flag active)...")
        existing_dh_content = ""
        if dh_path.exists():
            existing_dh_content = dh_path.read_text(encoding="utf-8")
        new_dh_content = existing_dh_content + "\n\n" + "\n\n".join(dh_additions)
        try:
            ast.parse(new_dh_content)
            dh_path.write_text(new_dh_content, encoding="utf-8")
            print(f"✅ Extracted duplicate definitions safely written to: {dh_path}")
        except Exception as e:
            print(f"❌ Aborted: Merged definitions inside dh.py failed AST parsing logic: {e}")
            sys.exit(1)
        updated_count = 0
        for file_str, objects in files_to_update.items():
            try:
                current_path = Path(file_str)
                updated_source = current_path.read_text(encoding="utf-8")
                for obj_name, obj_type, raw_code in objects:
                    updated_source = modify_affected_file(file_str, obj_name, obj_type, raw_code)
                current_path.write_text(updated_source, encoding="utf-8")
                print(f"✅ In-place code updated & verified: {file_str}")
                updated_count += 1
            except Exception as e:
                print(f"❌ Failed to parse or modify file safely {file_str}: {e}. Skipping structural changes.")
        print(f"\n📊 Refactor complete. Adjusted and verified {updated_count} files.")


if __name__ == "__main__":
    raise SystemExit(main())
