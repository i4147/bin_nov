#!/data/data/com.termux/files/usr/bin/env python
"""Group similar files by fuzzy hashing (ssdeep / ppdeep).
Hashes every file, compares the digests by similarity score and prints the groups (optionally as a table), writing a report, a similarity matrix and a CSRF-free copy of the groups when asked.
Useful for spotting near-duplicate documents or binaries.
Usage: fssim [paths ...] Examples: $ fssim ~/documents Runs as a script (``python fssim.py``) in the directory it should act on; the module can also be imported.
"""

import csv
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable

from ppdeep import compare, hash_from_file
import ssdeep

try:
    from tabulate import tabulate

    USE_TABULATE = True
except ImportError:
    USE_TABULATE = False
try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    USE_COLOR = True
except ImportError:
    USE_COLOR = False


def get_all_files(root: str = "."):
    paths = []
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            full_path = Path(dirpath).joinpath(f)
            paths.append(full_path)
    return paths


def compute_hashes(files: list[Path]):
    hashes = {}
    for f in files:
        hashes[f] = hash_from_file(str(f))
    return hashes


def group_similar_files(hashes: dict[str, Any], threshold: int):
    matrx = {}
    visited = set()
    groups = []
    files = list(hashes.keys())
    for i, f1 in enumerate(files):
        if f1 in visited:
            continue
        group = [f1]
        visited.add(f1)
        for f2 in files[i + 1 :]:
            if f2 in visited:
                continue
            score = compare(hashes[f1], hashes[f2])
            if score >= threshold:
                matrx[score] = f1, f2
                group.append(f2)
                visited.add(f2)
        if len(group) > 1:
            groups.append(group)
    with Path("similars.json").open("w", encoding="utf-8") as f:
        json.dump(matrx, f)
    print("similars.json created.")
    return groups


def copy_groups(groups: Iterable[Any], output_dir="output") -> None:
    Path(output_dir).mkdir(exist_ok=True, parents=True)
    for idx, group in enumerate(groups, start=1):
        group_dir = Path(output_dir).joinpath(f"group_{idx}")
        Path(group_dir).mkdir(exist_ok=True, parents=True)
        for f in group:
            try:
                shutil.move(f, group_dir)
            except Exception as e:
                print(f"Failed to copy {f}: {e}")


def write_report(groups: Iterable[Any], format="csv", output_dir="output") -> None:
    Path(output_dir).mkdir(exist_ok=True, parents=True)
    if format == "csv":
        report_file = Path(output_dir).joinpath("similar_report.csv")
        with Path(report_file).open("w", encoding="utf-8", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Group", "File"])
            for idx, group in enumerate(groups, start=1):
                for f in group:
                    writer.writerow([idx, f])
        print(f"CSV report written to {report_file}")
    elif format == "json":
        report_file = Path(output_dir).joinpath("similar_report.json")
        data = {f"group_{idx}": group for idx, group in enumerate(groups, start=1)}
        with Path(report_file).open("w", encoding="utf-8") as jf:
            json.dump(data, jf, indent=2)
        print(f"JSON report written to {report_file}")


def colorize_score(score: str, threshold: float) -> str:
    if not USE_COLOR or score == "":
        return str(score)
    if score == 100 or score >= threshold + 10:
        return Fore.GREEN + str(score) + Style.RESET_ALL
    if score >= threshold:
        return Fore.YELLOW + str(score) + Style.RESET_ALL
    return Fore.RED + str(score) + Style.RESET_ALL


def write_matrix(hashes: dict[str, Any], threshold: int, output_dir="output", pretty=False) -> None:
    Path(output_dir).mkdir(exist_ok=True, parents=True)
    files = list(hashes.keys())
    matrix_file = Path(output_dir).joinpath("similarity_matrix.csv")
    table = [["File", *files]]
    with Path(matrix_file).open("w", encoding="utf-8", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["File", *files])
        for f1 in files:
            row = [f1]
            for f2 in files:
                if f1 == f2:
                    score = 100
                else:
                    score = ssdeep.compare(hashes[f1], hashes[f2])
                    score = score if score >= threshold else ""
                row.append(score)
            writer.writerow(row)
            table.append(row)
    print(f"Threshold-filtered similarity matrix written to {matrix_file}")
    if pretty:
        if USE_TABULATE:
            colored_table = []
            for row in table[1:]:
                colored_row = [row[0]] + [colorize_score(cell, threshold) for cell in row[1:]]
                colored_table.append(colored_row)
            print(tabulate(colored_table, headers=table[0], tablefmt="grid"))
        else:
            header = " | ".join(table[0])
            print(header)
            print("-" * len(header))
            for row in table[1:]:
                formatted = [row[0]] + [colorize_score(cell, threshold) for cell in row[1:]]
                print(" | ".join(str(x) if x != "" else "." for x in formatted))


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <threshold> [copy|csv|json|matrix]")
        sys.exit(1)
    try:
        threshold = int(sys.argv[1])
    except ValueError:
        print("Threshold must be an integer (0–100).")
        sys.exit(1)
    mode = sys.argv[2] if len(sys.argv) > 2 else "copy"
    files = get_all_files(".")
    print(f"Found {len(files)} files. Computing hashes...")
    hashes = compute_hashes(files)
    print("Comparing files...")
    groups = group_similar_files(hashes, threshold)
    if not groups and mode != "matrix":
        print("No similar files found.")
    elif mode == "copy":
        print(f"Found {len(groups)} groups of similar files.")
        copy_groups(groups)
        print("Copied groups to 'output' directory.")
    elif mode in {"csv", "json"}:
        print(f"Found {len(groups)} groups of similar files.")
        write_report(groups, format=mode)
    elif mode == "matrix":
        write_matrix(hashes, threshold, pretty=True)
    else:
        print("Unknown mode. Use 'copy', 'csv', 'json', or 'matrix'.")


if __name__ == "__main__":
    raise SystemExit(main())
