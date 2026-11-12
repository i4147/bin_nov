#!/data/data/com.termux/files/usr/bin/env python
"""Delete every ``__pycache__`` directory below the current directory.
Removes the bytecode caches with :func:`shutil.rmtree` and prints how many directories were deleted and how much space was freed.
Usage: pyco Examples: $ pyco Runs as a script (``python pyco.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import shutil

from dh import fsz, gsz


def clean_pycache(start_dir: Path = Path.cwd()) -> None:
    removed = 0
    sz = 0
    for path in start_dir.rglob("__pycache__"):
        if path.exists():
            sz += gsz(path)
            removed += 1
            shutil.rmtree(str(path))
    if removed:
        print(f"   • Total size freed: {fsz(sz)}")
        print(f"   • dirs removed: {removed}")
    else:
        print("nothing found.")


if __name__ == "__main__":
    cwd = Path.cwd()
    clean_pycache(cwd)
