#!/data/data/com.termux/files/usr/bin/env python
"""Extract imports from Python files (raw lines, module sets, requirements).
Analysis / deduplication ------------------------ Seven scripts did three jobs, each reimplemented with tree-sitter *and* with ``ast``.
The AST flavour is stdlib-only and strictly more accurate, so tree-sitter was dropped: - ``ex_imports.py`` / ``eximports2.py`` (write each file's import lines to an output tree) -> ``lines`` command.
- ``eximports.py`` / ``eximports3.py`` (collect unique imports / requirements) -> ``modules`` / ``requirements``.
- ``fimport.py`` (imports with top-level vs nested context) -> ``modules --details``.
- ``import_collector.py`` + ``importz.py`` (third-party detection with the import->pip name map and an install script) -> ``requirements`` (writes ``requirements.txt`` + ``install_deps.sh`` for missing ones).
Extension-less files with a python shebang count as Python, as in ``importz.py``.
Examples -------- python3 python_imports_tool.py modules ~/project python3 python_imports_tool.py lines ~/project -o imports_out/ python3 python_imports_tool.py requirements ~/project"""

import argparse
import ast
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import importlib.metadata
import importlib.util
from pathlib import Path
import sys

from dh import MAX_WORKERS, SKIP_DIRS


STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names)
PACKAGE_MAPPING: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "fitz": "pymupdf",
    "skimage": "scikit-image",
    "telegram": "python-telegram-bot",
    "dateutil": "python-dateutil",
    "git": "GitPython",
    "jwt": "PyJWT",
    "OpenGL": "PyOpenGL",
}


def is_python_file(path: Path) -> bool:
    """True for ``.py`` files and extension-less files with a python shebang."""
    if path.suffix == ".py":
        return True
    if path.is_file() and not path.suffix:
        try:
            first: str = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            return "python" in first
        except (IndexError, OSError):
            return False
    return False


def find_python_files(root: Path) -> list[Path]:
    """Return all Python files under *root*."""
    candidates = root.rglob("*") if root.is_dir() else iter((root,))
    return sorted(p for p in candidates if p.is_file() and not SKIP_DIRS.intersection(p.parts) and is_python_file(p))


def local_module_names(root: Path) -> set[str]:
    """Names importable from *root* itself (modules + packages)."""
    names: set[str] = {p.stem for p in root.glob("*.py")}
    names.update(p.name for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    return names


def parse_tree(path: Path) -> ast.Module | None:
    """Parse *path*, returning None on syntax/encoding errors."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def import_lines_of(path: Path) -> tuple[Path, list[str]]:
    """Return the source text of every import statement in *path*."""
    tree: ast.Module | None = parse_tree(path)
    if tree is None:
        return path, []
    source_lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.end_lineno:
            found.append("\n".join(source_lines[node.lineno - 1 : node.end_lineno]))
    return path, found


def top_level_modules_of(path: Path) -> tuple[Path, set[str]]:
    """Return the set of top-level module names imported by *path*."""
    tree: ast.Module | None = parse_tree(path)
    names: set[str] = set()
    if tree is None:
        return path, names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return path, names


def module_contexts(path: Path) -> list[str]:
    """Describe each import with its nesting context (fimport-style)."""
    tree: ast.Module | None = parse_tree(path)
    if tree is None:
        return []
    notes: list[str] = []

    def visit(node: ast.AST, ctx: str) -> None:
        for child in ast.iter_child_nodes(node):
            sub_ctx: str = ctx
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sub_ctx = f"in {child.name}()"
            elif isinstance(child, ast.ClassDef):
                sub_ctx = f"in class {child.name}"
            if isinstance(child, ast.Import):
                for alias in child.names:
                    notes.append(f"import {alias.name} [{sub_ctx}] (line {child.lineno})")
            elif isinstance(child, ast.ImportFrom):
                mod: str = "." * child.level + (child.module or "")
                names: str = ", ".join(a.name for a in child.names)
                notes.append(f"from {mod} import {names} [{sub_ctx}] (line {child.lineno})")
            visit(child, sub_ctx)

    visit(tree, "top-level")
    return notes


def third_party(root: Path, files: list[Path], workers: int) -> list[str]:
    """Return sorted third-party module names used under *root*."""
    local: set[str] = local_module_names(root)
    all_names: set[str] = set()
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        for _path, names in pool.map(top_level_modules_of, files):
            all_names |= names
    return sorted(name for name in all_names if name not in STDLIB and name not in local and name != "__future__")


def is_installed(module_name: str) -> bool:
    """True when a distribution or import spec exists for *module_name*."""
    try:
        importlib.metadata.distribution(module_name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return importlib.util.find_spec(module_name) is not None


def cmd_lines(args: argparse.Namespace) -> int:
    """Write every file's import statements to an output tree."""
    files: list[Path] = find_python_files(args.root)
    written: int = 0
    for path in files:
        _path, lines = import_lines_of(path)
        if not lines:
            continue
        rel: Path = path.relative_to(args.root) if args.root in path.parents else Path(path.name)
        dest: Path = args.out_dir / rel.with_suffix(".imports.txt")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written += 1
    print(f"Wrote imports for {written} file(s) under {args.out_dir}")
    return 0


def cmd_modules(args: argparse.Namespace) -> int:
    """List imported modules, optionally classified / per file."""
    files: list[Path] = find_python_files(args.root)
    if args.details:
        for path in files:
            notes: list[str] = module_contexts(path)
            if notes:
                print(f"\n{path}:")
                for note in notes:
                    print(f"  {note}")
        return 0
    names: set[str] = set()
    with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        for _path, found in pool.map(top_level_modules_of, files):
            names |= found
    local: set[str] = local_module_names(args.root)
    if args.filter == "third-party":
        shown: list[str] = sorted(n for n in names if n not in STDLIB and n not in local)
    elif args.filter == "stdlib":
        shown = sorted(n for n in names if n in STDLIB)
    else:
        shown = sorted(names)
    text: str = "\n".join(shown)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Saved {len(shown)} module(s) to {args.output}")
    else:
        print(text)
        print(f"# {len(shown)} module(s) from {len(files)} file(s)", file=sys.stderr)
    return 0


def cmd_requirements(args: argparse.Namespace) -> int:
    """Write requirements (+ install script for the missing ones)."""
    files: list[Path] = find_python_files(args.root)
    deps: list[str] = third_party(args.root, files, MAX_WORKERS)
    if not deps:
        print("No third-party imports found.")
        return 0
    installed: list[str] = [d for d in deps if is_installed(d)]
    missing: list[str] = [PACKAGE_MAPPING.get(d, d) for d in deps if not is_installed(d)]
    pip_names: list[str] = [PACKAGE_MAPPING.get(d, d) for d in deps]
    args.out.write_text("\n".join(sorted(set(pip_names))) + "\n", encoding="utf-8")
    print(f"Wrote {len(deps)} requirement(s) to {args.out}")
    if installed:
        print(f"Already installed: {', '.join(installed)}")
    if missing:
        script: Path = Path("install_deps.sh")
        script.write_text(f"#!/bin/sh\npip install {' '.join(sorted(set(missing)))}\n", encoding="utf-8")
        script.chmod(0o755)
        print(f"Missing: {', '.join(sorted(set(missing)))} - run ./{script.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Extract/analyse Python imports.")
    sub = parser.add_subparsers(dest="command", required=True)

    ln = sub.add_parser("lines", help="Save each file's import lines to a tree")
    ln.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    ln.add_argument("-o", "--out-dir", type=Path, default=Path("output"))
    ln.set_defaults(handler=cmd_lines)

    mo = sub.add_parser("modules", help="List imported top-level modules")
    mo.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    mo.add_argument("--filter", choices=("all", "stdlib", "third-party"), default="all")
    mo.add_argument("--details", action="store_true", help="Per-file imports with nesting context")
    mo.add_argument("-o", "--output", type=Path, default=None, help="Output file (original: importz.txt)")
    mo.set_defaults(handler=cmd_modules)

    rq = sub.add_parser("requirements", help="Write pip requirements for third-party imports")
    rq.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    rq.add_argument("-o", "--out", type=Path, default=Path("requirements.txt"))
    rq.set_defaults(handler=cmd_requirements)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
