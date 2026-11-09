#!/data/data/com.termux/files/usr/bin/env python
"""Archive a directory into ``.tar`` or ``.tar.gz`` and optionally delete what was archived.
The ``tar`` and ``tgz`` subcommands both take a folder and write an archive next to it (or to ``--output``).
``--keep`` keeps the sources, while ``--cleanup-scope`` decides whether the folder itself or only its contents are removed afterwards.
Usage: archive_dir {tar,tgz} FOLDER [options] Positional arguments: folder Subcommands: tar tgz Options: --output --keep --quiet --cleanup-scope Examples: $ archive_dir tgz photos --output photos.tar.gz Command-line script: run ``python archive_dir.py`` or the ``archive_dir`` command.
The module is importable as well."""

import argparse
import multiprocessing as mp
from pathlib import Path
import shutil
import sys
import tarfile
from typing import Sequence

from dh import MAX_WORKERS


def positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        msg = "value must be >= 1"
        raise argparse.ArgumentTypeError(msg)
    return ivalue


def resolve_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        msg = f"Error: Folder '{resolved}' not found."
        raise FileNotFoundError(msg)
    if not resolved.is_dir():
        msg = f"Error: '{resolved}' is not a directory."
        raise NotADirectoryError(msg)
    return resolved


def default_tar_output(source: Path) -> Path:
    return source.parent / f"{source.name}.tar"


def default_tgz_output(source: Path) -> Path:
    return source.parent / f"{source.name}.tar.gz"


def create_archive(source: Path, output: Path, mode: str) -> None:
    with tarfile.open(output, mode) as tar:
        tar.add(source, arcname=source.name)


def remove_path(path: Path, *, safe_symlinks: bool = False, quiet: bool = False) -> None:
    try:
        if path.is_file():
            path.unlink()
            if not quiet:
                print(f"Removed file:{path}")
        elif path.is_dir():
            if safe_symlinks and path.is_symlink():
                path.unlink()
                if not quiet:
                    print(f"Removed symlink:{path}")
            else:
                shutil.rmtree(path)
                if not quiet:
                    print(f"Removed directory:{path}")
    except Exception as e:
        print(f"Error removing '{path}':{e}", file=sys.stderr)


def _remove_path_worker(path_str: str) -> None:
    remove_path(Path(path_str), safe_symlinks=True, quiet=True)


def cleanup_entries_parallel(entries: Sequence[Path], workers: int) -> None:
    if not entries:
        return
    with mp.Pool(processes=workers) as pool:
        results = [pool.apply_async(_remove_path_worker, (str(p),)) for p in entries]
        for result in results:
            result.get()


def cleanup_contents(source: Path, output: Path, workers: int) -> None:
    output_resolved = output.resolve()
    entries = [p for p in source.iterdir() if p.resolve() != output_resolved]
    cleanup_entries_parallel(entries, workers)


def run_tar(args: argparse.Namespace) -> int:
    source = resolve_directory(args.folder)
    output = args.output or default_tar_output(source)
    output = output.expanduser().resolve()
    print(f"Creating archive:{output}")
    create_archive(source, output, "w")
    print("Archive created.")
    if not args.keep:
        print("Removing original folder...")
        remove_path(source, safe_symlinks=False, quiet=args.quiet)
    return 0


def run_tgz(args: argparse.Namespace) -> int:
    source = resolve_directory(args.folder)
    output = args.output or default_tgz_output(source)
    output = output.expanduser().resolve()
    print(f"Creating archive:{output}")
    create_archive(source, output, "w:gz")
    print("Archive created. Removing original files...")
    if not args.keep:
        if args.cleanup_scope == "folder":
            remove_path(source, safe_symlinks=False, quiet=True)
        elif args.cleanup_scope == "contents":
            cleanup_contents(source, output, MAX_WORKERS)
    print("Cleanup complete.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="folder_archiver.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    tar_parser = subparsers.add_parser("tar")
    tar_parser.add_argument("folder", type=Path)
    tar_parser.add_argument("--output", type=Path, default=None)
    tar_parser.add_argument("--keep", action="store_true")
    tar_parser.add_argument("--quiet", action="store_true")
    tar_parser.set_defaults(func=run_tar)
    tgz_parser = subparsers.add_parser("tgz")
    tgz_parser.add_argument("folder", type=Path, nargs="?", default=Path.cwd())
    tgz_parser.add_argument("--output", type=Path, default=None)
    tgz_parser.add_argument("--keep", action="store_true")
    tgz_parser.add_argument(
        "--cleanup-scope",
        choices=("contents", "folder", "none"),
        default="contents",
    )
    tgz_parser.set_defaults(func=run_tgz)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as e:
        print(f"Error:{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
