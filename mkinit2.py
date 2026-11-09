#!/data/data/com.termux/files/usr/bin/env python
"""Rebuild the ``__init__.py`` of a package from its modules' public names.
Collects the public definitions per module in a process pool, merges them with the declarations already present in ``__init__.py`` and writes a new file that imports everything (backing up the previous version).
Usage: mkinit2 [package_dir] Examples: $ mkinit2 mypkg/ Runs as a script (``python mkinit2.py``) in the directory it should act on; the module can also be imported."""

import ast
import multiprocessing as mp
from pathlib import Path
import shutil
import sys


def get_public_names(path: Path) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(path))
    except SyntaxError as e:
        print(f"Warning: Could not parse {path}: {e}", file=sys.stderr)
        return []
    public_names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public_names.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            public_names.append(node.name)
    return sorted(set(public_names))


def get_python_modules(directory: Path) -> list[Path]:
    modules = []
    for path in directory.glob("*.py"):
        if path.name != "__init__.py" and (not path.name.startswith("_")):
            modules.append(path)
    return sorted(modules)


def process_module(module_path: Path) -> tuple[str, list[str]]:
    return module_path.stem, get_public_names(module_path)


def parse_existing_init(init_file: Path) -> tuple[set[str], set[str], list[str]]:
    existing_imports: set[str] = set()
    existing_all: set[str] = set()
    try:
        with open(init_file, "r", encoding="utf-8") as f:
            original_lines = f.readlines()
        tree = ast.parse("".join(original_lines), filename=str(init_file))
    except SyntaxError:
        return existing_imports, existing_all, original_lines
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is None and node.level == 1:
            for alias in node.names:
                existing_imports.add(alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                existing_all.add(elt.value)
    return existing_imports, existing_all, original_lines


def main():
    cwd = Path.cwd()
    print(f"Scanning directory: {cwd}")
    modules = get_python_modules(cwd)
    if not modules:
        print("No Python modules found in the current directory.")
        return
    print(f"Found {len(modules)} Python module(s). Parsing in parallel...")
    with mp.Pool(processes=8) as pool:
        results = pool.map(process_module, modules)
    module_public: dict[str, list[str]] = {}
    for module_name, public_names in results:
        module_public[module_name] = public_names
        print(f"  - {module_name}.py: {len(public_names)} public name(s)")
    modules_with_public = [(name, names) for name, names in module_public.items() if names]
    init_file = cwd / "__init__.py"
    if init_file.exists():
        backup_file = init_file.with_suffix(".py.bak")
        shutil.copy2(init_file, backup_file)
        print(f"\nBackup created: {backup_file}")
        existing_imports, existing_all, original_lines = parse_existing_init(init_file)
        while original_lines and original_lines[-1].strip() == "":
            original_lines.pop()
        new_lines = list(original_lines)
        if new_lines:
            new_lines.append("")
        appended_any = False
        for module_name, public_names in modules_with_public:
            missing = [n for n in public_names if n not in existing_imports]
            if missing:
                names_str = ", ".join(missing)
                new_lines.append(f"from .{module_name} import {names_str}")
                existing_imports.update(missing)
                appended_any = True
        merged_all = sorted(existing_all | existing_imports)
        cleaned_lines = []
        skip = False
        for line in new_lines:
            if skip:
                if "]" in line:
                    skip = False
                continue
            if line.strip().startswith("__all__"):
                if "[" in line and "]" in line:
                    continue
                skip = True
                continue
            cleaned_lines.append(line)
        while cleaned_lines and cleaned_lines[-1].strip() == "":
            cleaned_lines.pop()
        if merged_all:
            cleaned_lines.append("")
            all_str = ", ".join(repr(name) for name in merged_all)
            cleaned_lines.append(f"__all__ = [{all_str}]")
        cleaned_lines.append("")
        new_content = "\n".join(cleaned_lines)
        with open(init_file, "w", encoding="utf-8") as f:
            f.write(new_content)
        if appended_any:
            print(f"\nUpdated {init_file} (appended missing imports).")
        else:
            print(f"\nUpdated {init_file} (no new imports, __all__ refreshed).")
        print("\nGenerated content:")
        print("-" * 40)
        print(new_content)
        print("-" * 40)
    else:
        lines = []
        all_exports = []
        for module_name, public_names in modules_with_public:
            names_str = ", ".join(public_names)
            lines.append(f"from .{module_name} import {names_str}")
            all_exports.extend(public_names)
        if all_exports:
            lines.append("")
            all_str = ", ".join(repr(name) for name in all_exports)
            lines.append(f"__all__ = [{all_str}]")
        if lines:
            lines.append("")
        init_content = "\n".join(lines)
        with open(init_file, "w", encoding="utf-8") as f:
            f.write(init_content)
        print(f"\nCreated {init_file}")
        print("\nGenerated content:")
        print("-" * 40)
        print(init_content)
        print("-" * 40)


if __name__ == "__main__":
    raise SystemExit(main())
