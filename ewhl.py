#!/data/data/com.termux/files/usr/bin/env python
"""Move the empty wheels (only ``dist-info``, no code) into a subdirectory.
Opens each ``.whl``, checks whether it holds any ``.py`` or ``.so`` member and moves the code-less ones into ``empty_wheels`` (``-d`` renames it), printing each move.
Usage: ewhl [directory] [-d DEST] Positional arguments: directory Directory containing .whl files (default: current directory) Options: -d, --dest Destination subdirectory name (default: 'empty_wheels') -v, --verbose Show detailed output Examples: $ ewhl ~/wheels Command-line script: run ``python ewhl.py`` or the ``ewhl`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import shutil
import zipfile


def is_empty_wheel(wheel_path: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zip_ref:
            all_files = zip_ref.namelist()
            has_py_files = any(file.endswith(".py") for file in all_files)
            has_code_dirs = any(
                not (file.startswith(("dist-info/", "__pycache__/")))
                and not file.endswith("/")
                and not file.endswith(".dist-info/")
                for file in all_files
            )
            return not (has_py_files or has_code_dirs)
    except zipfile.BadZipFile:
        print(f"Error: {wheel_path} is not a valid zip file")
        return False
    except Exception as e:
        print(f"Error reading {wheel_path}: {e}")
        return False


def move_empty_wheels(source_dir: Path, dest_dir_name: str = "empty_wheels") -> None:
    source_path = Path(source_dir)
    dest_path = source_path / dest_dir_name
    dest_path.mkdir(exist_ok=True)
    wheel_files = list(source_path.glob("*.whl"))
    if not wheel_files:
        print(f"No .whl files found in {source_dir}")
        return
    print(f"Found {len(wheel_files)} wheel files to check")
    empty_wheels = []
    valid_wheels = []
    for wheel_file in wheel_files:
        print(f"Checking {wheel_file.name}...", end=" ")
        if is_empty_wheel(wheel_file):
            print("EMPTY")
            empty_wheels.append(wheel_file)
        else:
            print("OK")
            valid_wheels.append(wheel_file)
    if not empty_wheels:
        print("\nNo empty wheels found!")
        return
    print(f"\nFound {len(empty_wheels)} empty wheel(s)")
    for wheel_file in empty_wheels:
        dest_file = dest_path / wheel_file.name
        if dest_file.exists():
            counter = 1
            while dest_file.exists():
                dest_file = dest_path / f"{wheel_file.stem}_{counter}{wheel_file.suffix}"
                counter += 1
        shutil.move(str(wheel_file), str(dest_file))
        print(f"Moved: {wheel_file.name} -> {dest_dir_name}/{dest_file.name}")
    print("\nSummary:")
    print(f"  - Empty wheels moved to: {dest_dir_name}/")
    print(f"  - Valid wheels remaining: {len(valid_wheels)}")
    print(f"  - Total checked: {len(wheel_files)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move empty .whl files (only dist-info, no Python code) to a subdirectory"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    parser.add_argument(
        "-d",
        "--dest",
        default="empty_wheels",
        help="Destination subdirectory name (default: 'empty_wheels')",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed output")
    args = parser.parse_args()
    directory_path = Path(args.directory)
    if not directory_path.exists():
        print(f"Error: Directory '{args.directory}' does not exist")
        return
    move_empty_wheels(args.directory, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())
