#!/data/data/com.termux/files/usr/bin/env python
"""Compile Python files to ``__pycache__`` bytecode in parallel.
Compiles every ``.py`` file given on the command line (or below the current directory) with ``compileall`` using a small spawn-based worker pool and reports how the tree size changed.
Usage: mkpyc [files ...] Examples: $ mkpyc src/ Runs as a script (``python mkpyc.py``) in the directory it should act on; the module can also be imported."""

from collections import deque
import compileall
from multiprocessing import get_context
from pathlib import Path
import sys

from dh import fsz, get_files, gsz


MAX_QUEUE = 4


def process_file(path) -> bool | None:
    path = Path(path)
    if not path.exists():
        return False
    if ".git" in path.parts:
        return None
    compileall.compile_file(path, legacy=False, optimize=0)
    return True


def main() -> None:
    cwd = Path.cwd()
    before = gsz(cwd)
    args = sys.argv[1:]
    files = [Path(f) for f in args] if args else get_files(cwd, ext=[".py"])
    with get_context("spawn").Pool(8) as pool:
        pending = deque()
        for f in files:
            pending.append(pool.apply_async(process_file, (f,)))
            if len(pending) > MAX_QUEUE:
                pending.popleft().get()
        while pending:
            pending.popleft().get()
    after = gsz(cwd)
    diff_size = before - after
    if after > before:
        sign = "+"
    elif before > after:
        sign = "-"
    else:
        sign = ""
    print(f"space changed : {sign} {fsz(diff_size)}")


if __name__ == "__main__":
    raise SystemExit(main())
