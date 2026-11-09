#!/data/data/com.termux/files/usr/bin/env python
"""Group the subdirectories of the current directory into N similarly sized parts.
Measures each subdirectory, sorts them by size, packs them greedily into ``-n`` balanced ``group_*`` folders and moves them there, printing the resulting distribution.
Usage: group_by_size [-n PARTS] Options: -n, --parts Number of parts/groups to create (default: 3) Examples: $ group_by_size -n 4 Command-line script: run ``python group_by_size.py`` or the ``group_by_size`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import shutil


def get_directory_size(path: Path) -> int:
    total_size = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total_size += entry.stat().st_size
                except (PermissionError, FileNotFoundError):
                    continue
    except (PermissionError, FileNotFoundError):
        pass
    return total_size


def folderize_directories(n_parts: int) -> None:
    current_dir = Path()
    subdirs = [p for p in current_dir.iterdir() if p.is_dir() and not p.name.startswith("group_")]
    if not subdirs:
        print("No subdirectories found in the current directory.")
        return
    print(f"Found {len(subdirs)} subdirectories. Calculating sizes...")
    dir_sizes = {}
    for subdir in subdirs:
        size = get_directory_size(subdir)
        dir_sizes[subdir] = size
        print(f" - {subdir.name}: {size / (1024 * 1024):.2f} MB")
    sorted_subdirs = sorted(subdirs, key=lambda x: dir_sizes[x], reverse=True)
    bins = [{"size": 0, "items": []} for _ in range(n_parts)]
    for subdir in sorted_subdirs:
        min_bin = min(bins, key=lambda b: b["size"])
        min_bin["items"].append(subdir)
        min_bin["size"] += dir_sizes[subdir]
    for i, b in enumerate(bins, start=1):
        group_name = f"group_{i}"
        group_path = current_dir / group_name
        group_path.mkdir(exist_ok=True)
        print(f"\n--- {group_name} (Total Size: {b['size'] / (1024 * 1024):.2f} MB) ---")
        for subdir in b["items"]:
            dest_path = group_path / subdir.name
            print(f"Moving '{subdir.name}' -> '{group_name}/'")
            shutil.move(str(subdir), str(dest_path))
    print("\nFolderization complete successfully!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Group top-level subdirectories into N parts by similar size.")
    parser.add_argument(
        "-n",
        "--parts",
        type=int,
        default=3,
        help="Number of parts/groups to create (default: 3)",
    )
    args = parser.parse_args()
    folderize_directories(args.parts)
