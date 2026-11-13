#!/data/data/com.termux/files/usr/bin/env python
"""Reduce each sub-directory of the current directory to its single ``.whl`` file.
Looks for a wheel in the sub-directory or its ``dist/`` folder, moves it up when needed and deletes every other file and folder in that sub-directory, leaving one wheel per project.
Sub-directories without a wheel are skipped.
Usage: justwhl Examples: $ justwhl Deletes the build leftovers of each sub-directory, so review the tree before running it.
"""

from pathlib import Path
import shutil


def find_whl(subdir: Path) -> Path | None:
    for location in (subdir, subdir / "dist"):
        if location.is_dir():
            for whl in location.glob("*.whl"):
                return whl
    return None


def clean_subdir(subdir: Path) -> None:
    whl = find_whl(subdir)
    if whl is None:
        print(f"[skip]  {subdir.name}: no .whl file")
        return
    target = subdir / whl.name
    if whl.parent != subdir:
        shutil.move(str(whl), str(target))
        print(f"[move]  {whl.relative_to(subdir)} -> {target.name}")
    for entry in subdir.iterdir():
        if entry.resolve() == target.resolve():
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        print(f"[rm]    {subdir.name}/{entry.name}")
    print(f"[done]  {subdir.name}: kept {target.name}")


def main() -> None:
    cwd = Path.cwd()
    subdirs = [p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not subdirs:
        print("No top-level subdirectories found.")
        return
    for subdir in subdirs:
        clean_subdir(subdir)


if __name__ == "__main__":
    main()
