#!/data/data/com.termux/files/usr/bin/env python
"""Reinstall the pure-Python packages, or the packages of a list.
Collects the installed pure-Python distributions (or parses ``-f`` package file), processes them in batches of ten and reinstalls each with ``pip --force-reinstall --no-deps``, reporting the failures.
Usage: pip_reinstaller [-f requirements.txt] Options: -f, --file Path to a package list file (e.g., requirements.txt) Examples: $ pip_reinstaller -f requirements.txt Command-line script: run ``python pip_reinstaller.py`` or the ``pip_reinstaller`` command.
The module is importable as well."""

import argparse
import importlib.metadata
import multiprocessing
from pathlib import Path
import sys

from pip._internal.commands import main as pip_main


def installer(batch):
    if not batch:
        return
    print(f"[+] Reinstalling batch: {batch}")
    args = ["install", "--no-deps", "--force-reinstall"] + list(batch)
    try:
        pip_main(args)
    except TypeError:
        pip_main("install", "--no-deps", "--force-reinstall", *batch)


def _inspect_distribution(dist):
    try:
        name = dist.metadata.get("Name", "")
        if not name:
            return None
        if name.lower() in {"numpy", "scipy", "pandas"}:
            return None
        files = dist.files
        if not files:
            return None
        total_size = 0
        is_pure = True
        for f in files:
            if f.suffix.lower() in {".so", ".pyd", ".dll"}:
                is_pure = False
                break
            try:
                p = f.locate()
                if p.exists() and p.is_file():
                    total_size += p.stat().st_size
            except Exception:
                pass
        if is_pure:
            return (name, total_size)
    except Exception:
        pass
    return None


def get_pure_python_packages_sorted():
    dists = list(importlib.metadata.distributions())
    print(f"[*] Scanning {len(dists)} distributions using multiprocessing...")
    with multiprocessing.Pool(processes=min(4, multiprocessing.cpu_count())) as pool:
        results = pool.map(_inspect_distribution, dists)
    packages_info = [res for res in results if res is not None]
    packages_info.sort(key=lambda x: x[1])
    return [pkg[0] for pkg in packages_info]


def chunked(iterable, n):
    for i in range(0, len(iterable), n):
        yield iterable[i : i + n]


def parse_package_file(file_path):
    path = Path(file_path)
    if not path.exists():
        print(f"[!] Error: File '{file_path}' not found.")
        sys.exit(1)
    pkgs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                pkgs.append(line)
    return pkgs


if __name__ == "__main__":
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Reinstall pure Python packages or from a list file.")
    parser.add_argument("-f", "--file", type=str, help="Path to a package list file (e.g., requirements.txt)")
    args = parser.parse_args()
    if args.file:
        print(f"[*] Reading packages from file: {args.file}")
        pkgs_to_process = parse_package_file(args.file)
        print(f"[*] Loaded {len(pkgs_to_process)} packages from file. Skipping filters and sort.")
    else:
        print("[*] Scanning site-packages folder for pure Python packages...")
        pkgs_to_process = get_pure_python_packages_sorted()
        print(f"[*] Found {len(pkgs_to_process)} pure Python packages to reinstall.")
    batches = list(chunked(pkgs_to_process, 10))
    if not batches:
        print("[!] No packages found to process.")
        sys.exit(0)
    print(f"[*] Processing {len(batches)} batches sequentially (10 pkgs per batch)...")
    for batch in batches:
        installer(batch)
    print("[✓] Reinstallation complete!")
