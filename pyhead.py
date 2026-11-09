#!/data/data/com.termux/files/usr/bin/env python
"""Print the first kilobyte of a file.
Reads the file as UTF-8 (ignoring decode errors) and prints the first 1024 characters, falling back to a binary read when the text mode fails.
Usage: pyhead FILE Examples: $ pyhead data.bin Runs as a script (``python pyhead.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    try:
        with fn.open(encoding="utf-8", errors="ignore") as f:
            print(f.read(1024))
    except:
        with fn.open("rb") as f:
            print(f.read(1024))
