#!/data/data/com.termux/files/usr/bin/env python
"""Find duplicate files and replace them with symlinks (reversible)."""

import argparse
from collections import defaultdict
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from xxhash import xxh64

BACKUP_FILE: str = ".symlink_backup.json"
MIN_FILE_SIZE: int = 1


def calculate_file_hash(path: Path, chunk_size=32768) -> str | None:
    if not path.is_file():
        return None
    hasher = xxh64()
    try:
        with Path(path).open("rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as e:
        print(f"[ERROR] Reading {path}: {e}")
        return None


def find_duplicates(directory: str = "."):
    print(f"[INFO] Scanning directory: {Path(directory).resolve()}")
    size_map = defaultdict(list)
    file_count = 0
    skipped_count = 0
    cwd = Path.cwd()
    for path in cwd.rglob("*"):
        if path.is_symlink() or path.is_dir():
            continue
        if ".git" in path.parts:
            continue
        size = path.stat().st_size
        if size < MIN_FILE_SIZE:
            skipped_count += 1
            continue
        size_map[size].append(path)
        file_count += 1
    hash_map = defaultdict(list)
    potential_duplicates = [paths for paths in size_map.values() if len(paths) > 1]
    for paths in potential_duplicates:
        for path in paths:
            file_hash = calculate_file_hash(path)
            if file_hash:
                hash_map[file_hash].append(path)
    return {h: paths for h, paths in hash_map.items() if len(paths) > 1}


def choose_keeper(files: list[Path]):
    return min(files, key=lambda f: (len(str(f)), f))


def create_symlinks(duplicates: dict[str, Any], dry_run=False) -> int:
    backup_data = {"timestamp": datetime.now(tz=UTC).isoformat(), "operations": []}
    total_saved = 0
    symlink_count = 0
    for files in duplicates.values():
        keeper = choose_keeper(files)
        keeper_abs = Path(keeper).resolve()
        for duplicate in files:
            if duplicate == keeper:
                continue
            duplicate_abs = Path(duplicate).resolve()
            get_size = Path(duplicate).stat().st_size
            print(f"  Symlinking: {duplicate} -> {keeper_abs}")
            if not dry_run:
                backup_data["operations"].append({
                    "symlink": str(duplicate_abs),
                    "target": str(keeper_abs),
                    "original_existed": True,
                    "size": get_size,
                })
                try:
                    Path(duplicate).unlink()
                    Path(duplicate_abs).symlink_to(keeper_abs)
                    symlink_count += 1
                    total_saved += get_size
                except OSError as e:
                    print(f"  [ERROR] {e}")
            else:
                print(f"  [DRY RUN] Would replace {duplicate} with symlink to {keeper}")
                symlink_count += 1
                total_saved += get_size
    if not dry_run and symlink_count > 0:
        with Path(BACKUP_FILE).open("w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)
        print(f"\n[INFO] Backup data saved to {BACKUP_FILE}")
    print(f"  Space saved: {total_saved / (1024 * 1024):.2f} MB")
    if dry_run:
        print("[DRY RUN] No changes were made")
    return symlink_count


def reverse_symlinks(backup_file: str = BACKUP_FILE) -> bool:
    if not Path(backup_file).exists():
        print(f"[ERROR] Backup file {backup_file} not found!")
        return False
    with Path(backup_file).open(encoding="utf-8") as f:
        backup_data = json.load(f)
    restored_count = 0
    for op in backup_data["operations"]:
        symlink_path = op["symlink"]
        target_path = op["target"]
        if not Path(symlink_path).is_symlink():
            continue
        if not Path(target_path).exists():
            continue
        try:
            Path(symlink_path).unlink()
            import shutil

            shutil.copy2(target_path, symlink_path)
            restored_count += 1
        except OSError as e:
            print(f"[ERROR] Restoring {symlink_path}: {e}")
    backup_renamed = f"{backup_file}.restored.{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}"
    Path(backup_file).rename(backup_renamed)
    print(f"[INFO] Backup file renamed to: {backup_renamed}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Find duplicate files and replace with symlinks (reversible)")
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument("--reverse", action="store_true", help="Reverse previous symlinking operation")
    parser.add_argument(
        "--backup-file",
        default=BACKUP_FILE,
        help=f"Backup file path (default: {BACKUP_FILE})",
    )
    args = parser.parse_args()
    if args.reverse:
        reverse_symlinks(args.backup_file)
    else:
        duplicates = find_duplicates(args.directory)
        if not duplicates:
            print("\n[INFO] No duplicates found!")
            return
        print(f"\n[INFO] Found {len(duplicates)} groups of duplicates")
        print(f"[INFO] Total duplicate files: {sum(len(files) - 1 for files in duplicates.values())}")
        if args.dry_run:
            print("\n[INFO] [DRY RUN MODE - No changes will be made]")
        create_symlinks(duplicates, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
