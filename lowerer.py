#!/data/data/com.termux/files/usr/bin/env python
"""
Lower-case the whole content of a text file. Reads the file as UTF-8, applies :meth:`str.lower` to the entire text and writes it back in place.

Usage:
    lowerer FILE

Examples:
    $ lowerer notes.txt Runs as a script (python lowerer.py) in the directory it should act on; the module can also be imported.

"""

from __future__ import annotations
from pathlib import Path
import sys

from dh import atomic_write


def main() -> None:
    fn = Path(sys.argv[1])
    content = fn.read_text(encoding="utf-8")
    lower_content = content.lower()
    atomic_write(fn, lower_content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
