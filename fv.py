#!/data/data/com.termux/files/usr/bin/env python
"""Page through a text file with :mod:`pydoc`'s pager.
A minimal ``less`` replacement: reads the file and hands it to ``pydoc.pager``, which uses the terminal pager and keeps the raw text (no rendering).
Usage: fv FILE Examples: $ fv notes.md Runs as a script (``python fv.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import pydoc
import sys


def main() -> None:
    pydoc.pager(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
