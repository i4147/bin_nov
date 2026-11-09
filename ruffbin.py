#!/data/data/com.termux/files/usr/bin/env python
"""Run ``ruff format`` on the extension-less scripts below the current directory.
Walks the tree, skips symlinks and ``.git`` and formats every file without a suffix (the usual Python scripts in a ``bin`` folder) with ruff.
Usage: ruffbin Examples: $ ruffbin Runs as a script (``python ruffbin.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path

from dh import runcmd


def process_file(path: Path) -> None:
    try:
        runcmd(["ruff", "format", str(path)], show_output=True)
    except:
        return


if __name__ == "__main__":
    cwd = Path.cwd()
    for r, d, files in cwd.walk():
        for file in files:
            path = r / file
            if path.is_symlink():
                continue
            if ".git" in path.parts:
                continue
            if path.is_file() and not path.suffix:
                process_file(path)
