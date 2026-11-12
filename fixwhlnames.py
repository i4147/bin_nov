#!/data/data/com.termux/files/usr/bin/env python
"""Restore the real names of batch-renamed wheel files.
Reads the ``METADATA`` and ``WHEEL`` files inside each ``.whl``, reconstructs the ``name-version-python-abi-platform`` name from the tags and renames the file accordingly; it is a dry run unless ``--apply`` is given, and the originals are backed up in ``whl_backup``.
Usage: fixwhlnames [directory] [--apply] [--info-only] Positional arguments: directory Directory containing .whl files (default: current directory) Options: --apply, -a Actually rename files (dry run by default) --no-backup, -b Skip creating backups (backups are created by default) --info-only, -i Only show extracted info without renaming Examples: $ fixwhlnames ~/wheels --info-only Command-line script: run ``python fixwhlnames.py`` or the ``fixwhlnames`` command.
The module is importable as well."""

import argparse
from email.parser import HeaderParser
from multiprocessing import Pool
from pathlib import Path
import re
import shutil
from typing import Any, Final
import zipfile

from dh import MAX_WORKERS
from loguru import logger

WHEEL_SUFFIX: Final[str] = ".whl"
METADATA_SUFFIX: Final[str] = ".dist-info/METADATA"
WHEEL_METADATA_SUFFIX: Final[str] = ".dist-info/WHEEL"
BACKUP_DIR_NAME: Final[str] = "whl_backup"
TAG_PATTERNS: Final[tuple[str, ...]] = (
    (
        r".*?-.*?-.*?-(py3|py2\.py3|py2|cp[0-9]+)-(none|abi[0-9]+|cp[0-9]+m?)-"
        r"(manylinux[0-9_]+|linux|win_amd64|win32|macosx[0-9_]+)\.whl$"
    ),
    (
        r".*?-.*?-.*?-([a-z0-9]+(?:[\.\-][a-z0-9]+)?)-"
        r"([a-z0-9]+(?:[\.\-][a-z0-9]+)?)-"
        r"([a-z0-9_]+(?:[\.\-][a-z0-9_]+)?)\.whl$"
    ),
)
CP3_PATTERN: Final[re.Pattern[str]] = re.compile(r"cp3[0-9]")
TAG_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"Tag: (.*?)-(.*?)-")
Metadata = dict[str, str]
Tags = tuple[str, str, str]


def extract_metadata_from_wheel(wheel_path: Path) -> Metadata | None:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            metadata_files = [f for f in zf.namelist() if f.endswith(METADATA_SUFFIX)]
            if not metadata_files:
                logger.warning("No METADATA file found in {}", wheel_path.name)
                return None
            with zf.open(metadata_files[0]) as f:
                content = f.read().decode("utf-8", errors="ignore")
            parser = HeaderParser()
            msg = parser.parsestr(content)
            name = msg.get("Name")
            version = msg.get("Version")
            if name and version:
                return {"name": name, "version": version}
            logger.warning("Could not find Name/Version in METADATA of {}", wheel_path.name)
            return None
    except zipfile.BadZipFile:
        logger.error("{} is not a valid zip file", wheel_path.name)
        return None
    except Exception as e:
        logger.error("Error reading {}: {}", wheel_path.name, e)
        return None


def extract_wheel_tags(filename: str) -> Tags | None:
    for pattern in TAG_PATTERNS:
        match = re.search(pattern, filename)
        if match:
            return match.group(1), match.group(2), match.group(3)
    if "cp3" in filename:
        py_match = CP3_PATTERN.search(filename)
        if py_match:
            return py_match.group(0), "none", "any"
    return None


def reconstruct_wheel_name(wheel_path: Path, metadata: Metadata, original_filename: str) -> str | None:
    name = metadata["name"]
    version = metadata["version"]
    tags = extract_wheel_tags(wheel_path.name)
    if tags:
        python_tag, abi_tag, platform_tag = tags
        return f"{name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            wheel_files = [f for f in zf.namelist() if f.endswith(WHEEL_METADATA_SUFFIX)]
            if wheel_files:
                with zf.open(wheel_files[0]) as f:
                    content = f.read().decode("utf-8", errors="ignore")
                    if "Root-Is-Purelib: true" in content:
                        py_match = TAG_LINE_PATTERN.search(content)
                        if py_match:
                            python_tag = py_match.group(1)
                            abi_tag = py_match.group(2) or "none"
                            return f"{name}-{version}-{python_tag}-{abi_tag}-any.whl"
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.error("Error reading WHEEL metadata from {}: {}", wheel_path.name, e)
    logger.warning(
        "Could not determine tags for {}, using generic 'py3-none-any'",
        wheel_path.name,
    )
    return f"{name}-{version}-py3-none-any.whl"


def _process_single_file(path: Path, dry_run: bool, backup_dir: Path | None) -> tuple[bool, str | None]:
    print("[{}] Processing", path.name)
    metadata = extract_metadata_from_wheel(path)
    if not metadata:
        return False, path.name
    proper_name = reconstruct_wheel_name(path, metadata, path.name)
    if not proper_name:
        return False, path.name
    if proper_name == path.name:
        print("Already has correct name: {}", path.name)
        return False, None
    if dry_run:
        print("Would rename to: {}", proper_name)
        return True, None
    if backup_dir is not None:
        backup_path = backup_dir / path.name
        shutil.copy2(path, backup_path)
        print("Backup created: {}", backup_path.name)
    try:
        new_path = path.parent / proper_name
        path.rename(new_path)
        logger.success("Renamed to: {}", proper_name)
        return True, None
    except OSError as e:
        logger.error("Error renaming {}: {}", path.name, e)
        return False, path.name


def fix_whl_files_by_metadata(directory: str = ".", dry_run: bool = True, backup: bool = True) -> tuple[int, list[str]]:
    path = Path(directory)
    whl_files = sorted(path.glob(f"*{WHEEL_SUFFIX}"))
    if not whl_files:
        logger.warning("No .whl files found in {}", directory)
        return 0, []
    print("Found {} .whl files", len(whl_files))
    renamed_count = 0
    failed_files: list[str] = []
    backup_dir: Path | None = None
    if backup and not dry_run:
        backup_dir = path / BACKUP_DIR_NAME
        backup_dir.mkdir(exist_ok=True)
        print("Backups will be saved to: {}", backup_dir)
    for idx, path in enumerate(whl_files, 1):
        print("[{}/{}] Processing: {}", idx, len(whl_files), path.name)
        renamed, failed = _process_single_file(path, dry_run, backup_dir)
        if renamed:
            renamed_count += 1
        if failed is not None:
            failed_files.append(failed)
    print("=" * 40)
    print("SUMMARY:")
    print("Total files: {}", len(whl_files))
    if not dry_run:
        print("Successfully renamed: {}", renamed_count)
        print("Failed: {}", len(failed_files))
        if failed_files:
            print("Failed files: {}", ", ".join(failed_files))
    else:
        print("Would rename: {} files", renamed_count)
        print("Would skip/error: {}", len(failed_files))
    if dry_run and renamed_count > 0:
        print("Dry run complete. Run with --apply to apply changes.")
    return renamed_count, failed_files


def _extract_info_worker(wheel_path: Path) -> tuple[str, Metadata | None, str | None]:
    metadata = extract_metadata_from_wheel(wheel_path)
    if not metadata:
        return wheel_path.name, None, None
    proper_name = reconstruct_wheel_name(wheel_path, metadata, wheel_path.name)
    return wheel_path.name, metadata, proper_name


def batch_fix_with_parallel(directory: str = ".") -> None:
    path = Path(directory)
    whl_files = sorted(path.glob(f"*{WHEEL_SUFFIX}"))
    if not whl_files:
        logger.warning("No .whl files found in {}", directory)
        return
    print("Processing {} files with {} workers...", len(whl_files), MAX_WORKERS)
    results: list[tuple[str, Metadata | None, str | None]] = []
    with Pool(processes=MAX_WORKERS) as pool:
        async_results: list[Any] = [pool.apply_async(_extract_info_worker, (f,)) for f in whl_files]
        for ar in async_results:
            try:
                results.append(ar.get())
            except Exception as e:
                logger.error("Error processing file: {}", e)
    print("Extracted information:")
    for old_name, metadata, proper_name in results:
        if metadata is None:
            logger.warning("{} -> <no metadata>", old_name)
        else:
            print(
                "{} -> {} {} -> {}",
                old_name,
                metadata["name"],
                metadata["version"],
                proper_name,
            )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Fix batch-renamed .whl files by reading METADATA from inside each wheel"),
        epilog=("This is the most accurate method as it extracts the real package name and version."),
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    parser.add_argument(
        "--apply",
        "-a",
        action="store_true",
        help="Actually rename files (dry run by default)",
    )
    parser.add_argument(
        "--no-backup",
        "-b",
        default=True,
        action="store_true",
        help="Skip creating backups (backups are created by default)",
    )
    parser.add_argument(
        "--info-only",
        "-i",
        action="store_true",
        help="Only show extracted info without renaming",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    if args.info_only:
        print("Extracting wheel information (no renaming):")
        batch_fix_with_parallel(args.directory)
    else:
        fix_whl_files_by_metadata(
            directory=args.directory,
            dry_run=not args.apply,
            backup=not args.no_backup,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
