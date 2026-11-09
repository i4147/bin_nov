#!/data/data/com.termux/files/usr/bin/env python
"""Show the largest files below the current directory in a table.
Sorts the files by size, truncates the long paths and prints the top ``N`` (11 by default) with their human-readable sizes in aligned columns.
Usage: top11 [N] Examples: $ top11 20 Runs as a script (``python top11.py``) in the directory it should act on; the module can also be imported."""

import operator
from pathlib import Path
import sys

from dh import fsz, get_files


cwd = Path.cwd()
N: int = int(sys.argv[1]) if len(sys.argv) > 1 else 11


def get_sizes() -> list[tuple[Path, int]]:
    return [(path.relative_to(cwd), path.stat().st_size) for path in get_files(cwd)]


def main() -> None:
    sizez = get_sizes()
    if not sizez:
        print("No files found or unable to access directory.")
        return
    sizez.sort(key=operator.itemgetter(1), reverse=True)
    num_files = N or 11
    top_files = sizez[:num_files]
    max_path_len = max((len(str(path)) for path, size in top_files))
    max_path_len = min(max_path_len, 80)
    print(f"{'No.':<4} {'File Path':<{max_path_len}} {'Size':>12}")
    print("-" * (max_path_len + 20))
    for i, (path, size) in enumerate(top_files, 1):
        path_str = str(path)
        if len(path_str) > max_path_len:
            path_str = "..." + path_str[-(max_path_len - 3) :]
        size_str = fsz(size)
        print(f"{i:<3} {path_str[: max_path_len - 3]:<{max_path_len}} {size_str:>12}")


if __name__ == "__main__":
    raise SystemExit(main())
