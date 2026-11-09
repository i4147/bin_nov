#!/data/data/com.termux/files/usr/bin/env python
"""Move files (or glob matches) into ``~/isaac/may/scripts``.
Same argument style as :doc:`tolo`: file paths, shell globs and ``-f LISTFILE`` lists, with unique names for the collisions, moved into the personal scripts directory.
Usage: tosc [-f LISTFILE] [files ...] Examples: $ tosc tool.py Runs as a script (``python tosc.py``) in the directory it should act on; the module can also be imported."""

import glob
from pathlib import Path
import shutil
import sys

from dh import unique_path


h = Path.home() / "isaac" / "may" / "scripts"


def i(a):
    g = Path(a)
    if g.exists():
        return [g]
    k = glob.glob(a, recursive=True)
    return [Path(f) for f in k if Path(f).is_file()]


def j():
    args = sys.argv[1:]
    if not args:
        print(f"usage: {Path(sys.argv[0]).name} [-f LISTFILE] [FILE ...]", file=sys.stderr)
        raise SystemExit(1)
    patterns = []
    listfile = None
    k = 0
    while k < len(args):
        a = args[k]
        if a == "-f":
            if k + 1 >= len(args):
                print("-f requires an argument", file=sys.stderr)
                raise SystemExit(1)
            listfile = args[k + 1]
            k += 2
            continue
        patterns.append(a)
        k += 1
    if listfile is not None:
        try:
            with open(listfile) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except OSError as e:
            print(f"cannot read {listfile}: {e}", file=sys.stderr)
            raise SystemExit(1)
    if not patterns:
        print("no files to move", file=sys.stderr)
        raise SystemExit(1)
    d = []
    for a in patterns:
        e = i(a)
        if not e:
            print(f"no matches: {a}", file=sys.stderr)
            continue
        d.extend(e)
    for c in d:
        b = h / c.name
        if b.exists():
            b = unique_path(b)
        shutil.move(str(c), str(b))
        print(f"{c.name} --> {b.name}")


if __name__ == "__main__":
    raise SystemExit(j())
