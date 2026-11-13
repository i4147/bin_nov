#!/data/data/com.termux/files/usr/bin/env python
from pathlib import Path
import mmap
import os
import re
import tokenize
from typing import Iterator

from dh import SKIP_DIRS

SIZE_THRESHOLD: int = 1 << 20
_SKIP_IN_LOOKAHEAD: frozenset[int] = frozenset({
    tokenize.NL,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.COMMENT,
    tokenize.ENCODING,
})
_NON_PRINT_NAME_KEYWORDS: frozenset[str] = frozenset({
    "and",
    "or",
    "not",
    "in",
    "is",
    "if",
    "else",
    "elif",
    "for",
    "while",
    "return",
    "yield",
    "lambda",
    "assert",
    "raise",
    "try",
    "except",
    "finally",
    "with",
    "as",
    "class",
    "def",
    "from",
    "import",
    "pass",
    "break",
    "continue",
    "del",
    "global",
    "nonlocal",
    "await",
    "async",
    "True",
    "False",
    "None",
})
_RICH_PRINT_RE: re.Pattern[str] = re.compile(r"^\s*from\s+rich\s+import\s+print\b", re.MULTILINE)
_AUTOFIX_RE: re.Pattern[str] = re.compile(r"([ \t]*)print(?:[ \t]+(.*?))?(\r?\n)?$")


def iter_files(path: str | Path, ext: list[str] | None = None) -> Iterator[Path]:
    root = Path(path)
    if root.is_file():
        if ext is None or root.suffix in ext:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            if ext is None or p.suffix in ext:
                yield p


def _read_text(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _iter_tokens(path: Path) -> list[tokenize.TokenInfo] | None:
    try:
        if path.stat().st_size > SIZE_THRESHOLD:
            with path.open("rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    return list(tokenize.tokenize(mm.readline))
        with path.open("rb") as f:
            return list(tokenize.tokenize(f.readline))
    except (OSError, tokenize.TokenError, SyntaxError):
        return None


def _next_significant(tokens: list[tokenize.TokenInfo], i: int) -> int | None:
    j = i + 1
    while j < len(tokens) and tokens[j].type in _SKIP_IN_LOOKAHEAD:
        j += 1
    return j if j < len(tokens) else None


def _is_print_arg(tok: tokenize.TokenInfo) -> bool:
    if tok.type in (tokenize.STRING, tokenize.NUMBER):
        return True
    return tok.type == tokenize.NAME and tok.string not in _NON_PRINT_NAME_KEYWORDS


def find_print_statements(path: Path) -> list[tuple[int, str]]:
    text = _read_text(path)
    if text is None or _RICH_PRINT_RE.search(text):
        return []
    tokens = _iter_tokens(path)
    if tokens is None:
        return []
    lines = text.splitlines()
    findings: list[tuple[int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string != "print":
            continue
        row, col = tok.start
        if row - 1 >= len(lines):
            continue
        line = lines[row - 1]
        if line[:col].strip():
            continue
        n = _next_significant(tokens, i)
        if n is None or tokens[n].type == tokenize.NEWLINE:
            findings.append((row, line))
            continue
        if _is_print_arg(tokens[n]):
            findings.append((row, line))
    return findings


def autofix_file(path: Path, findings: list[tuple[int, str]]) -> bool:
    text = _read_text(path)
    if text is None:
        return False
    lines = text.splitlines(keepends=True)
    changed = False
    for row, _ in findings:
        if row - 1 >= len(lines):
            continue
        m = _AUTOFIX_RE.match(lines[row - 1])
        if not m:
            continue
        indent = m.group(1)
        rest = m.group(2) or ""
        nl = m.group(3) or ""
        lines[row - 1] = f"{indent}print({rest}){nl}"
        changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def process_file(path: Path, autofix: bool = False) -> str | None:
    findings = find_print_statements(path)
    if not findings:
        return None
    row, line = findings[0]
    if autofix:
        if autofix_file(path, findings):
            return f"{path} (fixed)\n  Line {row}: {line.strip()}"
        return f"{path} (could not fix)\n  Line {row}: {line.strip()}"
    return f"{path}\n  Line {row}: {line.strip()}"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Detect and fix Python 2 print statements")
    parser.add_argument("path", nargs="?", default=".", help="Path to file or directory to scan")
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically fix print statements",
    )
    args = parser.parse_args()

    found = False
    any_files = False
    for f in iter_files(args.path, ext=[".py"]):
        any_files = True
        result = process_file(f, autofix=args.autofix)
        if result:
            print(result)
            found = True

    if not any_files:
        print("No Python files found.")
        return
    if not found:
        print("No Python 2 print statements found.")
    elif args.autofix:
        print("\n✓ Files with issues have been automatically fixed.")
    else:
        print("\nRun with --autofix to automatically fix these issues.")


if __name__ == "__main__":
    raise SystemExit(main())
