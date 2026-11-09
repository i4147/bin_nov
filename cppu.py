#!/data/data/com.termux/files/usr/bin/env python
"""Reformat C/C++/Java/JS/JSON files in place with ``clang-format``.
Runs ``clang-format -i --style=LLVM`` over the files named on the command line, or over the matching extensions below the current directory, in parallel, and reports the size change.
Usage: cppu [files ...] Examples: $ cppu src/ Runs as a script (``python cppu.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import cprint, fsz, get_files, gsz, mpf, rrs, runcmd


EXT = [
    ".java",
    ".c",
    ".cpp",
    ".cxx",
    ".cc",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".js",
    ".json",
]


def process_file(path):
    path = Path(path)
    before = gsz(path)
    try:
        runcmd(["clang-format", "-i", "--style=LLVM", str(path)], show_output=False)
        after = gsz(path)
        rrs(path, before, after)
        del before, after
        return
    except:
        return


def main() -> None:
    files: list = []
    cwd = Path.cwd()
    before = gsz(cwd)
    args = sys.argv[1:]
    files = [Path(arg) for arg in args] if args else get_files(cwd, ext=EXT)
    all_count = len(files)
    cprint(f"{all_count} files found", "cyan")
    if all_count == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)
    after = gsz(cwd)
    dsz = before - after
    print(f"space change: {fsz(dsz)}")


if __name__ == "__main__":
    raise SystemExit(main())
