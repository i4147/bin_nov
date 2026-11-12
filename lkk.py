#!/data/data/com.termux/files/usr/bin/env python
"""List the files in the current directory whose name contains a substring.
Prints the matching names sorted alphabetically, marking symlinks with their target; ``-s`` also includes symlinks (they are skipped by default).
Usage: lkk [-s] SUBSTRING Examples: $ lkk dup Runs as a script (``python lkk.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    skip_symlinks = True
    search_pattern = None
    for arg in args:
        if arg == "-s":
            skip_symlinks = False
        else:
            search_pattern = arg.strip()
    found = []
    for path in cwd.glob("*"):
        if search_pattern in path.name:
            if skip_symlinks and path.is_symlink():
                continue
            found.append(path)
    for k in sorted(found):
        if k.is_symlink():
            print(f"  - {k.name} -> {k.resolve()}")
        else:
            print(f"  - {k.name}")


if __name__ == "__main__":
    raise SystemExit(main())
