#!/data/data/com.termux/files/usr/bin/env python
"""Find the duplicate files of the current directory with an xor hash.
Hashes every non-empty file in parallel with ``xorhash``, groups the identical hashes and prints each group; ``-r`` keeps the first file of a group and deletes the rest.
Usage: xordup [-r] Examples: $ xordup $ xordup -r Runs as a script (``python xordup.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import cprint, mpf
from xorhash import get_xorhash


REMOVE: bool = "-r" in sys.argv


def find_dups_optimized(root: Path):
    from os import walk as os_walk

    file_hashes = {}
    paths_to_process = []
    for r, _, files in os_walk(root):
        for file in files:
            path = Path(r) / file
            if ".git" in path.parts or not path.stat().st_size:
                continue
            if not path.is_symlink() and path.is_file() and path.exists():
                paths_to_process.append(path)
    if not paths_to_process:
        return {}
    results = mpf(get_xorhash, paths_to_process)
    for res in results:
        hash_result, path = res
        if hash_result is not None:
            file_hashes.setdefault(hash_result, []).append(path)
    return {h: paths for h, paths in file_hashes.items() if len(paths) > 1}


if __name__ == "__main__":
    cwd = Path.cwd()
    dupes = find_dups_optimized(cwd)
    if not dupes:
        print("No dups")
        sys.exit(1)
    for h, paths in dupes.items():
        cprint(f"dups with hash: {h}")
        for p in paths:
            print(" - ", p)
    if REMOVE:
        for paths in dupes.values():
            for p in paths[1:]:
                Path(p).unlink()
    print(f"Found {len(dupes)} group(s) of dups")
