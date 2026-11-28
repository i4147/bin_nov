#!/data/data/com.termux/files/usr/bin/env python
"""Trim each line at a target character.

Keeps the part of every line before (before) or after (after) the first occurrence of the target character, leaving other lines unchanged; edits files in place, with a .bak backup option.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from dh import atomic_write


def read_lines(
    path: Path,
    encoding: str = "utf-8",
) -> Iterator[str]:
    """Yield lines from a file."""
    with path.open(encoding=encoding) as file:
        yield from file


def process_file(
    path: Path,
    target_char: str,
    mode: str,
    *,
    encoding: str = "utf-8",
    strip_whitespace: bool = True,
    keep_empty: bool = False,
) -> str:
    """
    Process file contents.

    before:
        Keep everything before the first target character.

    after:
        Keep everything after the first target character.
    """
    result: list[str] = []

    for line in read_lines(path, encoding):
        stripped = line.strip() if strip_whitespace else line.rstrip("\r\n")

        if target_char in stripped:
            index = stripped.index(target_char)

            if mode == "before":
                cleaned = stripped[:index]
            else:
                cleaned = stripped[index + 1 :]

        else:
            cleaned = stripped

        if cleaned or keep_empty:
            result.append(cleaned)

    return "\n".join(result)


def create_backup(path: Path) -> Path:
    """Create a backup alongside the original file."""
    backup_path = path.with_name(f"{path.name}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def build_parser() -> argparse.ArgumentParser:
    """Build command-line argument parser."""
    parser = argparse.ArgumentParser(
        description=("Remove content before or after a target character on each line."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "file",
        type=Path,
        help="File to process",
    )

    parser.add_argument(
        "target_char",
        help="Character used as the processing boundary",
    )

    parser.add_argument(
        "-m",
        "--mode",
        choices=("before", "after"),
        default="before",
        help="Keep content before or after the target character",
    )

    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Apply changes directly to the original file",
    )

    parser.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="Create a .bak backup before applying changes",
    )

    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational messages",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether changes are necessary (exit 1 if changed)",
    )

    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding",
    )

    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Preserve empty lines",
    )

    parser.add_argument(
        "--no-strip",
        action="store_true",
        help="Preserve leading and trailing whitespace",
    )

    return parser


def main() -> int:
    """Application entry point."""
    parser = build_parser()
    args = parser.parse_args()

    file_name: Path = args.file
    target_char: str = args.target_char

    if len(target_char) != 1:
        parser.error("target_char must be exactly one character")

    if args.apply and args.check:
        parser.error("--apply and --check cannot be combined")

    if args.backup and not args.apply:
        parser.error("--backup requires --apply")

    if not file_name.is_file():
        parser.error(f"Not a regular file: {file_name}")

    try:
        original_content = file_name.read_text(
            encoding=args.encoding,
        )

        modified_content = process_file(
            file_name,
            target_char,
            args.mode,
            encoding=args.encoding,
            strip_whitespace=not args.no_strip,
            keep_empty=args.keep_empty,
        )

        changed = original_content != modified_content

        # Check mode.
        if args.check:
            if changed:
                if not args.quiet:
                    print(f"[CHECK] Changes required: {file_name}")
                return 1

            if not args.quiet:
                print(f"[CHECK] No changes required: {file_name}")

            return 0

        # Default: Dry-run.
        if not args.apply:
            if not args.quiet:
                print(f"[DRY-RUN] Preview: {file_name} (mode: {args.mode})")
                print("-" * 60)

            print(modified_content)

            if not args.quiet:
                print("-" * 60)
                print("No files were modified.")
                print("Use --apply to write changes.")

            return 0

        # Avoid unnecessary writes.
        if not changed:
            if not args.quiet:
                print(f"[UNCHANGED] {file_name}")
            return 0

        # Optional backup.
        if args.backup:
            backup_path = create_backup(file_name)

            if not args.quiet:
                print(f"[BACKUP] Created: {backup_path}")

        # Apply modifications.
        atomic_write(
            file_name,
            modified_content,
            encoding=args.encoding,
        )

        if not args.quiet:
            print(f"[APPLIED] Changes written to: {file_name}")

        return 0

    except (OSError, UnicodeError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
