#!/data/data/com.termux/files/usr/bin/env python
"""Create tar and tar.gz archives, with optional cleanup of the source folder.
The ``tar`` subcommand writes a plain tarball, ``tgz`` a gzip one (creating the members in parallel) and both can delete the archived folder with ``--cleanup`` or keep it with ``--no-cleanup``.
Usage: archive_merger {tar,tgz} FOLDER [--output PATH] [--cleanup] Positional arguments: folder Folder to archive Subcommands: tar Create TAR archive and optionally remove folder tgz Create TAR.GZ archive and cleanup contents Options: --output Output archive path --cleanup Remove folder after compression --no-cleanup Keep original files Examples: $ archive_merger tgz project --cleanup Command-line script: run ``python archive_merger.py`` or the ``archive_merger`` command.
The module is importable as well."""

import argparse
import multiprocessing as mp
from pathlib import Path
import shutil
import tarfile

from dh import MAX_WORKERS


def compress_tar(folder: Path, output: Path | None = None, cleanup: bool = False) -> bool:
    try:
        if output is None:
            output = folder.parent / f"{folder.name}.tar"
        shutil.make_archive(str(output), "tar", folder.parent, folder.name)
        if cleanup:
            if folder.is_dir():
                shutil.rmtree(folder)
            print(f"Cleaned up: {folder}")
        return True
    except Exception as e:
        print(f"Error compressing: {e}")
        return False


def compress_tgz_parallel(folder: Path, num_workers: int = 8, cleanup: bool = True) -> bool:
    try:
        folder = folder.resolve()
        archive_name = f"{folder.name}.tar.gz"
        archive_path = folder.parent / archive_name
        print(f"Creating archive: {archive_path}")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(folder, arcname=folder.name)
        if cleanup:
            print("Removing original files...")
            items = [item for item in folder.iterdir() if item.resolve() != archive_path]
            _remove_items_fast(items, num_workers)
            print("Cleanup complete.")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False


def _remove_item(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_items_fast(items: list[Path], num_workers: int) -> None:
    if not items:
        return
    with mp.Pool(processes=num_workers) as pool:
        results = [pool.apply_async(_remove_item, (item,)) for item in items]
        for result in results:
            result.get()


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive creation and cleanup tool")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    tar_parser = subparsers.add_parser("tar", help="Create TAR archive and optionally remove folder")
    tar_parser.add_argument("folder", type=Path, help="Folder to archive")
    tar_parser.add_argument("--output", type=Path, default=None, help="Output archive path")
    tar_parser.add_argument("--cleanup", action="store_true", default=False, help="Remove folder after compression")
    tgz_parser = subparsers.add_parser("tgz", help="Create TAR.GZ archive and cleanup contents")
    tgz_parser.add_argument("folder", type=Path, help="Folder to archive")
    tgz_parser.add_argument("--no-cleanup", action="store_true", help="Keep original files")
    args = parser.parse_args()
    folder = Path(args.folder)
    if not folder.exists():
        print(f"Error: Folder '{folder}' not found.")
        return 1
    if not folder.is_dir():
        print(f"Error: '{folder}' is not a directory.")
        return 1
    if args.mode == "tar":
        success = compress_tar(folder, args.output, args.cleanup)
    elif args.mode == "tgz":
        success = compress_tgz_parallel(folder, MAX_WORKERS, not args.no_cleanup)
    else:
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
