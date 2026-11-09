#!/data/data/com.termux/files/usr/bin/env python
"""Toolbox for the local ``dh`` package: reverse, inline and usage report.
Subcommands: ``reverse`` replaces the locally copied ``dh`` functions with ``from dh import ...`` (``-a`` writes, the default is a dry run), ``inline`` pastes the imported sources back into the files and ``usage`` scans a script folder and reports which ``dh`` helpers it uses.
``--match`` and ``--import-style`` choose how the functions are recognised and imported.
Usage: dhtools {reverse,inline,usage} [-a] [paths ...] Positional arguments: paths ...
Files or directories to process (default: current directory).
Subcommands: reverse Replace locally-copied dh functions with `from dh import ...`.
inline Inline `from dh import X` imports into the file source.
usage Scan a directory of Python scripts and report dh usage.
Options: -v, --verbose Enable debug logging and per-file match details.
-a, --apply Write changes in place (default: dry-run).
--match 'normalized' = ast.unparse after stripping docstrings (dh_reverse.py).
'raw' = raw source-line hash (fixdh.py, reverse_inline.py).
Default: normalized.
--import-style 'flat' -> `from dh import X` (dh_reverse / fixdh).
'module' -> `from dh.<mod> import X` (reverse_inline).
Default: flat.
--prune-imports Remove top-level imports that become unused (fixdh behaviour).
--dh-path --skip-file --bin-dir --report-path --dh-name Top-level package name to look for (default: dh).
Examples: $ dhtools reverse ~/bin -a Command-line script: run ``python dhtools.py`` or the ``dhtools`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
import hashlib
from pathlib import Path
import re
import sys
from typing import Iterable

from dh import MAX_WORKERS
from loguru import logger


log = logger
DEFAULT_DH_PATH: Path = Path.home() / "projects" / "py" / "dh" / "src" / "dh"
DEFAULT_BIN_DIR: Path = Path.home() / "bin"
DEFAULT_REPORT_PATH: Path = Path.home() / "dh_usage.txt"
DEFAULT_SKIP: frozenset[str] = frozenset({"dh_reverse.py"})


def _iter_py_files(paths: Iterable[Path], skip: frozenset[str] = frozenset()) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            if p.name not in skip:
                out.append(p)
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if f.name not in skip:
                    out.append(f)
    return out


def _read_and_parse(path: Path) -> tuple[str, ast.Module] | tuple[None, None]:
    try:
        src = path.read_text(encoding="utf-8")
        return src, ast.parse(src)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None, None


def _strip_docstring_and_unparse(node: ast.FunctionDef) -> str:
    body = [
        stmt
        for stmt in node.body
        if not (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
        )
    ]
    clone = ast.FunctionDef(
        name=node.name,
        args=node.args,
        body=body,
        decorator_list=node.decorator_list,
        returns=node.returns,
        type_comment=None,
        lineno=node.lineno,
        col_offset=node.col_offset,
    )
    src = ast.unparse(clone)
    return "\n".join(line.strip() for line in src.split("\n") if line.strip())


def _normalized_hash(node: ast.FunctionDef) -> str:
    return hashlib.sha256(_strip_docstring_and_unparse(node).encode()).hexdigest()


def _raw_source(source: str, node: ast.FunctionDef) -> str:
    lines = source.split("\n")
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _raw_hash(source: str, node: ast.FunctionDef) -> str:
    return hashlib.sha256(_raw_source(source, node).encode()).hexdigest()


def _function_hash(source: str, node: ast.FunctionDef, match: str) -> str:
    return _normalized_hash(node) if match == "normalized" else _raw_hash(source, node)


def _build_dh_map(dh_path: Path, match: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for py_file in sorted(dh_path.glob("**/*.py")):
        source, tree = _read_and_parse(py_file)
        if tree is None or source is None:
            continue
        stem = py_file.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                h = _function_hash(source, node, match)
                if node.name in result:
                    log.warning("Duplicate function '{}' in dh package", node.name)
                result[node.name] = (stem, h)
    return result


def _insert_dh_imports(
    source: str,
    matched: dict[str, str],
    style: str,
) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines(keepends=True)
    insert_at = 1 if (lines and lines[0].startswith("#!")) else 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = node.end_lineno or insert_at
        else:
            break
    if style == "flat":
        for node in tree.body:
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                break
            if isinstance(node, ast.ImportFrom) and node.module == "dh" and node.level == 0:
                existing = {a.name for a in node.names}
                merged = sorted(existing | set(matched))
                new_line = f"from dh import {', '.join(merged)}\n"
                lines[node.lineno - 1 : node.end_lineno] = [new_line]
                return "".join(lines)
        stmt = f"from dh import {', '.join(sorted(matched))}\n"
        lines.insert(insert_at, stmt)
        return "".join(lines)
    stmts = [f"from dh.{matched[name]} import {name}\n" for name in sorted(matched)]
    for i, stmt in enumerate(stmts):
        lines.insert(insert_at + i, stmt)
    return "".join(lines)


def _prune_unused_imports(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base: ast.AST = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
    lines = source.splitlines(keepends=True)
    to_remove: list[tuple[int, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        bound = [a.asname or a.name.split(".")[0] for a in node.names]
        if not any(b in used for b in bound):
            to_remove.append((node.lineno - 1, node.end_lineno))
    for start, end in sorted(to_remove, reverse=True):
        del lines[start:end]
    return "".join(lines)


def _apply_reverse_to_file(
    path: Path,
    dh_map: dict[str, tuple[str, str]],
    match: str,
    import_style: str,
    apply: bool,
    prune: bool,
    debug: bool,
) -> tuple[Path, bool, str]:
    source, tree = _read_and_parse(path)
    if tree is None or source is None:
        return (path, False, "")
    matched: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in dh_map:
            continue
        module, expected = dh_map[node.name]
        if _function_hash(source, node, match) == expected:
            matched[node.name] = module
    if debug:
        if matched:
            log.debug("{} matched: {}", path.name, sorted(matched))
        else:
            log.debug("{}: no dh functions matched", path.name)
    if not matched:
        return (path, False, "")
    lines = source.splitlines(keepends=True)
    remove_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in matched:
            remove_ranges.append((node.lineno - 1, node.end_lineno))
    for start, end in sorted(remove_ranges, reverse=True):
        del lines[start:end]
    new_source = "".join(lines)
    new_source = _insert_dh_imports(new_source, matched, import_style)
    if prune:
        new_source = _prune_unused_imports(new_source)
    if new_source == source:
        return (path, False, "")
    if not apply:
        return (path, True, f"Would update {path.name}: remove {sorted(matched)}")
    path.write_text(new_source, encoding="utf-8")
    return (path, True, f"Updated {path.name}: removed {sorted(matched)}")


def _reverse_worker(args: tuple) -> tuple[Path, bool, str]:
    return _apply_reverse_to_file(*args)


def cmd_reverse(args: argparse.Namespace) -> int:
    dh_path: Path = args.dh_path
    if not dh_path.is_dir():
        log.error("dh package not found at {}", dh_path)
        return 1
    print(f"Loading dh functions from {dh_path} (match={args.match}) ...")
    dh_map = _build_dh_map(dh_path, args.match)
    print(f"Loaded {len(dh_map)} functions from dh package\n")
    skip = frozenset(args.skip_file)
    files = _iter_py_files(args.paths, skip=skip)
    if not files:
        print("No Python files found to process.")
        return 0
    mode = "APPLYING CHANGES" if args.apply else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Processing {len(files)} Python files...\n")
    worker_args = [
        (
            f,
            dh_map,
            args.match,
            args.import_style,
            args.apply,
            args.prune_imports,
            args.verbose,
        )
        for f in files
    ]
    updated = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, changed, msg in pool.map(_reverse_worker, worker_args):
            if msg:
                print(msg)
            if changed:
                updated += 1
    print("=" * 40)
    if args.apply:
        print(f"Updated {updated} files")
    else:
        print(f"Would update {updated} files (use -a/--apply to apply)")
    return 0


def _build_dh_export_map(dh_path: Path) -> dict[str, Path]:
    init = dh_path / "__init__.py"
    if not init.exists():
        msg = f"Could not find __init__.py at {init}"
        raise FileNotFoundError(msg)
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return {}
    exports: dict[str, Path] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            module_file = dh_path / f"{node.module}.py"
            for alias in node.names:
                exports[alias.name] = module_file
    return exports


def _collect_local_refs(node: ast.AST, names: set[str]) -> set[str]:
    refs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in names:
            refs.add(child.id)
    return refs


def _collect_inline_source(module_file: Path, func_name: str) -> tuple[list[str], list[str]]:
    if not module_file.exists():
        return ([], [])
    try:
        source = module_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return ([], [])
    lines = source.splitlines()
    defined: dict[str, ast.stmt] = {}
    imports: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined[node.name] = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined[tgt.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            level = getattr(node, "level", 0) or 0
            if mod != "dh" and level == 0:
                imports.append(node)
    if func_name not in defined:
        return ([], [])
    seen: set[str] = set()
    queue: list[str] = [func_name]
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        node = defined.get(name)
        if node is None:
            continue
        for ref in _collect_local_refs(node, set(defined.keys())):
            if ref not in seen:
                queue.append(ref)
    blocks: list[str] = []
    for name in sorted(seen, key=lambda n: defined[n].lineno):
        node = defined[name]
        end = node.end_lineno or node.lineno
        blocks.append("\n".join(lines[node.lineno - 1 : end]))
    joined = "\n".join(blocks)
    needed: set[str] = set()
    for imp in imports:
        for alias in imp.names:
            bound = alias.asname or alias.name
            if re.search(rf"\b{re.escape(bound)}\b", joined):
                needed.add(ast.unparse(imp))
    return (sorted(needed), blocks)


def _inline_file(path: Path, dh_map: dict[str, Path], apply: bool) -> tuple[Path, bool, str]:
    if path.resolve() == Path(__file__).resolve():
        return (path, False, "")
    source, tree = _read_and_parse(path)
    if tree is None or source is None or "dh" not in source:
        return (path, False, "")
    lines = source.splitlines(keepends=True)
    dh_import_ranges: list[tuple[int, int]] = []
    dh_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "dh" and node.level == 0:
            dh_import_ranges.append((node.lineno - 1, node.end_lineno))
            for a in node.names:
                dh_names.add(a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "dh":
                    dh_import_ranges.append((node.lineno - 1, node.end_lineno))
    if not dh_names:
        return (path, False, "")
    for start, end in sorted(dh_import_ranges, reverse=True):
        del lines[start:end]
    needed_imports: set[str] = set()
    source_blocks: list[str] = []
    for name in sorted(dh_names):
        if name not in dh_map:
            source_blocks.append(f"# WARNING: Source code for '{name}' not found.")
            continue
        imp, blocks = _collect_inline_source(dh_map[name], name)
        needed_imports.update(imp)
        for block in blocks:
            if block not in source_blocks:
                source_blocks.append(block)
    if not source_blocks:
        return (path, False, "")
    insert_lines: list[str] = []
    if needed_imports:
        insert_lines.append("\n".join(needed_imports))
    insert_lines.extend(source_blocks)
    insertion = "\n\n" + "\n\n".join(insert_lines) + "\n\n"
    insert_at = 1 if (lines and lines[0].startswith("#!")) else 0
    try:
        new_tree = ast.parse("".join(lines))
        for node in new_tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = max(insert_at, node.end_lineno or insert_at)
            else:
                break
    except SyntaxError:
        pass
    new_source = "".join(lines[:insert_at]) + insertion + "".join(lines[insert_at:])
    if not apply:
        return (
            path,
            True,
            f"Would inline {sorted(dh_names)} in {path.name}",
        )
    path.write_text(new_source, encoding="utf-8")
    return (path, True, f"Inlined {sorted(dh_names)} in {path.name}")


def _inline_worker(args: tuple) -> tuple[Path, bool, str]:
    return _inline_file(*args)


def cmd_inline(args: argparse.Namespace) -> int:
    dh_path: Path = args.dh_path
    if not dh_path.is_dir():
        log.error("dh package not found at {}", dh_path)
        return 1
    print(f"Building function map from {dh_path}...")
    try:
        dh_map = _build_dh_export_map(dh_path)
    except FileNotFoundError as e:
        log.error("{}", e)
        return 1
    print(f"Found {len(dh_map)} functions in dh package\n")
    files = _iter_py_files(args.paths)
    if not files:
        print("No Python files found to process.")
        return 0
    mode = "APPLYING CHANGES" if args.apply else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Processing {len(files)} Python files...\n")
    worker_args = [(f, dh_map, args.apply) for f in files]
    updated = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, changed, msg in pool.map(_inline_worker, worker_args):
            if msg:
                print(msg)
            if changed:
                updated += 1
    verb = "Updated" if args.apply else "Would update"
    print("=" * 40)
    print(f"{verb} {updated} files")
    if not args.apply and updated:
        print("(use -a/--apply to write changes)")
    return 0


def _scan_dh_references(py_file: Path, dh_name: str) -> list[str]:
    _source, tree = _read_and_parse(py_file)
    if tree is None:
        print(f"   ⚠️  Skipping {py_file.name}: could not parse")
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == dh_name or node.module.startswith(dh_name + "."))
        ):
            for alias in node.names:
                names.append(alias.asname or alias.name)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == dh_name or alias.name.startswith(dh_name + "."):
                    imported_roots.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in imported_roots:
                names.append(func.attr)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            base: ast.AST = func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in imported_roots:
                names.append(func.attr)
    return names


def _count_dh_calls(py_file: Path, names: set[str]) -> dict[str, int]:
    _, tree = _read_and_parse(py_file)
    if tree is None:
        return {}
    counts: dict[str, int] = dict.fromkeys(names, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in counts:
            counts[node.func.id] += 1
    return counts


def _format_usage_report(
    bin_dir: Path,
    per_file: list[tuple[str, dict[str, int]]],
    per_function: dict[str, dict[str, int]],
    dh_name: str,
) -> str:
    lines: list[str] = []
    lines.append("=" * 40)
    lines.append(f"  {dh_name} Usage Report — generated {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("=" * 40)
    lines.append(f"  Scanned: {bin_dir}")
    lines.append(f"  Files with {dh_name} imports: {len(per_file)}")
    lines.append(f"  Unique {dh_name} functions used: {len(per_function)}")
    lines.append("")
    lines.append(f"{'Function':<30} {'Total Calls':<15} {'Files Used In':<15}")
    lines.append("-" * 40)
    for name in sorted(per_function, key=lambda n: -sum(per_function[n].values())):
        total = sum(per_function[name].values())
        files = len(per_function[name])
        lines.append(f"{name:<30} {total:<15} {files:<15}")
    lines.append("")
    lines.append("-" * 40)
    lines.append("  PER-FILE BREAKDOWN")
    lines.append("-" * 40)
    for fname, counts in sorted(per_file, key=lambda x: -sum(x[1].values())):
        total = sum(counts.values())
        lines.append(f"\n  📄 {fname}  ({total} call(s))")
        for name in sorted(counts, key=lambda n: -counts[n]):
            if counts[name] > 0:
                lines.append(f"      {name:<30} {counts[name]} time(s)")
    lines.append("")
    lines.append("=" * 40)
    lines.append("  END OF REPORT")
    lines.append("=" * 40)
    return "\n".join(lines)


def cmd_usage(args: argparse.Namespace) -> int:
    bin_dir: Path = args.bin_dir
    report_path: Path = args.report_path
    dh_name: str = args.dh_name
    if not bin_dir.is_dir():
        print(f"❌ {bin_dir} does not exist or is not a directory.")
        return 1
    files = sorted(bin_dir.glob("*.py"))
    if not files:
        print(f"⚠️  No .py files found in {bin_dir}.")
        return 0
    print(f"🔍 Scanning {len(files)} Python file(s) in {bin_dir} ...\n")
    per_function: dict[str, dict[str, int]] = {}
    per_file: list[tuple[str, dict[str, int]]] = []
    for f in files:
        names = _scan_dh_references(f, dh_name)
        if not names:
            continue
        counts = _count_dh_calls(f, set(names))
        per_file.append((f.name, counts))
        for name, cnt in counts.items():
            per_function.setdefault(name, {})[f.name] = cnt
    if not per_function:
        msg = f"No imports from '{dh_name}' found in {bin_dir}.\n"
        print(f"✅ {msg.strip()}")
        report_path.write_text(msg, encoding="utf-8")
        return 0
    report = _format_usage_report(bin_dir, per_file, per_function, dh_name)
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n✅ Report saved to {report_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dh_tools",
        description=(
            "Unified toolbox for the dh package: reverse (deduplicate "
            "local copies into imports), inline (paste source back), and "
            "usage (report calls)."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging and per-file match details.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser(
        "reverse",
        help="Replace locally-copied dh functions with `from dh import ...`.",
    )
    r.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path.cwd()],
        help="Files or directories to process (default: current directory).",
    )
    r.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Write changes in place (default: dry-run).",
    )
    r.add_argument(
        "--match",
        choices=["normalized", "raw"],
        default="normalized",
        help=(
            "'normalized' = ast.unparse after stripping docstrings "
            "(dh_reverse.py). 'raw' = raw source-line hash (fixdh.py, "
            "reverse_inline.py). Default: normalized."
        ),
    )
    r.add_argument(
        "--import-style",
        choices=["flat", "module"],
        default="flat",
        help=(
            "'flat' -> `from dh import X` (dh_reverse / fixdh). "
            "'module' -> `from dh.<mod> import X` (reverse_inline). "
            "Default: flat."
        ),
    )
    r.add_argument(
        "--prune-imports",
        action="store_true",
        help="Remove top-level imports that become unused (fixdh behaviour).",
    )
    r.add_argument(
        "--dh-path",
        type=Path,
        default=DEFAULT_DH_PATH,
        help=f"Path to the dh package (default: {DEFAULT_DH_PATH}).",
    )
    r.add_argument(
        "--skip-file",
        action="append",
        default=list(DEFAULT_SKIP),
        help=(f"Filename to skip (can repeat). Default: {sorted(DEFAULT_SKIP)}."),
    )
    i = sub.add_parser(
        "inline",
        help="Inline `from dh import X` imports into the file source.",
    )
    i.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path.cwd()],
        help="Files or directories to process (default: current directory).",
    )
    i.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Write changes in place (default: dry-run).",
    )
    i.add_argument(
        "--dh-path",
        type=Path,
        default=DEFAULT_DH_PATH,
        help=f"Path to the dh package (default: {DEFAULT_DH_PATH}).",
    )
    u = sub.add_parser(
        "usage",
        help="Scan a directory of Python scripts and report dh usage.",
    )
    u.add_argument(
        "--bin-dir",
        type=Path,
        default=DEFAULT_BIN_DIR,
        help=f"Directory to scan (default: {DEFAULT_BIN_DIR}).",
    )
    u.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Path to write the report to (default: {DEFAULT_REPORT_PATH}).",
    )
    u.add_argument(
        "--dh-name",
        default="dh",
        help="Top-level package name to look for (default: dh).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "reverse":
        return cmd_reverse(args)
    if args.command == "inline":
        return cmd_inline(args)
    if args.command == "usage":
        return cmd_usage(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
