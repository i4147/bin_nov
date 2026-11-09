#!/data/data/com.termux/files/usr/bin/env python
"""Small Python-source normalizers: brackets, ``is not``, print separators.

Analysis / deduplication
------------------------
- ``ensurebracket.py`` checks bracket balance by counting characters
  (it calls ``Path(path)`` on an undefined name first, and its naive
  counter is fooled by brackets inside strings/comments). The merged
  ``brackets`` command uses ``tokenize`` so only real bracket tokens
  count; use ``--naive`` for the exact original algorithm.
- ``fix_is_not.py`` -> ``is-not``: same tokenize-based rewrite of
  ``is not`` to ``!=`` (note: only safe when comparing values, which
  is why fixing still requires ``--fix``), with ``is`` -> ``==`` left
  alone exactly as in the original.
- ``normalize_print_sep.py`` -> ``print-sep``: rewrites
  ``print("="*60)``-style separator lines to ``print('-'*42)``; its
  ``NamedTuple`` was never actually given fields (it would crash), and
  the ``cprint`` alternative in the regex is preserved.

All commands are dry-run by default; ``--fix``/``-a`` writes files.

Examples
--------
  python3 python_normalize_tool.py brackets src/
  python3 python_normalize_tool.py is-not . --fix
  python3 python_normalize_tool.py print-sep . -a --replacement "print('='*60)"
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tokenize


SEPARATOR_RE: re.Pattern[str] = re.compile(r"""(cprint|print)\s*\(\s*['"](.)['"](\s*\*\s*)(\d+)([^)]*)\)""")
DEFAULT_REPLACEMENT: str = "print('-'*42)"
OPENERS: dict[str, str] = {"(": ")", "[": "]", "{": "}"}
CLOSERS: dict[str, str] = {v: k for k, v in OPENERS.items()}
SKIP_DIRS: frozenset[str] = frozenset({".git", "__pycache__", "venv", ".venv"})


@dataclass(slots=True)
class ScanResult:
    """Per-file outcome shared by all three checks."""

    path: Path
    count: int = 0
    ok: bool = True
    detail: str = ""


def find_py_files(inputs: Sequence[Path]) -> list[Path]:
    """Collect .py files (excluding this script itself)."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    self_path: Path = Path(__file__).resolve()
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob("*.py") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and not SKIP_DIRS.intersection(path.parts) and path.resolve() != self_path:
                found.add(path)
    return sorted(found)


def tokens_of(path: Path) -> list[tokenize.TokenInfo]:
    """Tokenize a Python file."""
    with path.open("rb") as handle:
        return list(tokenize.tokenize(handle.readline))


# --------------------------------------------------------------- brackets
def brackets_balanced_tokens(path: Path) -> bool:
    """Bracket balance over real OP tokens only."""
    stack: list[str] = []
    for tok in tokens_of(path):
        if tok.type != tokenize.OP:
            continue
        if tok.string in OPENERS:
            stack.append(tok.string)
        elif tok.string in CLOSERS and (not stack or stack.pop() != CLOSERS[tok.string]):
            return False
    return not stack


def brackets_balanced_naive(text: str) -> bool:
    """Original character-counting algorithm (strings included)."""
    stack: list[str] = []
    for char in text:
        if char in CLOSERS:
            top: str = stack.pop() if stack else "#"
            if CLOSERS[char] != top:
                return False
        elif char in OPENERS:
            stack.append(char)
    return not stack


def cmd_brackets(args: argparse.Namespace) -> int:
    """List files whose brackets balance (or, with --bad-only, don't)."""
    bad: int = 0
    for path in find_py_files(args.paths):
        try:
            balanced: bool = (
                brackets_balanced_naive(path.read_text(encoding="utf-8", errors="ignore"))
                if args.naive
                else brackets_balanced_tokens(path)
            )
        except (tokenize.TokenError, SyntaxError, OSError) as exc:
            print(f"error: {path}: {exc}")
            bad += 1
            continue
        if balanced and not args.bad_only:
            print(path.name)
        if not balanced:
            bad += 1
            if args.bad_only or args.verbose:
                print(f"UNBALANCED: {path}")
    return 1 if bad else 0


# ----------------------------------------------------------------- is-not
def fix_is_not(path: Path, fix: bool) -> ScanResult:
    """Find (and optionally rewrite) ``is not`` -> ``!=`` via tokens."""
    result: ScanResult = ScanResult(path)
    try:
        tokens: list[tokenize.TokenInfo] = tokens_of(path)
    except (tokenize.TokenError, SyntaxError, OSError) as exc:
        result.ok = False
        result.detail = str(exc)
        return result
    out: list[tokenize.TokenInfo] = []
    lines: list[int] = []
    index: int = 0
    while index < len(tokens):
        tok: tokenize.TokenInfo = tokens[index]
        nxt: tokenize.TokenInfo | None = tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            tok.type == tokenize.NAME
            and tok.string == "is"
            and nxt is not None
            and nxt.type == tokenize.NAME
            and nxt.string == "not"
        ):
            result.count += 1
            lines.append(tok.start[0])
            if fix:
                out.append(tok._replace(type=tokenize.OP, string="!="))
                index += 2
                continue
        out.append(tok)
        index += 1
    result.detail = "lines " + ", ".join(str(n) for n in lines) if lines else ""
    if fix and result.count:
        fixed: bytes = tokenize.untokenize(out)
        path.write_bytes(fixed)
    return result


def cmd_is_not(args: argparse.Namespace) -> int:
    """Report/fix ``is not`` usages."""
    files_total: int = 0
    hits_total: int = 0
    for path in find_py_files(args.paths):
        result: ScanResult = fix_is_not(path, args.fix)
        if not result.ok:
            print(f"error: {path}: {result.detail}")
            continue
        if result.count:
            files_total += 1
            hits_total += result.count
            state: str = "FIXED" if args.fix else "FOUND"
            print(f"[{state}] {path} -> {result.count}x ({result.detail})")
    print(
        f"Summary: {files_total} file(s), {hits_total} instance(s) of 'is not'"
        + ("" if args.fix else " (dry run; add --fix)")
    )
    return 0


# -------------------------------------------------------------- print-sep
def normalize_separators(content: str, replacement: str) -> tuple[str, int]:
    """Replace ``print('='*N)``-style calls; return (new_text, count)."""
    return SEPARATOR_RE.subn(replacement, content)


def cmd_print_sep(args: argparse.Namespace) -> int:
    """Report/fix print-separator calls."""
    files_total: int = 0
    hits_total: int = 0
    for path in find_py_files(args.paths):
        content: str = path.read_text(encoding="utf-8", errors="ignore")
        updated, count = normalize_separators(content, args.replacement)
        if count:
            files_total += 1
            hits_total += count
            print(f"{path}: {count} replacement(s)" + ("" if args.fix else " (dry run)"))
            if args.fix:
                path.write_text(updated, encoding="utf-8")
    print(f"Summary: {files_total} file(s), {hits_total} replacement(s).")
    if hits_total and not args.fix:
        print("Run with --fix (-a) to apply.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Python source normalizers/checks.")
    sub = parser.add_subparsers(dest="command", required=True)
    br = sub.add_parser("brackets", help="Check bracket balance")
    br.add_argument("paths", nargs="*", type=Path)
    br.add_argument("--naive", action="store_true", help="Original char-counting algorithm")
    br.add_argument("--bad-only", action="store_true", help="Only list unbalanced files")
    br.add_argument("-v", "--verbose", action="store_true")
    br.set_defaults(handler=cmd_brackets)
    isn = sub.add_parser("is-not", help="Find/fix 'is not' -> '!='")
    isn.add_argument("paths", nargs="*", type=Path)
    isn.add_argument("-a", "--fix", action="store_true", help="Rewrite files (default: report)")
    isn.set_defaults(handler=cmd_is_not)
    ps = sub.add_parser("print-sep", help="Normalize print('='*N) separators")
    ps.add_argument("paths", nargs="*", type=Path)
    ps.add_argument("--replacement", default=DEFAULT_REPLACEMENT)
    ps.add_argument("-a", "--fix", action="store_true")
    ps.set_defaults(handler=cmd_print_sep)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
