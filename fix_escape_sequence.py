#!/data/data/com.termux/files/usr/bin/env python
"""Rewrite string literals that raise ``SyntaxWarning: invalid escape sequence``.
Tokenizes each module, detects backslash sequences that are not valid escapes and either prefixes the literal with ``r`` (when it contains no valid escapes) or doubles the offending backslashes.
``--dry-run`` reports the files without writing, ``--quiet`` hides the per-file lines.
Usage: fix_escape_sequence [options] [paths ...] Positional arguments: paths ...
Options: -n, --dry-run -q, --quiet Examples: $ fix_escape_sequence src/ --dry-run Command-line script: run ``python fix_escape_sequence.py`` or the ``fix_escape_sequence`` command.
The module is importable as well."""

import argparse
import io
import multiprocessing as mp
from pathlib import Path
import sys
import tokenize
import warnings

from dh import MAX_WORKERS, SKIP_DIRS

VALID_ESCAPES: frozenset[str] = frozenset("\\'\"abfnrtv\n\r01234567xNuU")
HAS_FSTRING_TOKENS: bool = hasattr(tokenize, "FSTRING_START")
SELF_PATH: Path = Path(__file__).resolve()
Fix = tuple[tuple[int, int], tuple[int, int], str]
Result = tuple[str, str, int, int]


def _fix_body(body: str) -> str | None:
    out: list[str] = []
    changed = False
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if nxt in VALID_ESCAPES:
                out.append(c)
                out.append(nxt)
            else:
                out.append("\\\\")
                out.append(nxt)
                changed = True
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out) if changed else None


def _fix_string_literal(tok_src: str) -> str | None:
    i = 0
    while i < len(tok_src) and tok_src[i].isalpha():
        i += 1
    prefix, rest = tok_src[:i], tok_src[i:]
    if not rest or rest[0] not in ('"', "'"):
        return None
    q = rest[0]
    quote = q * 3 if rest.startswith(q * 3) else q
    if len(rest) < 2 * len(quote) or not rest.endswith(quote):
        return None
    if "r" in prefix.lower():
        return None
    body = rest[len(quote) : -len(quote)]
    new_body = _fix_body(body)
    if new_body is None:
        return None
    return prefix + quote + new_body + quote


def fix_source(src: str) -> str:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    fixes: list[Fix] = []
    fstring_raw: list[bool] = []
    for tok in tokens:
        t = tok.type
        if t == tokenize.STRING:
            fixed = _fix_string_literal(tok.string)
            if fixed is not None and fixed != tok.string:
                fixes.append((tok.start, tok.end, fixed))
        elif HAS_FSTRING_TOKENS and t == tokenize.FSTRING_START:
            fstring_raw.append("r" in tok.string[:-1].lower())
        elif HAS_FSTRING_TOKENS and t == tokenize.FSTRING_END:
            if fstring_raw:
                fstring_raw.pop()
        elif HAS_FSTRING_TOKENS and t == tokenize.FSTRING_MIDDLE and not (fstring_raw and fstring_raw[-1]):
            nb = _fix_body(tok.string)
            if nb is not None and nb != tok.string:
                fixes.append((tok.start, tok.end, nb))
    if not fixes:
        return src
    lines = src.splitlines(keepends=True)
    for (sr, sc), (er, ec), new in sorted(fixes, key=lambda f: f[0], reverse=True):
        if sr == er:
            line = lines[sr - 1]
            lines[sr - 1] = line[:sc] + new + line[ec:]
        else:
            first = lines[sr - 1][:sc]
            last = lines[er - 1][ec:]
            lines[sr - 1 : er] = [first + new + last]
    return "".join(lines)


def syntax_warnings(src: str, filename: str) -> list[str] | None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            compile(src, filename, "exec")
        except SyntaxError:
            return None
        return [str(w.message) for w in caught if issubclass(w.category, SyntaxWarning)]


def _is_self(p: Path) -> bool:
    try:
        return p.resolve() == SELF_PATH
    except OSError:
        return False


def iter_py_files(root: Path) -> list[Path]:
    root = root.resolve()
    out: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        if p.is_symlink():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if _is_self(p):
            continue
        out.append(p)
    return out


def process_file(path_str: str, dry_run: bool, quiet: bool) -> Result:
    p = Path(path_str)
    if p.is_symlink() or _is_self(p):
        return (path_str, "skipped", 0, 0)
    try:
        src = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (path_str, "skipped", 0, 0)
    warns = syntax_warnings(src, path_str)
    if warns is None:
        return (path_str, "error", 0, 0)
    if not warns:
        return (path_str, "clean", 0, 0)
    fixed = fix_source(src)
    if fixed == src:
        return (path_str, "unfixable", len(warns), len(warns))
    remaining = syntax_warnings(fixed, path_str)
    if remaining is None:
        return (path_str, "error", len(warns), len(warns))
    if not dry_run:
        try:
            p.write_text(fixed, encoding="utf-8")
        except OSError:
            return (path_str, "error", len(warns), len(warns))
    status = "fixed" if not remaining else "partial"
    return (path_str, status, len(warns), len(remaining))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    targets: list[str] = []
    for p in args.paths:
        if p.is_dir():
            targets.extend(str(x) for x in iter_py_files(p))
        elif p.is_file():
            if p.is_symlink() or _is_self(p):
                continue
            targets.append(str(p))
        else:
            print(f"error: not found: {p}", file=sys.stderr)
    if not targets:
        print("no files to process")
        return 0
    tasks: list[tuple[str, bool, bool]] = [(t, args.dry_run, args.quiet) for t in targets]
    with mp.Pool(MAX_WORKERS) as pool:
        results: list[Result] = pool.starmap(process_file, tasks)
    counts: dict[str, int] = {}
    for path, status, before, after in results:
        counts[status] = counts.get(status, 0) + 1
        if args.quiet:
            continue
        if status == "fixed":
            print(f"fixed: {path}")
        elif status == "partial":
            print(f"partial: {path}  ({after} warning(s) remain)", file=sys.stderr)
        elif status == "unfixable":
            print(f"unfixable: {path}  ({before} warning(s))")
        elif status == "error":
            print(f"error: {path}", file=sys.stderr)
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    bad = counts.get("error", 0) + counts.get("partial", 0) + counts.get("unfixable", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
