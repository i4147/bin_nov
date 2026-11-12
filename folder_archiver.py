#!/data/data/com.termux/files/usr/bin/env python
"""Archive folders with the ``shutil`` formats and optionally remove them.
The ``create`` subcommand packs each given folder as tar, tar.gz, tar.bz2, tar.xz or zip (``-f``), names the archive after the folder, deletes the source with ``--cleanup`` and previews with ``--dry-run``.
Usage: folder_archiver create [folders ...] [-f FORMAT] [-o ARCHIVE] [--cleanup] Positional arguments: folders ...
Folder(s) to archive Subcommands: create Archive a folder Options: -f, --format Archive format (default: tar) -o, --output Output archive path (single folder only) --cleanup Delete each folder after archiving (default: keep) -n, --dry-run Only show what would happen Examples: $ folder_archiver create project --format gztar --cleanup Command-line script: run ``python folder_archiver.py`` or the ``folder_archiver`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys

FORMAT_SUFFIX: dict[str, str] = {
    "tar": ".tar",
    "gztar": ".tar.gz",
    "bztar": ".tar.bz2",
    "xztar": ".tar.xz",
    "zip": ".zip",
}


def create_archive(folder: Path, fmt: str, output: Path | None, cleanup: bool, dry_run: bool) -> Path:
    archive: Path = output or folder.parent / f"{folder.name}{FORMAT_SUFFIX[fmt]}"
    if dry_run:
        print(f"Would create {archive} from {folder}" + (" and delete the folder" if cleanup else ""))
        return archive
    base_name: str = str(archive)
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    else:
        base_name = str(archive.with_suffix(""))
    made: str = shutil.make_archive(base_name, fmt, root_dir=folder.parent, base_dir=folder.name)
    print(f"Created: {made}")
    if cleanup:
        shutil.rmtree(folder)
        print(f"Removed: {folder}")
    return Path(made)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive creation and cleanup tool.",
        epilog="Example: %(prog)s create myfolder -f gztar --cleanup",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Archive a folder")
    create.add_argument("folders", nargs="+", type=Path, help="Folder(s) to archive")
    create.add_argument(
        "-f", "--format", choices=sorted(FORMAT_SUFFIX), default="tar", help="Archive format (default: tar)"
    )
    create.add_argument("-o", "--output", type=Path, default=None, help="Output archive path (single folder only)")
    create.add_argument("--cleanup", action="store_true", help="Delete each folder after archiving (default: keep)")
    create.add_argument("-n", "--dry-run", action="store_true", help="Only show what would happen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    if args.output is not None and len(args.folders) != 1:
        print("Error: --output requires exactly one folder.", file=sys.stderr)
        return 2
    failed: int = 0
    for folder in args.folders:
        if not folder.is_dir():
            print(f"Error: not a directory: {folder}", file=sys.stderr)
            failed += 1
            continue
        try:
            create_archive(folder, args.format, args.output, args.cleanup, args.dry_run)
        except OSError as exc:
            print(f"Error archiving {folder}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
