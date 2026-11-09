#!/data/data/com.termux/files/usr/bin/env python
"""Compare the public definitions of a package with its ``__init__.py`` exports.
Scans the Python files of the given paths for the top-level functions, classes and constants, reads the ``__all__`` of the package initialisers, reports the definitions that are not exported and can add them with ``-a`` (``--dry-run`` previews).
Usage: check_exports [-a] [--dry-run] [paths ...] Positional arguments: paths ...
Files or directories to scan (default: '.' — recursive) Options: -a, --autofix Automatically add missing definitions to __init__.py --dry-run With -a, show what would change without writing -v, --verbose Enable debug logging Examples: $ check_exports -a pkg/ Command-line script: run ``python check_exports.py`` or the ``check_exports`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Iterator
from pathlib import Path

from dh import SKIP_DIRS
from loguru import logger


ICONS = {"functions": "🔧", "classes": "🏛️ ", "constants": "📌"}


def extract_definitions(path: Path) -> dict[str, list[str]]:
    defs: dict[str, list[str]] = {"functions": [], "classes": [], "constants": []}
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse {path}: {e}")
        return defs
    except OSError as e:
        logger.error(f"Failed to read {path}: {e}")
        return defs
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                defs["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                defs["classes"].append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and not t.id.startswith("_"):
                    defs["constants"].append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            n = node.target.id
            if n.isupper() and not n.startswith("_"):
                defs["constants"].append(n)
    return defs


def extract_exports(init_path: Path) -> set[str]:
    exported: set[str] = set()
    try:
        source = init_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(init_path))
    except (SyntaxError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse {init_path}: {e}")
        return exported
    except OSError as e:
        logger.error(f"Failed to read {init_path}: {e}")
        return exported
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exported.add(elt.value)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    logger.warning(f"Wildcard import in {init_path}")
                    continue
                exported.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                exported.add(a.asname or a.name.split(".")[0])
    return exported


def iter_py_files(target: Path) -> Iterator[Path]:
    if target.is_file():
        if target.suffix == ".py":
            yield target
        else:
            logger.warning(f"Skipping non-Python file: {target}")
        return
    if not target.is_dir():
        logger.warning(f"Skipping: {target}")
        return
    try:
        entries = sorted(target.iterdir())
    except OSError as e:
        logger.warning(f"Cannot read {target}: {e}")
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file():
            if entry.suffix == ".py" and entry.name != "__init__.py":
                yield entry
        elif entry.is_dir():
            if entry.name in SKIP_DIRS or entry.name.endswith(".dist-info"):
                continue
            yield from iter_py_files(entry)


def collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for t in paths:
        if not t.exists():
            logger.error(f"Path does not exist: {t}")
            continue
        for f in iter_py_files(t):
            rp = f.resolve()
            if rp not in seen:
                seen.add(rp)
                files.append(f)
    return sorted(files)


def autofix_init(
    init_path: Path,
    missing: dict[str, list[str]],
    dry_run: bool,
) -> bool:
    if not missing:
        return True
    try:
        original = init_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Failed to read {init_path}: {e}")
        return False
    try:
        tree = ast.parse(original, filename=str(init_path))
    except SyntaxError as e:
        logger.error(f"Cannot autofix, {init_path} has a syntax error: {e}")
        return False
    all_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    all_node = node
                    break
        if all_node:
            break
    by_module: dict[str, list[str]] = {}
    for cat in ("functions", "classes", "constants"):
        for name in missing.get(cat, []):
            pass
    module_names: dict[str, list[str]] = {}
    for py in sorted(init_path.parent.glob("*.py")):
        if py.name == "__init__.py" or py.name.startswith("_"):
            continue
        mod = py.stem
        mod_defs = extract_definitions(py)
        hits = [
            n
            for cat in ("functions", "classes", "constants")
            for n in mod_defs[cat]
            if n in {x for cat2 in missing.values() for x in cat2}
        ]
        if hits:
            module_names[mod] = sorted(set(hits))
    import_lines: list[str] = []
    for mod, names in module_names.items():
        if len(names) == 1:
            import_lines.append(f"from .{mod} import {names[0]}")
        else:
            import_lines.append(f"from .{mod} import ({', '.join(names)})")
    new_names = sorted({n for names in missing.values() for n in names})
    if all_node is not None:
        existing: set[str] = set()
        if isinstance(all_node.value, (ast.List, ast.Tuple)):
            for elt in all_node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    existing.add(elt.value)
        combined = sorted(existing | set(new_names))
        new_block = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in combined) + "]"
        lines = original.splitlines(keepends=True)
        start, end = all_node.lineno - 1, all_node.end_lineno
        updated = "".join(lines[:start] + [new_block + "\n"] + lines[end:])
        if import_lines:
            if not updated.endswith("\n"):
                updated += "\n"
            updated += "\n" + "\n".join(import_lines) + "\n"
    else:
        updated = original
        if not updated.endswith("\n"):
            updated += "\n"
        if import_lines:
            updated += "\n" + "\n".join(import_lines) + "\n"
        updated += "\n__all__ = [\n" + "".join(f'    "{n}",\n' for n in new_names) + "]\n"
    if dry_run:
        print("\n" + "=" * 70)
        print(f"DRY RUN — would write to {init_path}:")
        print("=" * 70)
        if import_lines:
            print("\n".join(import_lines))
            print()
        print("__all__ entries added:", new_names)
        return True
    try:
        init_path.write_text(updated, encoding="utf-8")
        logger.success(f"Updated {init_path} with {len(new_names)} new export(s)")
        return True
    except OSError as e:
        logger.error(f"Failed to write {init_path}: {e}")
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Scan Python files for top-level public definitions. "
            "Accepts files and/or directories; with no arguments, processes '.' "
            "recursively. Skips symlinks and common non-source directories."
        )
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to scan (default: '.' — recursive)",
    )
    p.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically add missing definitions to __init__.py",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="With -a, show what would change without writing",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        format="<level>{level: <8}</level> | <level>{message}</level>",
        level="DEBUG" if args.verbose else "INFO",
        colorize=True,
    )
    paths = args.paths or [Path()]
    files = collect_files(paths)
    if not files:
        logger.error("No Python files found")
        return 1
    print(f"Scanning {len(files)} file(s):\n")
    missing_by_init: dict[Path, dict[str, list[str]]] = {}
    total_missing = 0
    for f in files:
        defs = extract_definitions(f)
        print(f"📄 {f}")
        any_def = False
        for cat in ("functions", "classes", "constants"):
            if defs[cat]:
                any_def = True
                print(f"  {ICONS[cat]} {cat.capitalize()}:")
                for n in sorted(defs[cat]):
                    print(f"      - {n}")
        if not any_def:
            print("  (no public top-level definitions)")
        if f.name != "__init__.py":
            init_path = f.parent / "__init__.py"
            if init_path.is_file():
                exported = extract_exports(init_path)
                missing = {"functions": [], "classes": [], "constants": []}
                for cat, names in defs.items():
                    for n in names:
                        if n not in exported and f.stem not in exported:
                            missing[cat].append(n)
                if any(missing.values()):
                    print(f"  ⚠️  Not exported from {init_path.name}:")
                    for cat, names in missing.items():
                        if names:
                            print(f"      {ICONS[cat]} {cat.capitalize()}: {', '.join(sorted(names))}")
                            total_missing += len(names)
                    bucket = missing_by_init.setdefault(init_path, {"functions": [], "classes": [], "constants": []})
                    for cat, names in missing.items():
                        bucket[cat].extend(names)
        print()
    if total_missing == 0:
        logger.success("All public definitions are exported")
        return 0
    logger.warning(f"Total definitions not exported: {total_missing}")
    if args.autofix:
        print()
        print("Dry-run mode — no files will be modified" if args.dry_run else "Autofix — updating __init__.py files")
        ok = True
        for init_path, missing in missing_by_init.items():
            deduped = {cat: sorted(set(names)) for cat, names in missing.items()}
            if not autofix_init(init_path, deduped, dry_run=args.dry_run):
                ok = False
        return 0 if ok else 1
    print("Run with -a to add missing exports to __init__.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
