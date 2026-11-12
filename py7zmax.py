#!/data/data/com.termux/files/usr/bin/env python
"""Compress a single file into a ``.7z`` archive with maximum LZMA2 settings.
Writes ``<name>.7z`` with the LZMA2 filter at preset 9 plus the extreme flag, replaces the file atomically and removes the original once the archive is complete.
Existing archives are never overwritten.
Usage: py7zmax FILE Examples: $ py7zmax dump.sql Runs as a script (``python py7zmax.py``) in the directory it should act on; the module can also be imported.
"""

import contextlib
import lzma
import os
from pathlib import Path
import sys

import py7zr


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"Error: '{src}' is not a regular file", file=sys.stderr)
        return 1
    dst = src.with_name(src.name + ".7z")
    if dst.exists():
        print(f"Error: '{dst}' already exists", file=sys.stderr)
        return 1
    filters = [
        {
            "id": py7zr.FILTER_LZMA2,
            "preset": 9 | lzma.PRESET_EXTREME,
        }
    ]
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with py7zr.SevenZipFile(tmp, "w", filters=filters) as z:
            z.write(src, arcname=src.name)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    src.unlink()
    print(f"OK: '{src}' -> '{dst}' (original removed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
