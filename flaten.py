#!/data/data/com.termux/files/usr/bin/env python
"""Flatten a directory tree, moving every file into one folder.
Moves all files from the sub-directories into the target directory, renaming on collision so nothing is overwritten, and removes the directories that became empty.
Usage: flaten [SOURCE_DIR] [TARGET_DIR] Examples: $ flaten ~/downloads ~/flat Runs as a script (``python flaten.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import shutil
import sys


def unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def flatten_directory(directory=".") -> None:
    root = Path(directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a valid directory")
        return
    print(f"Flattening directory: {root}")
    files_to_move = [path for path in root.rglob("*") if path.is_file()]
    if not files_to_move:
        print("No files found in subdirectories.")
        return
    print(f"Found {len(files_to_move)} file(s) to move")
    moved_count = 0
    renamed_count = 0
    for path in files_to_move:
        target_path = root / path.name
        if target_path == path:
            continue
        if target_path.exists():
            new_target = unique_target(target_path)
            print(f"Renaming: {path.name} -> {new_target.name} (name already taken)")
            target_path = new_target
            renamed_count += 1
        try:
            shutil.move(str(path), str(target_path))
            print(f"Moved: {path} -> {target_path}")
            moved_count += 1
        except Exception as e:
            print(f"Error moving {path}: {e}")
    print(f"\nMoved {moved_count} file(s), {renamed_count} renamed due to collisions")
    all_dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    removed_dirs = 0
    for subdir in all_dirs:
        try:
            if not any(subdir.iterdir()):
                subdir.rmdir()
                print(f"Removed empty directory: {subdir}")
                removed_dirs += 1
        except Exception as e:
            print(f"Error removing {subdir}: {e}")
    print(f"\nRemoved {removed_dirs} empty directory(ies)")
    print("Flattening complete!")


def main() -> None:
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    flatten_directory(target_dir)


if __name__ == "__main__":
    main()
