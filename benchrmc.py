#!/data/data/com.termux/files/usr/bin/env python
"""Benchmark the comment/docstring stripper of ``rmc3`` on a tree of Python files.
Copies the discovered sources into a temporary directory, runs the ``rmc3`` transform pipeline over the copies with ``MAX_WORKERS`` processes and prints files/s, MiB/s and the error count, so the throughput of the cleaner can be measured without touching the originals.
Usage: benchrmc [PATH] Positional arguments: path Examples: $ benchrmc ~/code/bin Command-line script: run ``python benchrmc.py`` or the ``benchrmc`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import shutil
import tempfile
import time

from dh import MAX_WORKERS

import rmc3 as sc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    sources = list(sc.iter_python_files([args.path], []))
    if not sources:
        parser.error("no Python files found")
    options = sc.TransformOptions(
        remove_all=True,
        remove_all_comments=True,
        remove_docstrings=True,
        remove_type_annotations=True,
        remove_commented_code=False,
        collapse_blank_lines=True,
        make_backup=False,
        overwrite_backup=False,
        dry_run=False,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        copies = []
        for number, source in enumerate(sources):
            destination = root / f"{number}{source.suffix}"
            shutil.copyfile(source, destination)
            copies.append(destination)
        started = time.perf_counter()
        results = list(sc._result_iterator(copies, options, MAX_WORKERS, sc.DEFAULT_CHUNK_SIZE))
        elapsed = time.perf_counter() - started
    errors = sum(result.error is not None for result in results)
    total_bytes = sum(result.old_size for result in results)
    rate = len(results) / elapsed if elapsed else float("inf")
    mib_rate = total_bytes / (1024 * 1024) / elapsed if elapsed else float("inf")
    print(f"files={len(results)} bytes={total_bytes} seconds={elapsed:.3f}")
    print(f"throughput={rate:.1f} files/s, {mib_rate:.1f} MiB/s, errors={errors}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
