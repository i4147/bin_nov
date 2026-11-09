#!/data/data/com.termux/files/usr/bin/env python
"""Compile Python files to bytecode, optionally deleting the sources.
Runs ``compileall`` over the files or directories given (default: every ``.py`` below the current directory, in a worker pool) with the chosen ``--optimize`` level; ``--legacy`` writes the ``.pyc`` next to the source instead of ``__pycache__`` and ``REMOVE_ORIG`` deletes the ``.py`` afterwards.
Usage: mkpic [-o LEVEL] [-l] [paths ...] Examples: $ mkpic -o 2 pkg/ Runs as a script (``python mkpic.py``) in the directory it should act on; the module can also be imported."""

import compileall
import os
from pathlib import Path
import sys

from dh import get_pyfiles, mpf


REMOVE_ORIG: bool = False
LEGACY_MODE: bool = False
OPTIMIZE_LEVEL: int = 0


def process_file(path: Path) -> bool | None:
    path = Path(path)
    if not path.exists() or path.is_symlink():
        return False
    if ".git" in path.parts:
        return False
    if path.is_dir():
        for f in path.rglob("*.py"):
            process_file(f)
    if path.is_file():
        pyc_file = path.with_suffix(".pyc") if LEGACY_MODE else None
        if pyc_file and pyc_file.exists():
            pyc_file.unlink()
        compileall.compile_file(path, optimize=OPTIMIZE_LEVEL, legacy=LEGACY_MODE)
        if REMOVE_ORIG:
            path.unlink()
        return True
    return False


def main() -> int:
    global REMOVE_ORIG, LEGACY_MODE, OPTIMIZE_LEVEL
    os.environ["PYTHONPYCACHEPREFIX"] = "__pycache__"
    cwd = Path.cwd()
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "--optimize"):
            if i + 1 < len(args):
                try:
                    OPTIMIZE_LEVEL = int(args[i + 1])
                    if OPTIMIZE_LEVEL not in (0, 1, 2):
                        print(f"Error: Optimize level must be 0, 1, or 2 (got {OPTIMIZE_LEVEL})")
                        return 1
                    i += 2
                    continue
                except ValueError:
                    print(f"Error: Invalid optimize level: {args[i + 1]}")
                    return 1
            else:
                print("Error: -o/--optimize requires an argument (0, 1, or 2)")
                return 1
        elif arg in ("-l", "--legacy"):
            LEGACY_MODE = True
            i += 1
        elif arg in ("-h", "--help"):
            print("Usage: python script.py [options] [files/directories]")
            print("Options:")
            print("  -o, --optimize LEVEL  Set optimization level (0, 1, or 2, default: 0)")
            print("  -l, --legacy        Create legacy .pyc file beside original file")
            print("  -h, --help          Show this help message")
            print("  files/directories   Files or directories to process (default: current directory)")
            return 0
        else:
            break
    files = []
    if i < len(args):
        for arg in args[i:]:
            p = Path(arg)
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files.extend(get_pyfiles(p))
    else:
        files = get_pyfiles(cwd)
    if not files:
        print("No Python files found to process")
        return 0
    if len(files) == 1:
        process_file(files[0])
        return 0
    mpf(process_file, files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
