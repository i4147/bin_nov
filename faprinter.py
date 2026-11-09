#!/data/data/com.termux/files/usr/bin/env python
"""Print a file line by line with correct right-to-left rendering.
Each line is passed through ``faprint`` (the Persian text shaper) and printed, so mixed Persian/English text keeps its visual order in a terminal.
Usage: faprinter FILE Examples: $ faprinter poem.txt Runs as a script (``python faprinter.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from faprint import faprint as pp


def ylines(path: Path):
    with path.open(encoding="utf-8") as f:
        yield from f


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    for k in ylines(fn):
        print(pp(k))
