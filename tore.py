#!/data/data/com.termux/files/usr/bin/env python
"""Move a directory into ``~/repos``.
Resolves the given path, moves it into ``~/repos`` under the same name and appends a numeric suffix when that name is taken.
Usage: tore PATH Examples: $ tore ~/Downloads/project Runs as a script (``python tore.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil
import sys

from dh import unique_path


if __name__ == "__main__":
    src = Path(sys.argv[1]).resolve()
    cwd = Path.home() / "repos"
    dst = cwd / src.name
    if dst.exists():
        dst = unique_path(dst)
    shutil.move(str(src), str(dst))
    print(f"{src.name} --> {dst.name}")
