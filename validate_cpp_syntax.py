#!/data/data/com.termux/files/usr/bin/env python
"""Syntax-check C and C++ sources and headers with the compiler.
Chooses gcc or g++ by suffix, runs ``-fsyntax-only`` on each file of the tree (or of the given paths) in a process pool and prints the files that fail.
Usage: validate_cpp_syntax [paths ...] Examples: $ validate_cpp_syntax src/ Runs as a script (``python validate_cpp_syntax.py``) in the directory it should act on; the module can also be imported.
"""

import multiprocessing as mp
from pathlib import Path
import subprocess
import sys

from dh import MAX_WORKERS

CPP_EXTS: set[str] = {".cpp", ".cc", ".cxx"}
C_EXTS: set[str] = {".c"}
HEADER_CPP_EXTS: set[str] = {".hpp", ".hh", ".hxx"}
HEADER_C_EXTS: set[str] = {".h"}
ALL_EXTS = CPP_EXTS | C_EXTS | HEADER_CPP_EXTS | HEADER_C_EXTS


def run(cmd: str, input_bytes=None):
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return proc.returncode, proc.stdout.decode(errors="replace")
    except FileNotFoundError as e:
        return 127, f"command not found: {e}"


def check_file(path: Path):
    ext = path.suffix.lower()
    if ext in CPP_EXTS:
        cmd = [
            "clang++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsyntax-only",
            str(path),
        ]
        rc, out = run(cmd)
    elif ext in C_EXTS:
        cmd = ["clang", "-std=c11", "-Wall", "-Wextra", "-fsyntax-only", str(path)]
        rc, out = run(cmd)
    elif ext in HEADER_CPP_EXTS:
        include_line = f'#include "{path.name}"\n'.encode()
        cmd = ["clang++", "-x", "c++", "-fsyntax-only", "-", "-I", str(path.parent)]
        rc, out = run(cmd, input_bytes=include_line)
    elif ext in HEADER_C_EXTS:
        include_line = f'#include "{path.name}"\n'.encode()
        cmd = ["clang", "-x", "c", "-fsyntax-only", "-", "-I", str(path.parent)]
        rc, out = run(cmd, input_bytes=include_line)
    else:
        return path, True, ""
    return path, rc == 0, out


def gather_files(paths: list[Path]):
    files = []
    if not paths:
        cwd = Path.cwd()
        for p in sorted(cwd.iterdir()):
            if p.is_file() and p.suffix.lower() in ALL_EXTS:
                files.append(p)
        return files
    for arg in paths:
        p = Path(arg)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in ALL_EXTS:
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            print(f"warning: skipping non-existent path: {arg}", file=sys.stderr)
    return files


def main() -> int:
    args = sys.argv[1:]
    files = gather_files(args)
    if not files:
        print("No C/C++/header files found.", file=sys.stderr)
        return 0
    print(f"Checking {len(files)} file(s) with {MAX_WORKERS} workers...", file=sys.stderr)
    failures = 0
    with mp.Pool(processes=MAX_WORKERS) as pool:
        for path, ok, output in pool.imap_unordered(check_file, files):
            if not ok:
                failures += 1
                print(f"\n=== FAIL: {path} ===")
                print(output.rstrip())
    if failures == 0:
        print("\nAll files passed syntax check.", file=sys.stderr)
    else:
        print(f"\n{failures} file(s) failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
