#!/data/data/com.termux/files/usr/bin/env python
"""Run ``cythonize`` over Cython sources in parallel.
Collects the ``.pyx`` files given on the command line (directories are searched recursively) or below the current directory and runs ``cythonize`` in each file's parent directory with a worker pool.
Usage: cytonizer [paths ...] Examples: $ cytonizer src/ Runs as a script (``python cytonizer.py``) in the directory it should act on; the module can also be imported."""

import os
from os import chdir as os_chdir
from pathlib import Path
import sys

from dh import get_files, mpf


START_DIR = Path.cwd()
NUM_PROCESSES = 4


def process_file(path) -> None:
    path = Path(path)
    pardir = path.parent
    os_chdir(pardir)
    os.system(f"cythonize {path.name}")


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = []
    if args:
        for arg in args:
            p = Path(arg)
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files.extend(get_files(p, ext=[".pyx"]))
    else:
        files = get_files(cwd, ext=[".pyx"])
    _ = mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
