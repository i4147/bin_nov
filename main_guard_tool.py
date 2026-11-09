#!/data/data/com.termux/files/usr/bin/env python
"""Detect and add the ``if __name__ == "__main__":`` guards.
The ``check`` subcommand reports the files without a guard and ``fix`` adds one by moving the top-level code into ``main()`` (``--method ast``) or by appending the guard (``--method regex``); ``--dry-run`` and ``--exclude`` are available.
Usage: main_guard_tool {check,fix} [-m ast|regex] [--dry-run] [paths ...] Positional arguments: paths ...
Files or directories (default: current dir) Options: -m, --method Fix method (default: ast) --dry-run Preview changes without modifying files --exclude Extra directories to exclude Examples: $ main_guard_tool check src/ $ main_guard_tool fix --dry-run src/ Command-line script: run ``python main_guard_tool.py`` or the ``main_guard_tool`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Literal


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
MAIN_GUARD_PATTERN: re.Pattern[str] = re.compile(r"""if\s+__name__\s*==\s*["']__main__["']\s*:""")
GUARD_SNIPPET: str = '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
Status = Literal["skipped", "missing", "would_add", "added", "fixed", "error"]
Method = Literal["ast", "regex"]


@dataclass(slots=True)
class FileResult:
    path: Path
    status: Status
    message: str = ""


def is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test: ast.expr = node.test
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def has_main_guard(tree: ast.Module) -> bool:
    return any(is_main_guard(node) for node in tree.body)


def should_wrap(node: ast.stmt) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
        return False
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return False
    return not is_main_guard(node)


def indent_block(block: str, spaces: int = 4) -> str:
    prefix: str = " " * spaces
    return "".join(prefix + line if line.strip() else line for line in block.splitlines(True))


def fix_file_ast(path: Path, dry_run: bool = False) -> FileResult:
    src: str = path.read_text(encoding="utf-8")
    try:
        tree: ast.Module = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return FileResult(path, "error", f"Parse error: {exc}")
    if has_main_guard(tree):
        return FileResult(path, "skipped", "Already has main guard")
    wrap_nodes: list[ast.stmt] = [node for node in tree.body if should_wrap(node)]
    if not wrap_nodes:
        return FileResult(path, "skipped", "Nothing to wrap")
    lines: list[str] = src.splitlines(True)
    keep: list[tuple[int, int]] = []
    main_parts: list[str] = []
    for node in tree.body:
        if node.end_lineno is None:
            return FileResult(path, "error", "Missing end_lineno (AST limitation)")
        segment: str = "".join(lines[node.lineno - 1 : node.end_lineno])
        if should_wrap(node):
            main_parts.append(segment.rstrip() + "\n")
        else:
            keep.append((node.lineno, node.end_lineno))
    new_parts: list[str] = []
    cursor: int = 1
    for start, end in sorted(keep):
        if start > cursor:
            new_parts.append("".join(lines[cursor - 1 : start - 1]))
        new_parts.append("".join(lines[start - 1 : end]))
        cursor = end + 1
    if cursor <= len(lines):
        new_parts.append("".join(lines[cursor - 1 :]))
    main_body: str = "".join(main_parts).rstrip("\n")
    main_fn: str = "\n\ndef main() -> None:\n"
    main_fn += (indent_block(main_body + "\n").rstrip("\n") + "\n") if main_body.strip() else "    pass\n"
    new_src: str = "".join(new_parts).rstrip() + main_fn + GUARD_SNIPPET
    if dry_run:
        return FileResult(path, "would_add", "Would wrap code in main() (dry-run)")
    path.write_text(new_src, encoding="utf-8")
    return FileResult(path, "fixed", "Wrapped code in main() and added guard")


def fix_file_regex(path: Path, dry_run: bool = False) -> FileResult:
    try:
        content: str = path.read_text(encoding="utf-8")
    except OSError as exc:
        return FileResult(path, "error", f"Failed to read: {exc}")
    if MAIN_GUARD_PATTERN.search(content):
        return FileResult(path, "skipped", "Already has guard")
    if dry_run:
        return FileResult(path, "would_add", "Would add guard (dry-run)")
    try:
        path.write_text(content.rstrip() + GUARD_SNIPPET, encoding="utf-8")
    except OSError as exc:
        return FileResult(path, "error", f"Failed to write: {exc}")
    return FileResult(path, "added", "Added guard successfully")


def collect_files(inputs: Sequence[str], excludes: Sequence[str]) -> list[Path]:
    excluded: frozenset[str] = frozenset(excludes)
    roots: list[Path] = [Path(item) for item in inputs] or [Path()]
    out: set[Path] = set()
    for root in roots:
        candidates = root.rglob("*.py") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and path.suffix == ".py" and not excluded.intersection(path.parts):
                out.add(path)
    return sorted(out)


def cmd_check(files: list[Path]) -> int:
    missing: list[Path] = []
    guarded: int = 0
    errors: int = 0
    for path in files:
        try:
            tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError):
            errors += 1
            continue
        if has_main_guard(tree):
            guarded += 1
        else:
            missing.append(path)
    print(f"Summary: {len(missing)} missing, {guarded} with guard, {errors} errors")
    for path in missing:
        print(f"  {path}")
    return 1 if missing else 0


def cmd_fix(files: list[Path], method: Method, dry_run: bool) -> int:
    fix: object = fix_file_ast if method == "ast" else fix_file_regex
    counts: dict[str, int] = {"fixed": 0, "added": 0, "would_add": 0, "skipped": 0, "error": 0}
    for path in files:
        result: FileResult = fix(path, dry_run)
        counts[result.status if result.status in counts else "error"] += 1
        print(f"{path}: {result.status} - {result.message}")
    print(
        f"\nResults: fixed={counts['fixed']} added={counts['added']} "
        f"would_add={counts['would_add']} skipped={counts['skipped']} errors={counts['error']}"
    )
    return 2 if counts["error"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect and add main guards to Python files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  %(prog)s check src/\n  %(prog)s fix --method ast --dry-run",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, help_text in (("check", "Detect missing guards"), ("fix", "Add guards to files")):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("paths", nargs="*", help="Files or directories (default: current dir)")
        child.add_argument("--exclude", nargs="+", default=[], metavar="DIR", help="Extra directories to exclude")
    fix_parser = sub.choices["fix"]
    fix_parser.add_argument("-m", "--method", choices=("ast", "regex"), default="ast", help="Fix method (default: ast)")
    fix_parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    files: list[Path] = collect_files(args.paths, (*DEFAULT_EXCLUDES, *args.exclude))
    if not files:
        print("No Python files found.")
        return 0
    print(f"Found {len(files)} Python file(s)")
    if args.mode == "check":
        return cmd_check(files)
    return cmd_fix(files, method=args.method, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
