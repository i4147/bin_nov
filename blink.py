#!/data/data/com.termux/files/usr/bin/env python
"""Remove dangling symlinks under the current directory.
Skips anything inside ``.git`` and reports links that point to a missing target; ``-d`` only lists them (dry run) instead of unlinking.
Usage: blink [-d] Examples: $ blink -d $ blink Runs as a script (``python blink.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


def blink(directory: Path) -> None:
    for root, _, files in directory.walk():
        for f in files:
            fullpath = Path(root) / f
            if ".git" in fullpath.parts:
                continue
            if fullpath.is_symlink() and not fullpath.exists():
                if "-d" not in sys.argv:
                    fullpath.unlink()
                    print(f" - {f} removed.")
                else:
                    print(f" - {f} (rerun without -d to remove")


def main():
    cwd = Path.cwd()
    blink(cwd)


if __name__ == "__main__":
    raise SystemExit(main())
