#!/data/data/com.termux/files/usr/bin/env python
"""Compile a C or C++ source with clang and run the resulting binary.
Picks clang/clang++ (falling back to gcc/g++) based on the extension, compiles with ``-Wall -Wextra -O2`` and ``-std=c17``/``-std=c++17`` into the temporary directory, makes the binary executable and runs it with the remaining arguments, forwarding stdin/stdout/stderr and the exit status.
Usage: cpprun SOURCE [args ...] Examples: $ cpprun main.cpp < input.txt Runs as a script (``python cpprun.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import stat
import sys

import sh


COMPILERS = {
    ".c": ("clang", ("clang", "gcc")),
    ".cpp": ("clang++", ("clang++", "g++")),
    ".cc": ("clang++", ("clang++", "g++")),
    ".cxx": ("clang++", ("clang++", "g++")),
}


def _pick_compiler(candidates):
    for c in candidates:
        if sh.which(c):
            return c
    return None


def cpp_run(*args):
    if not args:
        print("Usage: cpprun <file.c|file.cpp|file.cc> [args...]")
        return 1
    src = Path(args[0]).expanduser().resolve()
    if not src.is_file():
        print(f"Error: {src} not found", file=sys.stderr)
        return 1
    ext = src.suffix.lower()
    if ext not in COMPILERS:
        print(
            f"Error: unsupported extension '{ext}'. Expected one of: {', '.join(COMPILERS)}",
            file=sys.stderr,
        )
        return 1
    preferred, fallbacks = COMPILERS[ext]
    compiler_name = _pick_compiler((preferred, *fallbacks))
    if compiler_name is None:
        print("Error: no compiler found. Run: pkg install clang", file=sys.stderr)
        return 1
    tmpdir = Path(os.environ.get("TMPDIR") or f"{os.environ.get('PREFIX', '/data/data/com.termux/files/usr')}/tmp")
    tmpdir.mkdir(parents=True, exist_ok=True)
    exe = tmpdir / src.stem
    std_flag = "-std=c17" if ext == ".c" else "-std=c++17"
    compiler = sh.Command(compiler_name)
    try:
        compiler(
            std_flag,
            "-Wall",
            "-Wextra",
            "-O2",
            str(src),
            "-o",
            str(exe),
            _out=sys.stdout,
            _err=sys.stderr,
        )
    except sh.ErrorReturnCode as e:
        return e.exit_code
    except sh.CommandNotFound:
        print(f"Error: {compiler_name} not found on PATH", file=sys.stderr)
        return 1
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    try:
        prog = sh.Command(str(exe))
        prog(
            *args[1:],
            _in=sys.stdin,
            _out=sys.stdout,
            _err=sys.stderr,
        )
    except sh.ErrorReturnCode as e:
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(cpp_run(*sys.argv[1:]))
