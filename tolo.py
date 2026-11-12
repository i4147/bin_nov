#!/data/data/com.termux/files/usr/bin/env python
"""Move files (or glob matches) into ``~/.local/bin``.
Accepts file paths, shell globs and a ``-f LISTFILE`` with one pattern per line, resolves the matches, gives a unique name to the collisions and moves each file into ``~/.local/bin``, printing the new names.
Usage: tolo [-f LISTFILE] [files ...] Examples: $ tolo tool.py $ tolo -f scripts.txt Runs as a script (``python tolo.py``) in the directory it should act on; the module can also be imported.
"""

import glob
from pathlib import Path
import shutil
import sys

from dh import unique_path

h = Path.home() / ".local" / "bin"


def i(a: Path) -> list[Path]:
    g = Path(a)
    if g.exists():
        return [g]
    k = glob.glob(a, recursive=True)
    return [Path(f) for f in k if Path(f).is_file()]


def main() -> None:
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
    raise SystemExit(main())
