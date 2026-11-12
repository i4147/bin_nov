#!/data/data/com.termux/files/usr/bin/env python
"""Clean text files with a set of dedicated subcommands.
Subcommands: ``empty-lines`` drops the blank lines, ``invisible`` strips the non-printable characters, ``pattern`` removes a multi-line pattern (read from ``/sdcard/lic`` or given inline), ``lines`` removes the lines matching substrings or regexes, ``header`` removes the ``# Author/# Email/# Time`` header and ``json-fields`` renames the JSON keys to ``field_1``, ``field_2``...
``--dry-run`` and ``--no-backup`` control the writes.
Usage: textclean {empty-lines,invisible,pattern,lines,header,json-fields} [options] [paths ...] Positional arguments: paths ...
Files or directories to process (default: current directory).
path File to clean.
Subcommands: empty-lines Remove blank/whitespace-only lines.
invisible Strip non-printable chars from a single file.
pattern Remove a multi-line pattern (from a file) from many files.
lines Remove lines matching substrings (or regexes).
header Remove a header block matching a regex.
json-fields Rewrite JSON list items so keys become field_1, field_2, ...
Options: --dry-run Do not modify files.
--no-recursive Do not descend into sub-directories.
-v, --verbose --no-backup Do not create <file>.bak before writing.
--pattern-file --inline Use this literal pattern instead of --pattern-file.
--apply, --auto-remove Actually remove the pattern (default: list matches only).
-p, --pattern Substring (or regex with -r).
Repeatable.
-r, --regex Treat patterns as regexes.
--multiline Compile regexes with re.MULTILINE (only with -r).
...
plus 4 more; run ``textclean --help`` for the full list.
Examples: $ textclean empty-lines src/ $ textclean lines -p TODO -p FIXME .
Command-line script: run ``python textclean.py`` or the ``textclean`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import string
import sys
from typing import Any, Callable, Iterable, Iterator, Sequence

from dh import MAX_WORKERS, SKIP_DIRS

DEFAULT_PATTERN_FILE: Path = Path("/sdcard/lic")
DEFAULT_JOBS: int = 8
DEFAULT_HEADER_REGEX: str = r"^# Author\s*:.*\n# Email\s*:.*\n# Time\s*:.*\n\n?"
HISTORICAL_LINE_PATTERNS: tuple[str, ...] = (
    "dist-info",
    ".so",
    ".py",
    ".pth",
    "__",
    ".zip",
)
PRINTABLE_CHARS: frozenset[str] = frozenset(string.printable) | {"\n", "\r", "\t"}
_ANSI: dict[str, str] = {
    "blue": "\033[34m",
    "green": "\033[32m",
    "grey": "\033[90m",
    "red": "\033[31m",
    "reset": "\033[0m",
}


def cprint(msg: str, color: str | None = None, *, end: str = "\n") -> None:
    if color and color in _ANSI and sys.stdout.isatty():
        sys.stdout.write(f"{_ANSI[color]}{msg}{_ANSI['reset']}{end}")
    else:
        sys.stdout.write(f"{msg}{end}")


def is_binary(path: Path, chunk: int = 8192) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(chunk)
    except OSError:
        return True


def iter_files(
    roots: Sequence[Path],
    *,
    recursive: bool = True,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> Iterator[Path]:
    for root in roots:
        root = Path(root)
        if root.is_file() and not root.is_symlink():
            yield root
            continue
        if not root.is_dir():
            print(f"Warning: not a file or directory: {root}", file=sys.stderr)
            continue
        it = root.rglob("*") if recursive else root.glob("*")
        for f in it:
            if not f.is_file() or f.is_symlink():
                continue
            if skip_dirs and any(part in skip_dirs for part in f.parts):
                continue
            yield f


def iter_text_files(
    roots: Sequence[Path],
    *,
    recursive: bool = True,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> Iterator[Path]:
    for f in iter_files(roots, recursive=recursive, skip_dirs=skip_dirs):
        if not is_binary(f):
            yield f


def read_text(path: Path, *, errors: str = "ignore") -> str:
    return path.read_text(encoding="utf-8", errors=errors)


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def cmd_empty_lines(args: argparse.Namespace) -> int:
    roots = args.paths or [Path.cwd()]
    files = list(iter_text_files(roots, recursive=not args.no_recursive))
    total_removed = 0
    for f in files:
        try:
            text = read_text(f, errors="strict")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{f}: skipped ({e})")
            continue
        lines = text.splitlines(keepends=False)
        kept = [ln for ln in lines if ln.strip()]
        removed = len(lines) - len(kept)
        if removed == 0:
            if args.verbose:
                cprint(f"{f.name} | ", end="")
                cprint("NO CHANGE", "grey")
            continue
        total_removed += removed
        if args.dry_run:
            cprint(f"{f.name} | would remove ", end="")
            cprint(str(removed), "blue")
        else:
            write_text(f, "\n".join(kept))
            cprint(f"{f.name} | ", end="")
            cprint(str(removed), "blue")
    if args.dry_run:
        print(f"\nTotal lines that would be removed: {total_removed}")
    return 0


def _find_unprintable(text: str) -> list[tuple[int, int, str, int]]:
    hits: list[tuple[int, int, str, int]] = []
    row, col = 1, 1
    for ch in text:
        if ch not in PRINTABLE_CHARS:
            hits.append((row, col, ch, ord(ch)))
        if ch == "\n":
            row += 1
            col = 1
        else:
            col += 1
    return hits


def cmd_invisible(args: argparse.Namespace) -> int:
    f: Path = args.path
    if not f.is_file():
        print(f"Error: '{f}' is not a file", file=sys.stderr)
        return 1
    if not args.no_backup:
        shutil.copy2(f, Path(str(f) + ".bak"))
    text = read_text(f, errors="ignore")
    hits = _find_unprintable(text)
    if hits:
        print(f"Found {len(hits)} unprintable character(s):")
        for line, col, _ch, code in hits:
            print(f"  Line {line}, Col {col}: char code {code} (0x{code:02X})")
    else:
        print("No unprintable characters found.")
    if args.dry_run:
        return 0
    cleaned = "".join(ch for ch in text if ch in PRINTABLE_CHARS)
    write_text(f, cleaned)
    return 0


def cmd_pattern(args: argparse.Namespace) -> int:
    if args.inline is not None:
        pattern = args.inline
        src_desc = "<inline>"
    else:
        pf: Path = args.pattern_file
        if not pf.exists():
            print(f"Error: pattern file not found: {pf}", file=sys.stderr)
            return 1
        pattern = read_text(pf)
        src_desc = str(pf)
    if not pattern.strip():
        print("Error: pattern is empty", file=sys.stderr)
        return 1
    print(f"Pattern loaded from {src_desc} ({len(pattern)} chars, {len(pattern.splitlines())} lines)")
    roots = args.paths or [Path.cwd()]
    files = list(iter_text_files(roots, recursive=not args.no_recursive))
    if not files:
        print("No text files found to process.")
        return 0
    print(f"Found {len(files)} text file(s) to process")
    apply_changes = args.apply and not args.dry_run
    if not apply_changes:
        print("\nDRY RUN — files that contain the pattern:")
        for f in files:
            try:
                if pattern in read_text(f):
                    print(f"  {f}")
            except OSError:
                continue
        print("\nUse --apply (or --auto-remove) to actually remove the pattern.")
        return 0

    def _process(path: Path) -> tuple[Path, int, int, str | None]:
        try:
            text = read_text(path)
        except OSError as e:
            return path, 0, 0, str(e)
        count = text.count(pattern)
        if count == 0:
            return path, 0, 0, None
        write_text(path, text.replace(pattern, ""))
        return path, count, len(pattern) * count, None

    modified = 0
    total_hits = 0
    total_bytes = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        for path, count, nbytes, err in pool.map(_process, files):
            if err:
                print(f"{path}: ERROR — {err}", file=sys.stderr)
                errors += 1
                continue
            if count:
                modified += 1
                total_hits += count
                total_bytes += nbytes
                print(f"{path}: removed {count} occurrence(s) ({nbytes:,} bytes)")
    print("\n" + "=" * 40)
    print("PROCESSING REPORT")
    print("=" * 40)
    print(f"Files processed : {len(files)}")
    print(f"Files modified  : {modified}")
    print(f"Occurrences     : {total_hits}")
    print(f"Bytes removed   : {total_bytes:,}")
    if errors:
        print(f"Errors          : {errors}")
    return 0


def _build_line_predicate(
    patterns: Sequence[str],
    *,
    regex: bool,
    multiline: bool,
    ignore_case: bool,
) -> Callable[..., Any]:
    if regex:
        flags = 0
        if ignore_case:
            flags |= re.IGNORECASE
        if multiline:
            flags |= re.MULTILINE
        compiled = [re.compile(p, flags) for p in patterns]

        def _pred_regex(line: str) -> bool:
            return any(m.search(line) for m in compiled)

        return _pred_regex
    if ignore_case:
        low = [p.lower() for p in patterns]

        def _pred_lower(line: str) -> bool:
            ll = line.lower()
            return any(p in ll for p in low)

        return _pred_lower

    def _pred_plain(line: str) -> bool:
        return any(p in line for p in patterns)

    return _pred_plain


def cmd_lines(args: argparse.Namespace) -> int:
    if not args.pattern:
        print(
            "Error: at least one -p/--pattern is required.\n"
            "For the old remove_lines_containing_str_from_files.py defaults use:\n"
            "  " + " ".join(f"-p {p!r}" for p in HISTORICAL_LINE_PATTERNS),
            file=sys.stderr,
        )
        return 2
    should_drop = _build_line_predicate(
        args.pattern,
        regex=args.regex,
        multiline=args.multiline,
        ignore_case=args.ignore_case,
    )
    roots = args.paths or [Path.cwd()]
    files = list(iter_text_files(roots, recursive=not args.no_recursive))
    total_removed = 0
    modified_files = 0
    for f in files:
        if args.skip_name and any(s in f.name for s in args.skip_name):
            continue
        try:
            text = read_text(f)
        except OSError as e:
            print(f"{f}: skipped ({e})", file=sys.stderr)
            continue
        lines = text.splitlines(keepends=True)
        kept = [ln for ln in lines if not should_drop(ln)]
        removed = len(lines) - len(kept)
        if removed == 0:
            if args.verbose:
                cprint(f"{f.name} | ", end="")
                cprint("NO CHANGE", "grey")
            continue
        total_removed += removed
        modified_files += 1
        if args.dry_run:
            cprint(f"{f.name} | would remove ", end="")
            cprint(str(removed), "blue")
            print("    sample lines that would be removed:")
            for ln in lines:
                if should_drop(ln):
                    print(f"      - {ln.rstrip()}")
        else:
            write_text(f, "".join(kept))
            cprint(f"{f.name} | ", end="")
            cprint(str(removed), "blue")
    print(f"\n{'Would remove' if args.dry_run else 'Removed'} {total_removed} line(s) from {modified_files} file(s).")
    return 0


def cmd_header(args: argparse.Namespace) -> int:
    try:
        pat = re.compile(args.pattern, re.MULTILINE)
    except re.error as e:
        print(f"Invalid regex: {e}", file=sys.stderr)
        return 2
    roots = args.paths or [Path.cwd()]
    files = list(iter_text_files(roots, recursive=not args.no_recursive))
    if args.ext:
        files = [f for f in files if f.suffix in set(args.ext)]
    modified = 0
    for f in files:
        try:
            text = read_text(f, errors="strict")
        except (OSError, UnicodeDecodeError) as e:
            print(f"Skipped {f}: {e}")
            continue
        new = pat.sub("", text, count=args.count)
        if new == text:
            continue
        modified += 1
        if args.dry_run:
            print(f"Would clean: {f}")
        else:
            write_text(f, new)
            print(f"Cleaned: {f}")
    verb = "would be" if args.dry_run else "were"
    print(f"\nDone. {modified} file(s) {verb} modified.")
    return 0


def cmd_json_fields(args: argparse.Namespace) -> int:
    fn: Path = args.path
    if not fn.is_file():
        print(f"Error: '{fn}' is not a file", file=sys.stderr)
        return 1
    try:
        with fn.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(data, list):
        print("Error: top-level JSON must be a list", file=sys.stderr)
        return 1
    out: list = []
    for item in data:
        if isinstance(item, dict) and len(item) >= 2:
            out.append({f"field_{i + 1}": v for i, v in enumerate(item.values())})
        else:
            out.append(item)
    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    with fn.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"Successfully transformed {fn}")
    return 0


def _add_common_walk_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: current directory).",
    )
    sp.add_argument("--dry-run", action="store_true", help="Do not modify files.")
    sp.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into sub-directories.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="textclean.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p = sub.add_parser("empty-lines", help="Remove blank/whitespace-only lines.")
    _add_common_walk_args(p)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_empty_lines)
    p = sub.add_parser("invisible", help="Strip non-printable chars from a single file.")
    p.add_argument("path", type=Path, help="File to clean.")
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create <file>.bak before writing.",
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_invisible)
    p = sub.add_parser(
        "pattern",
        help="Remove a multi-line pattern (from a file) from many files.",
    )
    _add_common_walk_args(p)
    p.add_argument(
        "--pattern-file",
        type=Path,
        default=DEFAULT_PATTERN_FILE,
        help=f"File whose contents are the pattern (default: {DEFAULT_PATTERN_FILE}).",
    )
    p.add_argument(
        "--inline",
        default=None,
        help="Use this literal pattern instead of --pattern-file.",
    )
    p.add_argument(
        "--apply",
        "--auto-remove",
        dest="apply",
        action="store_true",
        help="Actually remove the pattern (default: list matches only).",
    )
    p.set_defaults(func=cmd_pattern)
    p = sub.add_parser("lines", help="Remove lines matching substrings (or regexes).")
    _add_common_walk_args(p)
    p.add_argument(
        "-p",
        "--pattern",
        action="append",
        default=[],
        help="Substring (or regex with -r). Repeatable.",
    )
    p.add_argument("-r", "--regex", action="store_true", help="Treat patterns as regexes.")
    p.add_argument(
        "--multiline",
        action="store_true",
        help="Compile regexes with re.MULTILINE (only with -r).",
    )
    p.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="Case-insensitive match.",
    )
    p.add_argument(
        "--skip-name",
        action="append",
        default=[],
        help="Skip files whose name contains this substring. Repeatable.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_lines)
    p = sub.add_parser("header", help="Remove a header block matching a regex.")
    _add_common_walk_args(p)
    p.add_argument(
        "-p",
        "--pattern",
        default=DEFAULT_HEADER_REGEX,
        help="Regex for the header block (default: Author/Email/Time).",
    )
    p.add_argument(
        "--ext",
        action="append",
        default=[],
        help="Only process files with this extension (e.g. .py). Repeatable.",
    )
    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="Maximum number of substitutions per file (default: 1).",
    )
    p.set_defaults(func=cmd_header)
    p = sub.add_parser(
        "json-fields",
        help="Rewrite JSON list items so keys become field_1, field_2, ...",
    )
    p.add_argument("path", type=Path, help="JSON file to rewrite.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_json_fields)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
