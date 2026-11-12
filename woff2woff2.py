#!/data/data/com.termux/files/usr/bin/env python
"""Encode WOFF fonts as WOFF2.
Loads each ``.woff`` font, saves it with the ``woff2`` flavour next to the source, removes the original and works through the files in a pool.
Usage: woff2woff2 [fonts ...] Examples: $ woff2woff2 fonts/ Runs as a script (``python woff2woff2.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import cprint, get_files, mpf, unique_path
from fontTools.ttLib import TTFont

cwd = Path.cwd()


def process_file(path: Path) -> None:
    path = Path(path)
    woff2_path = path.with_suffix(".woff2")
    if woff2_path.exists() and woff2_path.stat().st_size:
        woff2_path = unique_path(woff2_path)
    try:
        font = TTFont(path)
        font.flavor = "woff2"
        font.save(woff2_path)
        print(f"{path.name} converted.")
        path.unlink()
    except:
        cprint(f"error convering {path.name}")


def main() -> None:
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".woff"])
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
