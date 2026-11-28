#!/data/data/com.termux/files/usr/bin/env python
"""
append_to_files.py — Append the contents of an input file to every file with a given extension in the current directory (non-recursive).

Usage:
    python append_to_files.py notes.txt --ext md python append_to_files.py header.html --ext .html --dry-run python append_to_files.py banner.md --ext md --separator " --- "

Notes:
    * Only files in the current directory are touched (no recursion). * The input file itself is skipped if it happens to match --ext. * Files that end with a newline don't get an extra blank line inserted.

"""

from __future__ import annotations
import argparse
from pathlib import Path
import sys


DEFAULT_SEPARATOR: str = "\n"


def iter_targets(directory: Path, ext: str, exclude: Path) -> list[Path]:
    """Return matching files in `directory` (non-recursive), sorted, minus `exclude`."""
    wanted = ext if ext.startswith(".") else "." + ext
    wanted = wanted.lower()
    exclude_resolved = exclude.resolve()
    out: list[Path] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() != wanted:
            continue
        if p.resolve() == exclude_resolved:
            continue
        out.append(p)
    return out


def append_content(target: Path, payload: str, separator: str, dry_run: bool) -> int:
    """Append `payload` (with separator if needed). Returns bytes written."""
    existing = target.read_text(encoding="utf-8", errors="replace")
    if existing and not existing.endswith("\n"):
        prefix = separator
    else:
        prefix = separator.lstrip("\n") if existing else ""

    addition = prefix + payload if payload else ""
    if not addition:
        return 0

    if dry_run:
        print(f"[dry-run] would append {len(addition)} bytes to {target}")
    else:
        with target.open("a", encoding="utf-8") as f:
            f.write(addition)
        print(f"appended {len(addition)} bytes to {target}")
    return len(addition.encode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="append_to_files.py",
        description="Append an input file's contents to every file with a "
        "given extension in the current directory (non-recursive).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inputfile", type=Path, help="file whose contents will be appended")
    p.add_argument(
        "-e",
        "--ext",
        default="md",
        help="target extension (with or without leading dot)",
    )
    p.add_argument(
        "-d",
        "--dir",
        type=Path,
        default=Path(),
        help="directory to scan (non-recursive)",
    )
    p.add_argument(
        "-s",
        "--separator",
        default=DEFAULT_SEPARATOR,
        help="text inserted between existing content and appended payload (escape sequences are interpreted)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without writing anything",
    )
    p.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="exit non-zero if no target files are found",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.inputfile.is_file():
        print(f"error: input file not found: {args.inputfile}", file=sys.stderr)
        return 1

    if not args.dir.is_dir():
        print(f"error: not a directory: {args.dir}", file=sys.stderr)
        return 1

    payload = args.inputfile.read_text(encoding="utf-8", errors="replace")
    if not payload:
        print(f"error: input file is empty: {args.inputfile}", file=sys.stderr)
        return 1

    separator = args.separator.encode().decode("unicode_escape")

    targets = iter_targets(args.dir, args.ext, args.inputfile)
    if not targets:
        msg = f"no '*{args.ext.lstrip('.')}' files found in {args.dir.resolve()}"
        print(msg, file=sys.stderr)
        return 1 if args.fail_on_empty else 0

    total = 0
    for t in targets:
        try:
            total += append_content(t, payload, separator, args.dry_run)
        except OSError as e:
            print(f"error: could not write {t}: {e}", file=sys.stderr)

    action = "would append" if args.dry_run else "appended"
    print(f"\n{action} to {len(targets)} file(s), {total} byte(s) total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
