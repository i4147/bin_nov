#!/data/data/com.termux/files/usr/bin/env python
"""Compile Python sources to bytecode and measure the space it saves.
The ``compile`` subcommand byte-compiles the targets with ``-o``/``-l``/``--delete-source`` options and ``measure`` reports the size change of the current directory before and after, working through a process pool.
Usage: mkpik {compile,measure} [targets ...] [-o 0|1|2] [-l] Positional arguments: targets ...
Files or directories to compile Subcommands: compile Compile Python files with custom optimization and output options measure Compile Python files and report space change in current directory Options: -o, --optimize Optimization level (0, 1, or 2) -l, --legacy Create legacy .pyc file beside original file --delete-source Delete source .py files after compilation --pycache-prefix Directory prefix for pycache files --pool-size Number of worker processes Examples: $ mkpik measure src/ Command-line script: run ``python mkpik.py`` or the ``mkpik`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import os
from pathlib import Path
import py_compile
import sys


def get_py_files(target: Path) -> list[Path]:
    if not target.exists() or target.is_symlink():
        return []
    if ".git" in target.parts:
        return []
    if target.is_file() and target.suffix == ".py":
        return [target]
    if target.is_dir():
        files = []
        for p in target.rglob("*.py"):
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts:
                files.append(p)
        return files
    return []


def get_dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total


def format_size(size_bytes: int) -> str:
    size = float(abs(size_bytes))
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def compile_single_file(file_path: Path, optimize: int = 0, legacy: bool = False, delete_source: bool = False) -> bool:
    file_path = Path(file_path)
    if not file_path.exists() or file_path.is_symlink() or ".git" in file_path.parts:
        return False
    if file_path.is_file():
        pyc_file = file_path.with_suffix(".pyc") if legacy else None
        py_compile.compile(file_path, cfile=pyc_file, optimize=optimize, doraise=True)
        if delete_source:
            file_path.unlink()
        return True
    return False


def cmd_compile(args: argparse.Namespace) -> None:
    os.environ["PYTHONPYCACHEPREFIX"] = args.pycache_prefix
    sys.pycache_prefix = args.pycache_prefix
    targets = [Path(p) for p in args.targets]
    py_files: list[Path] = []
    for target in targets:
        py_files.extend(get_py_files(target))
    if not py_files:
        print("No Python files found to process")
        return
    if len(py_files) == 1:
        compile_single_file(py_files[0], optimize=args.optimize, legacy=args.legacy, delete_source=args.delete_source)
        return
    with ProcessPoolExecutor() as executor:
        futures = [
            executor.submit(compile_single_file, f, args.optimize, args.legacy, args.delete_source) for f in py_files
        ]
        for future in futures:
            future.result()


def cmd_measure(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    initial_size = get_dir_size(cwd)
    if args.targets:
        py_files = []
        for t in args.targets:
            py_files.extend(get_py_files(Path(t)))
    else:
        py_files = get_py_files(cwd)
    with ProcessPoolExecutor(max_workers=args.pool_size) as executor:
        futures = [executor.submit(compile_single_file, f, 0, False, False) for f in py_files]
        for future in futures:
            future.result()
    final_size = get_dir_size(cwd)
    size_diff = initial_size - final_size
    if final_size > initial_size:
        sign = "+"
    elif initial_size > final_size:
        sign = "-"
    else:
        sign = ""
    print(f"space changed :{sign} {format_size(size_diff)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Python Bytecode Compiler and Disk Space Tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compile_parser = subparsers.add_parser(
        "compile", help="Compile Python files with custom optimization and output options"
    )
    compile_parser.add_argument("targets", nargs="*", default=["."], help="Files or directories to compile")
    compile_parser.add_argument(
        "-o", "--optimize", type=int, choices=[0, 1, 2], default=0, help="Optimization level (0, 1, or 2)"
    )
    compile_parser.add_argument(
        "-l", "--legacy", action="store_true", help="Create legacy .pyc file beside original file"
    )
    compile_parser.add_argument(
        "--delete-source", action="store_true", help="Delete source .py files after compilation"
    )
    compile_parser.add_argument("--pycache-prefix", default="__pycache__", help="Directory prefix for pycache files")
    compile_parser.set_defaults(func=cmd_compile)
    measure_parser = subparsers.add_parser(
        "measure", help="Compile Python files and report space change in current directory"
    )
    measure_parser.add_argument("targets", nargs="*", default=[], help="Specific files or directories to process")
    measure_parser.add_argument("--pool-size", type=int, default=8, help="Number of worker processes")
    measure_parser.set_defaults(func=cmd_measure)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
