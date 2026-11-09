#!/data/data/com.termux/files/usr/bin/env python
"""Rename a compiled extension to the Android CPython 3.14 file name.
Takes the current file and a new stem and renames the file to ``<stem>.cpython-314-arm-linux-androideabi.so`` in the same directory.
Usage: raster FILE.so NEW_STEM Examples: $ raster _speedups.so _speedups Runs as a script (``python raster.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    new_stem = sys.argv[2]
    new_name = new_stem + ".cpython-314-arm-linux-androideabi.so"
    new_path = fn.with_name(new_name)
    fn.rename(new_path)
