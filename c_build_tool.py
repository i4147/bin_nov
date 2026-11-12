#!/data/data/com.termux/files/usr/bin/env python
"""Build the C, C++ and Cython sources of a directory in parallel.
Compiles each ``.c``/``.cpp``/``.cc``/``.cxx`` file with clang/clang++ (``--cflags`` passes extra flags, ``--strip`` strips the binaries), the ``cythonize`` subcommand builds the ``.pyx`` modules and the jobs run in a thread pool with a 60-second timeout.
Usage: c_build_tool [build] [--strip] [--cflags FLAGS] [paths ...] | c_build_tool cythonize Positional arguments: paths ...
Files or dirs (default: current dir) Options: --strip Strip each binary after compiling --cflags Extra compiler flags, e.g.
--cflags="-O2 -Wall" Examples: $ c_build_tool --cflags '-O2 -Wall' src/ Command-line script: run ``python c_build_tool.py`` or the ``c_build_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess
import sys

from dh import MAX_WORKERS, SKIP_DIRS

TIMEOUT_SECONDS: int = 60
COMPILERS: dict[str, str] = {".c": "clang", ".cpp": "clang++", ".cc": "clang++", ".cxx": "clang++"}


@dataclass(slots=True)
class JobResult:
    source: Path
    ok: bool
    message: str


def collect_sources(inputs: Sequence[Path], suffixes: frozenset[str]) -> list[Path]:
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            if root.suffix.lower() in suffixes:
                found.add(root)
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in suffixes and not SKIP_DIRS.intersection(path.parts):
                    found.add(path)
    return sorted(found)


def compile_one(job: tuple[Path, str, tuple[str, ...], bool]) -> JobResult:
    source, _unused, flags, strip = job
    compiler: str = COMPILERS[source.suffix.lower()]
    output: Path = source.with_suffix("")
    try:
        proc = subprocess.run(
            [compiler, str(source), "-o", str(output), *flags], capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return JobResult(source, False, f"timeout: {source.name}")
    except FileNotFoundError:
        return JobResult(source, False, f"compiler not found: {compiler}")
    if proc.returncode != 0:
        return JobResult(source, False, f"failed: {source.name}\n{proc.stderr}")
    if strip:
        subprocess.run(["strip", str(output)], capture_output=True)
    return JobResult(source, True, f"compiled: {source.name} -> {output.name}")


def cythonize_one(source: Path) -> JobResult:
    try:
        proc = subprocess.run(
            ["cythonize", source.name], cwd=source.parent, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except FileNotFoundError:
        return JobResult(source, False, "cythonize not found (pip install Cython)")
    except subprocess.TimeoutExpired:
        return JobResult(source, False, f"timeout: {source.name}")
    if proc.returncode != 0:
        return JobResult(source, False, f"failed: {source.name}\n{proc.stderr}")
    return JobResult(source, True, f"cythonized: {source.name}")


def run_parallel(
    jobs: Sequence[JobResult] | list[tuple[Path, str, tuple[str, ...], bool]], worker: str, workers: int
) -> int:
    fn = compile_one if worker == "compile" else cythonize_one
    failed: int = 0
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fn, job) for job in jobs]
        for future in as_completed(futures):
            result: JobResult = future.result()
            print(("OK  " if result.ok else "FAIL") + f" {result.message}")
            failed += 0 if result.ok else 1
    return failed


def cmd_build(args: argparse.Namespace) -> int:
    sources = collect_sources(args.paths, frozenset(COMPILERS))
    if not sources:
        print("No .c/.cpp files found.")
        return 0
    flags: tuple[str, ...] = tuple(shlex.split(args.cflags))
    jobs: list[tuple[Path, str, tuple[str, ...], bool]] = [(s, "", flags, args.strip) for s in sources]
    print(f"Compiling {len(sources)} file(s) with {MAX_WORKERS} worker(s)...")
    failed: int = run_parallel(jobs, "compile", MAX_WORKERS)
    print(f"Summary: {len(sources) - failed} ok, {failed} failed")
    return 1 if failed else 0


def cmd_cythonize(args: argparse.Namespace) -> int:
    sources = collect_sources(args.paths, frozenset({".pyx"}))
    if not sources:
        print("No .pyx files found.")
        return 0
    failed: int = run_parallel(sources, "cython", MAX_WORKERS)
    print(f"Summary: {len(sources) - failed} ok, {failed} failed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parallel C/C++/Cython builder (clang by default).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("build", "Compile .c/.cpp files"), ("cythonize", "Cythonize .pyx files")):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: current dir)")
    build = sub.choices["build"]
    build.add_argument("--strip", action="store_true", help="Strip each binary after compiling")
    build.add_argument("--cflags", default="", help='Extra compiler flags, e.g. --cflags="-O2 -Wall"')
    sub.choices["build"].set_defaults(handler=cmd_build)
    sub.choices["cythonize"].set_defaults(handler=cmd_cythonize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
