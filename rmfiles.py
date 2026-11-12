#!/data/data/com.termux/files/usr/bin/env python
"""Delete the top-level files named in a list file.
Reads ``list.txt`` (or the file given as the argument), deletes the matching files of the current directory, ignores entries that point outside it and finally removes the list file itself.
Usage: rmfiles [LIST.txt] Examples: $ rmfiles list.txt Runs as a script (``python rmfiles.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

DEFAULT_LIST: str = "list.txt"


def main() -> int:
    list_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIST
    list_file = Path(list_name)
    if not list_file.is_file():
        print(f"Error: list file not found: {list_file}", file=sys.stderr)
        return 1
    cwd = Path.cwd()
    removed = 0
    missing = 0
    for raw_line in list_file.read_text().splitlines():
        name = raw_line.strip()
        if not name or name.startswith("#"):
            continue
        target = cwd / name
        if target.parent.resolve() != cwd.resolve():
            print(f"skipping (not top-level): {name}", file=sys.stderr)
            continue
        if target.is_file():
            target.unlink()
            print(f"removed: {name}")
            removed += 1
        else:
            print(f"not found: {name}", file=sys.stderr)
            missing += 1
    if list_file.resolve().parent == cwd.resolve():
        list_file.unlink()
        print(f"removed list file: {list_file.name}")
    print(f"\nDone. {removed} removed, {missing} not found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
