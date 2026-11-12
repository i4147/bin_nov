#!/data/data/com.termux/files/usr/bin/env python
"""Check (and optionally fix) a package's ``__init__.py`` exports.
Analysis / deduplication ------------------------ ``check_funcs.py`` is a strict subset of ``check_exports.py`` (same AST scanning, no autofix), so it maps to the ``check`` subcommand and the larger script maps to ``check --fix``.
The ``loguru`` dependency was dropped in favour of plain printing; the duplicated autofix/__all__ assembly logic was factored into one helper.
A name counts as "defined" when it is a top-level public function, class or UPPER_CASE constant in a sibling module, and as "exported" when it (or its module) is imported in ``__init__.py`` or listed in ``__all__``.
Examples -------- python3 package_exports_tool.py check mypackage/ python3 package_exports_tool.py check mypackage/ --fix --dry-run python3 package_exports_tool.py check mypackage/ --fix
"""

import argparse
import ast
from collections.abc import Sequence
from pathlib import Path
import sys

Categories = dict[str, list[str]]  # {"functions": [...], "classes": [...], "constants": [...]}
MissingMap = dict[str, Categories]  # filename -> categories


def empty_categories() -> Categories:
    """Return an empty category mapping."""
    return {"functions": [], "classes": [], "constants": []}


def parse_file(path: Path) -> ast.Module | None:
    """Parse *path*, returning None (with a warning) on failure."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        print(f"warning: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def is_constant_name(name: str) -> bool:
    """Return True for public UPPER_CASE names."""
    return name.isupper() and not name.startswith("_")


def extract_definitions(path: Path) -> Categories:
    """Return public top-level functions/classes/constants defined in *path*."""
    result: Categories = empty_categories()
    tree: ast.Module | None = parse_file(path)
    if tree is None:
        return result
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            result["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            result["classes"].append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and is_constant_name(target.id):
                    result["constants"].append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if is_constant_name(node.target.id):
                result["constants"].append(node.target.id)
    return result


def literal_str_list(node: ast.expr) -> list[str]:
    """Return string constants of a list/tuple literal."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]


def extract_exports(init_path: Path) -> tuple[set[str], ast.Assign | None]:
    """Return (exported names, the ``__all__`` assignment node if present)."""
    exported: set[str] = set()
    all_node: ast.Assign | None = None
    tree: ast.Module | None = parse_file(init_path)
    if tree is None:
        return exported, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            all_node = node
            exported.update(literal_str_list(node.value))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    exported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exported.add(alias.asname or alias.name.split(".")[0])
    return exported, all_node


def find_missing(directory: Path) -> MissingMap:
    """Return definitions that are not exported from ``directory/__init__.py``."""
    init_path: Path = directory / "__init__.py"
    if not init_path.is_file():
        msg = f"Error: no __init__.py found in {directory}"
        raise SystemExit(msg)
    exported, _ = extract_exports(init_path)
    missing: MissingMap = {}
    modules = sorted(p for p in directory.glob("*.py") if p.name != "__init__.py" and not p.name.startswith("_"))
    for py_file in modules:
        file_missing: Categories = empty_categories()
        for category, names in extract_definitions(py_file).items():
            file_missing[category] = [name for name in names if name not in exported and py_file.stem not in exported]
        if any(file_missing.values()):
            missing[py_file.name] = file_missing
    return missing


def import_lines(missing: MissingMap) -> list[str]:
    """Build ``from .module import ...`` lines for the missing names."""
    lines: list[str] = []
    for filename in sorted(missing):
        cats: Categories = missing[filename]
        names: list[str] = sorted(cats["classes"] + cats["constants"] + cats["functions"])
        if names:
            joined: str = names[0] if len(names) == 1 else "(" + ", ".join(names) + ")"
            lines.append(f"from .{Path(filename).stem} import {joined}")
    return lines


def all_missing_names(missing: MissingMap) -> list[str]:
    """Return every missing name, sorted."""
    return sorted({name for cats in missing.values() for names in cats.values() for name in names})


def fix_init(init_path: Path, missing: MissingMap, dry_run: bool) -> bool:
    """Append imports for missing names and merge them into ``__all__``."""
    new_names: list[str] = all_missing_names(missing)
    imports: list[str] = import_lines(missing)
    if not new_names:
        return True
    original: str = init_path.read_text(encoding="utf-8")
    _, all_node = extract_exports(init_path)
    existing: set[str] = set(literal_str_list(all_node.value)) if all_node else set()
    combined: list[str] = sorted(existing | set(new_names))
    all_block: str = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in combined) + "]"
    if all_node is not None and all_node.end_lineno is not None:
        lines: list[str] = original.splitlines(keepends=True)
        updated: str = "".join(lines[: all_node.lineno - 1]) + all_block + "\n" + "".join(lines[all_node.end_lineno :])
        if imports:
            updated = updated.rstrip("\n") + "\n\n" + "\n".join(imports) + "\n"
    else:
        updated = original.rstrip("\n") + "\n\n" + "\n".join(imports) + "\n\n" + all_block + "\n"
    if dry_run:
        print(f"--- would write additions to {init_path} ---")
        print("\n".join(imports))
        print(all_block)
        return True
    init_path.write_text(updated, encoding="utf-8")
    print(f"Updated {init_path} with {len(new_names)} new export(s)")
    return True


def cmd_check(args: argparse.Namespace) -> int:
    """Run the ``check`` subcommand."""
    directory: Path = args.directory
    missing: MissingMap = find_missing(directory)
    if not missing:
        print("All definitions are exported in __init__.py")
        return 0
    total: int = 0
    print("Definitions NOT exported in __init__.py:")
    for filename in sorted(missing):
        print(f"\n{filename}")
        for category in ("functions", "classes", "constants"):
            names: list[str] = missing[filename][category]
            if names:
                total += len(names)
                print(f"  {category}: {', '.join(sorted(names))}")
    print(f"\nTotal missing: {total}")
    if args.fix:
        ok: bool = fix_init(directory / "__init__.py", missing, args.dry_run)
        return 0 if ok else 1
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Check __init__.py exports against package modules.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Report (and optionally fix) missing exports")
    check.add_argument(
        "directory", nargs="?", type=Path, default=Path.cwd(), help="Package directory (default: current dir)"
    )
    check.add_argument("-a", "--fix", action="store_true", help="Add missing imports to __init__.py")
    check.add_argument("--dry-run", action="store_true", help="With --fix, preview without writing")
    check.set_defaults(handler=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
