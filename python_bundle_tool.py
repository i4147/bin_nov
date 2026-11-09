#!/data/data/com.termux/files/usr/bin/env python
"""Merge a small Python package into one file (and split it back).
Analysis / deduplication ------------------------ Three different mergers existed: - ``mergepy.py``: naive concatenation + relative-import rewrite.
- ``merge_pkg.py``: AST surgery (hoist imports, reorder classes and functions, embed base64 assets, monkeypatch ``open``) - clever but the reordering changes semantics for any nontrivial package.
- ``prefactor.py``: embed each module's source, execute modules in dependency (topological) order inside real ``sys.modules`` entries.
This is the sound approach, so ``merge --mode bundle`` (default) keeps it (simplified, typed, ``# File:`` section per module).
``merge --mode concat`` reproduces ``mergepy`` for flat script piles and is exactly what ``split`` (ex-``unmerger.py``) can undo: files are separated by ``# File: <relpath>`` headers.
Relative-import rewriting in concat mode uses AST source segments instead of regexes, so multi-line imports survive.
Examples -------- python3 python_bundle_tool.py merge mypkg/ -o mypkg_single.py python3 python_bundle_tool.py merge scripts/ --mode concat -o all.py python3 python_bundle_tool.py split all.py -o restored/"""

import argparse
import ast
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys


FILE_HEADER: re.Pattern[str] = re.compile(r"^# File: (.+?)$", re.MULTILINE)
DEFAULT_MAX_FILES: int = 10


@dataclass(slots=True)
class ModuleInfo:
    """One source module and its local dependencies."""

    path: Path
    fullname: str
    source: str
    deps: set[str] = field(default_factory=set)


def find_py_files(root: Path, exclude: Path | None = None) -> list[Path]:
    """All ``*.py`` files under *root* (pycache/exclude skipped)."""
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if exclude is not None and (path == exclude or exclude in path.parents):
            continue
        files.append(path)
    return files


def fullname_for(root: Path, path: Path, package_mode: bool, package_name: str | None) -> str:
    """Dotted module name for *path* relative to *root*."""
    parts: list[str] = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
        if package_mode:
            return package_name or root.name
        return ".".join(parts) if parts else root.name
    if package_mode:
        return ".".join([package_name or root.name, *parts])
    return ".".join(parts)


def resolve_relative(current: str, module: str | None, level: int) -> str | None:
    """Absolute dotted name for a relative import."""
    if level == 0:
        return module
    parent: list[str] = current.split(".")[:-1]
    if level - 1 > len(parent):
        return None
    target: list[str] = parent[: len(parent) - (level - 1)]
    if module:
        target += module.split(".")
    return ".".join(target) if target else None


def analyze(path: Path, root: Path, package_mode: bool, package_name: str | None, known: set[str]) -> ModuleInfo:
    """Parse one module and collect its local import dependencies."""
    source: str = path.read_text(encoding="utf-8")
    fullname: str = fullname_for(root, path, package_mode, package_name)
    info = ModuleInfo(path=path, fullname=fullname, source=source)
    try:
        tree: ast.Module = ast.parse(source, filename=str(path))
    except SyntaxError:
        return info
    prefix: str = package_name or root.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if package_mode:
                    if alias.name == prefix or alias.name.startswith(prefix + "."):
                        info.deps.add(alias.name)
                elif alias.name.split(".")[0] in known:
                    info.deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved: str | None = resolve_relative(fullname, node.module, node.level or 0)
            if resolved:
                info.deps.add(resolved)
            elif package_mode and node.module and (node.module == prefix or node.module.startswith(prefix + ".")):
                info.deps.add(node.module)
    normalized: set[str] = set()
    for dep in info.deps:
        for candidate in known | info.deps:
            if candidate == dep or candidate.startswith(dep + ".") or dep.startswith(candidate + "."):
                if candidate in known or "." not in candidate:
                    normalized.add(candidate if candidate in known else dep)
    info.deps = {d for d in normalized if d in known} or (info.deps & known)
    return info


def topological_order(modules: dict[str, ModuleInfo]) -> tuple[list[str], bool]:
    """Dependency-first order (cycles appended alphabetically)."""
    edges: dict[str, set[str]] = {name: {d for d in info.deps if d in modules} for name, info in modules.items()}
    indegree: dict[str, int] = {name: len(deps) for name, deps in edges.items()}
    queue: deque[str] = deque(sorted(n for n, deg in indegree.items() if deg == 0))
    ordered: list[str] = []
    while queue:
        name: str = queue.popleft()
        ordered.append(name)
        for other, deps in edges.items():
            if name in deps:
                deps.discard(name)
                indegree[other] -= 1
                if indegree[other] == 0:
                    queue.append(other)
    has_cycle: bool = len(ordered) != len(modules)
    if has_cycle:
        ordered += sorted(set(modules) - set(ordered))
    return ordered, has_cycle


def bundle_source(modules: dict[str, ModuleInfo], ordered: list[str], top_name: str) -> str:
    """Generate the self-contained single file (prefactor strategy)."""
    lines: list[str] = [
        '"""Auto-generated single-file package (python_bundle_tool)."""',
        f"# Modules: {', '.join(ordered)}",
        "import sys, types",
        "",
        "_orig_sources = {",
    ]
    for name in ordered:
        lines.append(f"    {name!r}: {modules[name].source!r},")
    lines += [
        "}",
        "",
        "def get_original_source(module_name):",
        '    """Original source string for a bundled module, or None."""',
        "    return _orig_sources.get(module_name)",
        "",
        "for _name in _orig_sources:",
        "    _mod = types.ModuleType(_name)",
        '    _mod.__file__ = f"<bundled:{_name}>"',
        "    _mod.__package__ = _name.rpartition('.')[0]",
        "    sys.modules[_name] = _mod",
        "",
        f"_order = {ordered!r}",
        "for _name in _order:",
        "    _mod = sys.modules[_name]",
        '    exec(compile(_orig_sources[_name], f"<bundled:{_name}>", "exec"), _mod.__dict__)',
        "",
    ]
    if top_name in modules:
        lines += [
            "try:",
            f"    import {top_name} as _top",
            "    for _k, _v in vars(_top).items():",
            "        if not _k.startswith('_'):",
            "            globals()[_k] = _v",
            "except Exception:",
            "    pass",
            "",
        ]
    lines.append("# End of bundled package")
    return "\n".join(lines) + "\n"


def rewrite_relative_imports(source: str, root: Path, path: Path, package_name: str) -> str:
    """``from .x import y`` -> ``from <pkg>.x import y`` (concat mode)."""
    fullname: str = fullname_for(root, path, True, package_name)
    try:
        tree: ast.Module = ast.parse(source)
    except SyntaxError:
        return source
    edits: list[tuple[int, int, str]] = []
    src_lines: list[str] = source.splitlines(keepends=True)
    offsets: list[int] = [0]
    for line in src_lines:
        offsets.append(offsets[-1] + len(line))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0 and node.end_lineno:
            absolute: str | None = resolve_relative(fullname, node.module, node.level)
            if absolute is None:
                continue
            names: str = ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names)
            replacement: str = f"from {absolute} import {names}\n"
            start: int = offsets[node.lineno - 1]
            end: int = offsets[node.end_lineno]
            edits.append((start, end, replacement))
    result: str = source
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def concat_source(files: list[Path], root: Path, package_name: str) -> str:
    """Naive concatenation with ``# File:`` headers (mergepy strategy)."""
    chunks: list[str] = []
    for path in files:
        source: str = rewrite_relative_imports(path.read_text(encoding="utf-8"), root, path, package_name)
        rel: Path = path.relative_to(root)
        chunks.append(f"# File: {rel}\n{source.rstrip()}\n")
    return "\n".join(chunks)


def cmd_merge(args: argparse.Namespace) -> int:
    """Merge a package/script directory into one file."""
    root: Path = args.input.resolve()
    if not root.is_dir():
        print("Error: input must be a directory.", file=sys.stderr)
        return 2
    output: Path = (args.output or Path(f"{root.name}_single.py")).resolve()
    files: list[Path] = find_py_files(root, exclude=output)
    if not files:
        print("No Python files found.", file=sys.stderr)
        return 2
    if len(files) > args.max_files and not args.force:
        print(f"Found {len(files)} files > --max-files {args.max_files}; use --force.", file=sys.stderr)
        return 2
    package_mode: bool = (root / "__init__.py").exists()
    package_name: str | None = args.package_name or (root.name if package_mode else None)
    if args.mode == "concat":
        merged: str = concat_source(files, root, package_name or root.name)
        output.write_text(merged, encoding="utf-8")
        print(f"Merged {len(files)} files into {output} (concat)")
        return 0
    known: set[str] = {fullname_for(root, p, package_mode, package_name) for p in files}
    modules: dict[str, ModuleInfo] = {}
    for path in files:
        info: ModuleInfo = analyze(path, root, package_mode, package_name, known)
        modules[info.fullname] = info
    ordered, has_cycle = topological_order(modules)
    if has_cycle:
        print("Warning: import cycles detected; best-effort ordering used.", file=sys.stderr)
    output.write_text(bundle_source(modules, ordered, package_name or root.name), encoding="utf-8")
    print(f"Wrote bundled file: {output}")
    print(f"Modules ({len(modules)}): {', '.join(ordered)}")
    return 0


def unique_path(path: Path) -> Path:
    """Return *path* or ``name_N.suffix`` when it exists."""
    candidate: Path = path
    counter: int = 1
    while candidate.exists():
        candidate = path.parent / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    return candidate


def parse_merged(text: str) -> dict[str, str]:
    """Split ``# File:``-delimited content into {relpath: source}."""
    parts: list[str] = FILE_HEADER.split(text)
    files: dict[str, str] = {}
    for index in range(1, len(parts) - 1, 2):
        files[parts[index].strip()] = parts[index + 1].lstrip("\n").rstrip() + "\n"
    return files


def cmd_split(args: argparse.Namespace) -> int:
    """Undo a concat merge: recreate the original file tree."""
    files: dict[str, str] = parse_merged(args.input.read_text(encoding="utf-8"))
    if not files:
        print("No '# File:' sections found (only concat merges can be split).", file=sys.stderr)
        return 1
    for rel, content in files.items():
        dest: Path = args.out_dir / rel
        if dest.exists() and not args.force:
            dest = unique_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        print(f"Created: {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Merge/split small Python packages.")
    sub = parser.add_subparsers(dest="command", required=True)
    me = sub.add_parser("merge", help="Merge a directory of .py files into one")
    me.add_argument("input", nargs="?", type=Path, default=Path.cwd())
    me.add_argument("-o", "--output", type=Path, default=None, help="Default: <dirname>_single.py")
    me.add_argument(
        "--mode",
        choices=("bundle", "concat"),
        default="bundle",
        help="bundle: runnable embedded modules; concat: # File: text merge",
    )
    me.add_argument("--package-name", default=None)
    me.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    me.add_argument("--force", action="store_true", help="Allow > --max-files")
    me.set_defaults(handler=cmd_merge)
    sp = sub.add_parser("split", help="Split a concat merge back into files")
    sp.add_argument("input", type=Path)
    sp.add_argument("-o", "--out-dir", type=Path, default=Path.cwd())
    sp.add_argument("--force", action="store_true", help="Overwrite existing files")
    sp.set_defaults(handler=cmd_split)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
