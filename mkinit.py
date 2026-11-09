#!/data/data/com.termux/files/usr/bin/env python
"""Generate or refresh ``__init__.py`` files that re-export a package's public names.
Parses each module of the package, collects the public functions, classes and constants (honouring an existing ``__all__``), writes the ``from .module import name`` block within the line limit and skips private or excluded names.
``--recursive`` descends into sub-packages, ``--check``/``--dry-run`` only report.
Usage: mkinit [path] [-r] [--check] [--dry-run] [-e PATTERN] Positional arguments: path Options: -r, --recursive -n, --dry-run --check -e, --exclude --no-constants --upper-only --ignore-all Examples: $ mkinit pkg/ -r $ mkinit pkg/ --check Command-line script: run ``python mkinit.py`` or the ``mkinit`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Sequence
from dataclasses import dataclass
import fnmatch
from pathlib import Path
import sys


LINE_LIMIT: int = 88


@dataclass(frozen=True, slots=True)
class Options:
    exclude: tuple[str, ...] = ()
    include_constants: bool = True
    upper_only: bool = False
    respect_all: bool = True


def is_excluded(path: Path, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(path.stem, pattern) for pattern in patterns)


def is_valid_module_file(path: Path, options: Options) -> bool:
    return (
        path.is_file()
        and path.suffix == ".py"
        and not path.name.startswith("_")
        and path.stem.isidentifier()
        and not is_excluded(path, options.exclude)
    )


def is_valid_subpackage(path: Path, options: Options) -> bool:
    return (
        path.is_dir()
        and not path.name.startswith("_")
        and path.name.isidentifier()
        and not is_excluded(path, options.exclude)
        and (path / "__init__.py").is_file()
    )


def parse_module(path: Path) -> ast.Module | None:
    try:
        source = path.read_text(encoding="utf-8")
        return ast.parse(source, filename=str(path))
    except (UnicodeDecodeError, OSError, SyntaxError, ValueError):
        return None


def target_names(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in node.elts:
            names.extend(target_names(element))
        return names
    if isinstance(node, ast.Starred):
        return target_names(node.value)
    return []


def extract_declared_all(tree: ast.Module) -> list[str] | None:
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if any("__all__" in target_names(t) for t in node.targets):
                value = node.value
        elif isinstance(node, ast.AnnAssign) and "__all__" in target_names(node.target):
            value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            items = [el.value for el in value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)]
            if len(items) == len(value.elts):
                return items
    return None


def is_constant_allowed(name: str, options: Options) -> bool:
    if not options.include_constants:
        return False
    return name.isupper() if options.upper_only else True


def get_public_names(tree: ast.Module, options: Options) -> list[str]:
    if options.respect_all:
        declared = extract_declared_all(tree)
        if declared is not None:
            return list(dict.fromkeys(declared))
    type_alias = getattr(ast, "TypeAlias", None)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.extend(
                    n for n in target_names(target) if not n.startswith("_") and is_constant_allowed(n, options)
                )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names.extend(
                n for n in target_names(node.target) if not n.startswith("_") and is_constant_allowed(n, options)
            )
        elif type_alias is not None and isinstance(node, type_alias):
            alias_name: str = node.name.id
            if not alias_name.startswith("_"):
                names.append(alias_name)
    return list(dict.fromkeys(names))


def format_import(module_name: str, names: Sequence[str]) -> str:
    single = f"from .{module_name} import {', '.join(names)}"
    if len(single) <= LINE_LIMIT:
        return single
    body = "".join(f"    {name},\n" for name in names)
    return f"from .{module_name} import (\n{body})"


def build_init_content(project_dir: Path, options: Options) -> str:
    import_lines: list[str] = []
    exported: dict[str, str] = {}
    entries = sorted(project_dir.iterdir(), key=lambda p: p.name)
    for module_path in (p for p in entries if is_valid_module_file(p, options)):
        tree = parse_module(module_path)
        if tree is None:
            print(f"skipped (unparsable): {module_path}", file=sys.stderr)
            continue
        module_name = module_path.stem
        fresh: list[str] = []
        for name in get_public_names(tree, options):
            if name in exported:
                print(
                    f"skipped duplicate '{name}' in {module_path.name} (already from {exported[name]})",
                    file=sys.stderr,
                )
                continue
            exported[name] = module_path.name
            fresh.append(name)
        if fresh:
            import_lines.append(format_import(module_name, fresh))
        elif module_name not in exported:
            import_lines.append(f"from . import {module_name}")
            exported[module_name] = module_path.name
    for pkg in (p for p in entries if is_valid_subpackage(p, options)):
        if pkg.name in exported:
            continue
        import_lines.append(f"from . import {pkg.name}")
        exported[pkg.name] = pkg.name
    if not exported:
        return ""
    all_block = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in exported) + "]"
    return "\n".join(import_lines) + "\n\n" + all_block + "\n"


def process_package(
    project_dir: Path,
    options: Options,
    dry_run: bool,
    check: bool,
    recursive: bool,
) -> list[Path]:
    changed: list[Path] = []
    if recursive:
        for child in sorted(project_dir.iterdir(), key=lambda p: p.name):
            if is_valid_subpackage(child, options):
                changed.extend(process_package(child, options, dry_run, check, True))
    init_path = project_dir / "__init__.py"
    content = build_init_content(project_dir, options)
    try:
        existing = init_path.read_text(encoding="utf-8") if init_path.exists() else None
    except (UnicodeDecodeError, OSError):
        existing = None
    if existing == content:
        return changed
    changed.append(init_path)
    if dry_run or check:
        if dry_run:
            print(f"--- {init_path}\n{content}")
        return changed
    init_path.write_text(content, encoding="utf-8")
    return changed


def create_init_file(
    project_dir: Path | None = None,
    options: Options | None = None,
    dry_run: bool = False,
    check: bool = False,
    recursive: bool = False,
) -> Path:
    resolved = (project_dir or Path.cwd()).resolve()
    if not resolved.is_dir():
        msg = f"{resolved} is not a directory"
        raise NotADirectoryError(msg)
    process_package(resolved, options or Options(), dry_run, check, recursive)
    return resolved / "__init__.py"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gen-init")
    parser.add_argument("path", nargs="?", type=Path, default=None)
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("-e", "--exclude", action="append", default=[])
    parser.add_argument("--no-constants", action="store_true")
    parser.add_argument("--upper-only", action="store_true")
    parser.add_argument("--ignore-all", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    options = Options(
        exclude=tuple(args.exclude),
        include_constants=not args.no_constants,
        upper_only=args.upper_only,
        respect_all=not args.ignore_all,
    )
    root = (args.path or Path.cwd()).resolve()
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2
    changed = process_package(root, options, args.dry_run, args.check, args.recursive)
    if args.check:
        for path in changed:
            print(f"out of date: {path}", file=sys.stderr)
        return 1 if changed else 0
    if not args.dry_run:
        for path in changed:
            print(f"updated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
