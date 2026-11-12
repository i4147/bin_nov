#!/data/data/com.termux/files/usr/bin/env python
"""Syntax-check files and move/copy the invalid ones aside.
Analysis / deduplication ------------------------ - ``asteval.py`` (AST parse, copies bad files to ``<dir>/error/``) and ``black_check_mp.py`` (AST parse despite the name, moves files into top-level ``ok/``/``error/``) differ only in layout/action: ``--layout per-dir|split`` and ``--action copy|move``.
- ``okerror.py`` is the real ``black --check`` variant -> ``--checker black``.
- ``luaeval.py`` does the same job for Lua via tree-sitter -> ``--language lua`` (optional deps: ``tree-sitter``, ``tree-sitter-lua``).
- Hardcoded pool size 8 -> ``-j/--workers``; ``error``/``ok`` dir names are options.
The private ``dh`` walker is stdlib ``pathlib`` now.
Examples -------- python3 syntax_triage_tool.py triage --dry-run python3 syntax_triage_tool.py triage src/ --layout split --action move python3 syntax_triage_tool.py triage --checker black --layout split python3 syntax_triage_tool.py triage --language lua
"""

import argparse
import ast
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterable
from dh import MAX_WORKERS, SKIP_DIRS


def unique_destination(dest: Path) -> Path:
    """Return *dest*, or ``name_N.suffix`` if it already exists."""
    if not dest.exists():
        return dest
    counter: int = 1
    while True:
        candidate: Path = dest.parent / f"{dest.stem}_{counter}{dest.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def check_python_ast(path: Path) -> tuple[Path, bool, str]:
    """Return (path, ok, detail) using ``ast.parse``."""
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return path, True, ""
    except (SyntaxError, ValueError, UnicodeDecodeError, OSError) as exc:
        return path, False, str(exc)


def check_python_black(path: Path) -> tuple[Path, bool, str]:
    """Return (path, ok, detail) using ``black --check``."""
    proc = subprocess.run(["black", "--check", "-q", str(path)], capture_output=True, text=True)
    detail: str = (proc.stderr or proc.stdout).strip()
    return path, proc.returncode == 0, "" if proc.returncode == 0 else detail


def check_lua(path: Path) -> tuple[Path, bool, str]:
    """Return (path, ok, detail) using tree-sitter-lua."""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_lua
    except ImportError:
        return path, False, "missing deps: pip install tree-sitter tree-sitter-lua"
    parser = Parser(Language(tree_sitter_lua.language()))
    tree = parser.parse(path.read_bytes())
    stack: list[object] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:  # type: ignore[attr-defined]
            return path, False, "tree-sitter reported an ERROR/MISSING node"
        stack.extend(node.children)  # type: ignore[attr-defined]
    return path, True, ""


CHECKERS: dict[str, Any] = {"ast": check_python_ast, "black": check_python_black, "lua": check_lua}


def collect_files(inputs: Sequence[Path], suffix: str, excluded_names: frozenset[str]) -> list[Path]:
    """Collect files with *suffix*, skipping venvs and the output dirs."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    self_path: Path = Path(__file__).resolve()
    skip: frozenset[str] = SKIP_DIRS | excluded_names
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob(f"*{suffix}") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and not skip.intersection(path.parts) and path.resolve() != self_path:
                found.add(path)
    return sorted(found)


def destination_for(path: Path, ok: bool, args: argparse.Namespace) -> Path | None:
    """Return where *path* should go, or None to leave it in place."""
    if args.layout == "per-dir":
        return None if ok else path.parent / args.error_name / path.name
    # split layout: ok/ and error/ in the current directory
    return (Path(args.ok_name) if ok else Path(args.error_name)) / path.name


def cmd_triage(args: argparse.Namespace) -> int:
    """Run the ``triage`` subcommand."""
    suffix: str = ".lua" if args.language == "lua" else ".py"
    checker_name: str = "lua" if args.language == "lua" else args.checker
    files = collect_files(args.paths, suffix, frozenset({args.ok_name, args.error_name}))
    if not files:
        print("No files found to process.")
        return 0
    print(f"Checking {len(files)} file(s) with '{checker_name}'...")
    ok_count: int = 0
    bad_count: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(CHECKERS[checker_name], path) for path in files]
        for future in as_completed(futures):
            path, ok, detail = future.result()
            ok_count, bad_count = ok_count + (1 if ok else 0), bad_count + (0 if ok else 1)
            dest: Path | None = destination_for(path, ok, args)
            if dest is None:
                print(f"OK    {path}")
                continue
            dest = unique_destination(dest)
            if args.dry_run:
                print(f"{'OK' if ok else 'BAD'}  would {args.action}: {path} -> {dest} {detail}".rstrip())
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if args.action == "move":
                shutil.move(str(path), str(dest))
            else:
                shutil.copy2(path, dest)
            print(f"{'OK' if ok else 'BAD'}  {args.action}: {path} -> {dest} {detail}".rstrip())
    print(f"Summary: {ok_count} ok, {bad_count} bad")
    return 1 if bad_count else 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Syntax-check files and sort the invalid ones aside.")
    sub = parser.add_subparsers(dest="command", required=True)
    tri = sub.add_parser("triage", help="Check files and move/copy failures (or all files) aside")
    tri.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: current dir)")
    tri.add_argument("--language", choices=("python", "lua"), default="python")
    tri.add_argument(
        "--checker",
        choices=("ast", "black"),
        default="ast",
        help="Python only: syntax (ast) or formatting (black) check",
    )
    tri.add_argument(
        "--layout",
        choices=("per-dir", "split"),
        default="per-dir",
        help="per-dir: <dir>/error/ for bad files; split: ./ok and ./error for all",
    )
    tri.add_argument("--action", choices=("copy", "move"), default="copy")
    tri.add_argument("--error-name", default="error", help="Error dir name (default: error)")
    tri.add_argument("--ok-name", default="ok", help="OK dir name for split layout (default: ok)")
    tri.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS, help="Workers (default: 8)")
    tri.add_argument("-n", "--dry-run", action="store_true", help="Report only, touch nothing")
    tri.set_defaults(handler=cmd_triage)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
