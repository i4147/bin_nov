#!/data/data/com.termux/files/usr/bin/env python
"""Detect and add ``if __name__ == "__main__":`` guards to Python files.
The ``check`` subcommand reports the files without a guard, ``fix`` either moves the top-level code into a ``main()`` (``--strategy ast``) or appends the guard with a placeholder ``main()`` (``--strategy template``); ``--exclude``, ``--dry-run`` and ``--indent`` tune the run.
Usage: addmainguard {check,fix} [options] [paths ...] Positional arguments: paths ...
Files or directories (default: current directory) Subcommands: check Report files missing a main guard fix Add main guards to files missing them Options: --exclude Extra directory name to exclude (repeatable) --no-default-excludes Do not apply the built-in exclusion list -v, --verbose Verbose logging --detect Detection backend (default: ast) --strategy Fix strategy: 'ast' moves top-level code into main(); 'template' inserts a placeholder main() (default: ast) --dry-run Preview without writing --indent Examples: $ addmainguard check src/ $ addmainguard fix src/ --dry-run Command-line script: run ``python addmainguard.py`` or the ``addmainguard`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import re
import sys
from typing import Sequence

from dh import MAX_WORKERS
from loguru import logger

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
)
DEFAULT_INDENT: int = 4
MAIN_GUARD_RE: re.Pattern[str] = re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']\s*:')
MAIN_TEMPLATE: str = (
    "\n\n"
    "def main() -> None:\n"
    '    """Entry point for the script."""\n'
    "    # TODO: Add your main logic here\n"
    '    print("Hello from main!")\n'
)
GUARD_TEMPLATE: str = '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def is_main_guard_if(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left = test.left
    if not (isinstance(left, ast.Name) and left.id == "__name__"):
        return False
    if len(test.comparators) != 1:
        return False
    comp = test.comparators[0]
    return isinstance(comp, ast.Constant) and comp.value == "__main__"


def has_main_guard_ast(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.If) and is_main_guard_if(n) for n in tree.body)


def is_movable(node: ast.stmt) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return False
    return not (isinstance(node, ast.If) and is_main_guard_if(node))


def indent_text(text: str, spaces: int = DEFAULT_INDENT) -> str:
    prefix = " " * spaces
    out: list[str] = []
    for line in text.splitlines(True):
        if line.strip() == "":
            out.append(line)
        else:
            out.append(prefix + line)
    return "".join(out)


def fix_with_ast(
    path: Path,
    *,
    indent_size: int = DEFAULT_INDENT,
    dry_run: bool = False,
) -> tuple[str, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "error", f"read failed: {exc}"
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return "error", f"parse error: {exc}"
    if has_main_guard_ast(tree):
        return "skipped", "already has main guard"
    top = list(tree.body)
    movable = [n for n in top if is_movable(n)]
    if not movable:
        return "skipped", "nothing to wrap"
    lines = source.splitlines(True)
    body_chunks: list[str] = []
    keep_ranges: list[tuple[int, int]] = []
    for node in top:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not isinstance(start, int) or not isinstance(end, int):
            return "error", "missing lineno/end_lineno"
        if is_movable(node):
            body_chunks.append("".join(lines[start - 1 : end]).rstrip() + "\n")
        else:
            keep_ranges.append((start, end))
    keep_ranges.sort()
    kept: list[str] = []
    cursor = 1
    for start, end in keep_ranges:
        if start > cursor:
            kept.append("".join(lines[cursor - 1 : start - 1]))
        kept.append("".join(lines[start - 1 : end]))
        cursor = end + 1
    if cursor <= len(lines):
        kept.append("".join(lines[cursor - 1 :]))
    body = "".join(body_chunks).rstrip("\n")
    tail: list[str] = ["\n\n", "def main():\n"]
    if body.strip() == "":
        tail.append("    pass\n")
    else:
        tail.append(indent_text(body + "\n", indent_size).rstrip("\n") + "\n")
    tail.append('\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    new_source = "".join(kept).rstrip() + "".join(tail)
    if dry_run:
        return "would_add", "would wrap top-level code in main()"
    try:
        path.write_text(new_source, encoding="utf-8")
    except OSError as exc:
        return "error", f"write failed: {exc}"
    return "added", "wrapped top-level code in main()"


def insert_main_template(source: str) -> str:
    if "def main(" in source:
        return source
    lines = source.split("\n")
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            insert_at = i + 1
        elif stripped and insert_at == 0:
            insert_at = 0
    lines.insert(insert_at, MAIN_TEMPLATE)
    return "\n".join(lines)


def append_main_guard(source: str) -> str:
    if MAIN_GUARD_RE.search(source):
        return source
    return source.rstrip() + GUARD_TEMPLATE


def fix_with_template(path: Path, *, dry_run: bool = False) -> tuple[str, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "error", f"read failed: {exc}"
    if MAIN_GUARD_RE.search(source):
        return "skipped", "already has guard"
    new_source = append_main_guard(insert_main_template(source))
    if dry_run:
        return "would_add", "would add guard"
    try:
        path.write_text(new_source, encoding="utf-8")
    except OSError as exc:
        return "error", f"write failed: {exc}"
    return "added", "added guard successfully"


def discover_files(paths: Sequence[str], excludes: frozenset[str]) -> list[Path]:
    roots = [Path(p) for p in paths] if paths else [Path()]
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            for candidate in root.rglob("*.py"):
                if not candidate.is_file():
                    continue
                if excludes.intersection(candidate.parts):
                    continue
                found.append(candidate)
        elif root.suffix == ".py" and root.is_file():
            found.append(root)
    return sorted(set(found))


def _fix_worker(payload: tuple[str, str, bool, int]) -> tuple[Path, str, str]:
    path_str, strategy, dry_run, indent_size = payload
    path = Path(path_str)
    if strategy == "ast":
        status, message = fix_with_ast(path, indent_size=indent_size, dry_run=dry_run)
    else:
        status, message = fix_with_template(path, dry_run=dry_run)
    return path, status, message


def _check_worker(payload: tuple[str, str]) -> tuple[Path, str, str]:
    path_str, detect = payload
    path = Path(path_str)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return path, "error", f"read failed: {exc}"
    if detect == "regex":
        status = "skipped" if MAIN_GUARD_RE.search(source) else "missing"
        return path, status, ""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return path, "error", f"parse error: {exc}"
    status = "skipped" if has_main_guard_ast(tree) else "missing"
    return path, status, ""


def _resolve_excludes(args: argparse.Namespace) -> frozenset[str]:
    base: list[str] = []
    if not args.no_default_excludes:
        base.extend(DEFAULT_EXCLUDES)
    base.extend(args.exclude)
    return frozenset(base)


def run_check(args: argparse.Namespace) -> int:
    excludes = _resolve_excludes(args)
    files = discover_files(args.paths, excludes)
    if not files:
        logger.warning("No Python files found")
        return 0
    print(f"Found {len(files)} Python files (detect={args.detect}, workers={MAX_WORKERS}, excludes={len(excludes)})")
    missing: list[Path] = []
    skipped = 0
    errors: list[tuple[Path, str]] = []
    payloads = [(str(p), args.detect) for p in files]

    def handle(path: Path, status: str, message: str) -> None:
        nonlocal skipped
        if status == "missing":
            missing.append(path)
        elif status == "skipped":
            skipped += 1
        else:
            errors.append((path, message))

    if MAX_WORKERS > 1 and len(files) > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for path, status, message in pool.map(_check_worker, payloads):
                handle(path, status, message)
    else:
        for payload in payloads:
            handle(*_check_worker(payload))
    print(f"  already had guard: {skipped}")
    for path, message in errors:
        print(f"  ERROR {path}: {message}", file=sys.stderr)
    if missing:
        print(f"Missing main guard in {len(missing)} file(s):")
        for path in sorted(missing):
            print(f"  {path}")
        print("Tip: run `add_main_guard.py fix` to add them")
        return 1
    print("All Python files have the main guard")
    return 0


def _classify(
    status: str,
    path: Path,
    message: str,
    added: list[Path],
    would_add: list[Path],
    skipped: list[Path],
    errors: list[tuple[Path, str]],
) -> None:
    if status == "added":
        added.append(path)
    elif status == "would_add":
        would_add.append(path)
    elif status == "skipped":
        skipped.append(path)
    else:
        errors.append((path, message))


def run_fix(args: argparse.Namespace) -> int:
    excludes = _resolve_excludes(args)
    files = discover_files(args.paths, excludes)
    if not files:
        logger.warning("No Python files found")
        return 0
    print(
        f"Found {len(files)} Python files "
        f"(strategy={args.strategy}, dry_run={args.dry_run}, "
        f"workers={MAX_WORKERS}, indent={args.indent})"
    )
    added: list[Path] = []
    would_add: list[Path] = []
    skipped: list[Path] = []
    errors: list[tuple[Path, str]] = []
    payloads = [(str(p), args.strategy, args.dry_run, args.indent) for p in files]
    if MAX_WORKERS > 1 and len(files) > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for path, status, message in pool.map(_fix_worker, payloads):
                _classify(status, path, message, added, would_add, skipped, errors)
    else:
        for payload in payloads:
            _classify(*_fix_worker(payload), added, would_add, skipped, errors)
    print("Results:")
    print(f"  already had guard: {len(skipped)}")
    if args.dry_run:
        print(f"  would add guard:   {len(would_add)}")
        for path in sorted(would_add):
            print(f"    {path}")
    else:
        print(f"  added guard:       {len(added)}")
    print(f"  errors:            {len(errors)}")
    for path, message in errors:
        print(f"    ERROR {path}: {message}", file=sys.stderr)
    if args.dry_run and would_add:
        print("Dry run complete: no files were modified")
    return 0 if not errors else 2


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="DIR",
        help="Extra directory name to exclude (repeatable)",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Do not apply the built-in exclusion list",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="add_main_guard",
        description=("Detect and add `if __name__ == '__main__':` guards to Python files."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  add_main_guard.py check\n"
            "  add_main_guard.py check src/ tool.py --detect regex\n"
            "  add_main_guard.py fix --strategy ast --dry-run\n"
            "  add_main_guard.py fix --strategy template -a src/\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Report files missing a main guard")
    check.add_argument(
        "paths",
        nargs="*",
        help="Files or directories (default: current directory)",
    )
    check.add_argument(
        "--detect",
        choices=("ast", "regex"),
        default="ast",
        help="Detection backend (default: ast)",
    )
    _add_common_options(check)
    fix = sub.add_parser("fix", help="Add main guards to files missing them")
    fix.add_argument(
        "paths",
        nargs="*",
        help="Files or directories (default: current directory)",
    )
    fix.add_argument(
        "--strategy",
        choices=("ast", "template"),
        default="ast",
        help=(
            "Fix strategy: 'ast' moves top-level code into main(); "
            "'template' inserts a placeholder main() (default: ast)"
        ),
    )
    fix.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing",
    )
    fix.add_argument(
        "--indent",
        type=int,
        default=DEFAULT_INDENT,
        help=f"Indent width for AST strategy (default: {DEFAULT_INDENT})",
    )
    _add_common_options(fix)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args)
    if args.command == "fix":
        return run_fix(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
