#!/data/data/com.termux/files/usr/bin/env python
"""Syntax-check C and C++ sources with clang in parallel.
Runs ``clang -fsyntax-only`` for ``.c``/``.h``/``.inc`` files and ``clang++ -fsyntax-only`` for the C++ extensions, over the files on the command line or below the current directory, and prints one OK/error line per file from a spawn-based pool.
Usage: pycppcheck [files ...] Examples: $ pycppcheck src/ Runs as a script (``python pycppcheck.py``) in the directory it should act on; the module can also be imported."""

from collections import deque
from multiprocessing import get_context
from pathlib import Path
import sys

from dh import cprint, get_files


c_files = {".c", ".h", ".inc"}
cpp_files = {".cpp", ".cc", ".cxx", ".hpp", ".hpp11", ".hh", ".hxx"}


def validate_cpp(path: Path) -> tuple[bool, str]:
    cmd = ""
    if path.suffix in c_files:
        cmd = "clang -fsyntax-only str(path)"
    if path.suffix in cpp_files:
        cmd = "clang++ -fsyntax-only str(path)"
    ret, txt, err = run_command(cmd)
    return (path, ret, txt, err)


if __name__ == "__main__":
    args = sys.argv[1:]
    cwd = Path.cwd()
    files = list(
        [Path(p) for p in args]
        if args
        else get_files(
            cwd,
            ext=[
                ".c",
                ".cc",
                ".cpp",
                ".cxx",
                ".h",
                ".hh",
                ".hpp",
                ".hxx",
                ".inc",
                "hpp11",
            ],
        )
    )
    results = []
    with get_context("spawn").Pool(8) as pool:
        pending = deque()
        for f in files:
            pending.append(pool.apply_async(validate_cpp, (f,)))
            if len(pending) > 8:
                results.append(pending.popleft().get())
        while pending:
            results.append(pending.popleft().get())
    for result in results:
        if int(result[1]) == 2:
            cprint(f"[✖] : {result[0].name} has error", "white")
        else:
            cprint(f"[✅] : {result[0].name} is ok", "cyan")
