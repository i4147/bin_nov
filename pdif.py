#!/data/data/com.termux/files/usr/bin/env python
"""Compare two text files and report the differing line numbers.
Prints the line count of both files, how many lines they share and the one-based numbers of the lines that only occur in one of them.
Usage: pdif FILE1 FILE2 Examples: $ pdif old.txt new.txt Runs as a script (``python pdif.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


def compare_files(file1: str, file2: str) -> None:
    try:
        with (
            Path(file1).open("r", encoding="utf-8") as f1,
            Path(file2).open("r", encoding="utf-8") as f2,
        ):
            lines1 = f1.readlines()
            lines2 = f2.readlines()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return
    lines1 = [line.rstrip("\n") for line in lines1]
    lines2 = [line.rstrip("\n") for line in lines2]
    diff_lines_1 = []
    diff_lines_2 = []
    common_count = 0
    for i, line in enumerate(lines1):
        if line not in lines2:
            diff_lines_1.append(i + 1)
    for i, line in enumerate(lines2):
        if line not in lines1:
            diff_lines_2.append(i + 1)
    for line in lines1:
        if line in lines2:
            common_count += 1
    print(f"{file1} : {len(lines1)}")
    print(f"{file2} : {len(lines2)}")
    print(f"common: {common_count}")
    print(f"Number of different lines in File 1: {len(diff_lines_1)}")
    if diff_lines_1:
        print(f"Line numbers: {diff_lines_1}")
    print(f"Number of different lines in File 2: {len(diff_lines_2)}")
    if diff_lines_2:
        print(f"Line numbers: {diff_lines_2}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python script.py <file1> <file2>")
        sys.exit(1)
    compare_files(sys.argv[1], sys.argv[2])
