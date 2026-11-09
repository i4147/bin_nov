#!/data/data/com.termux/files/usr/bin/env python
"""Convert the TTF/OTF fonts below the current directory to WOFF2.
Compresses each font with fontTools' ``woff2`` encoder, gives a unique name when the ``.woff2`` already exists, deletes the source on success and converts the files in parallel.
Usage: ttf2woff2 [fonts ...] Examples: $ ttf2woff2 fonts/ Runs as a script (``python ttf2woff2.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import cprint, get_files, mpf, unique_path
from fontTools.ttLib import woff2


cwd = Path.cwd()


def process_file(path: Path) -> None:
    path = Path(path)
    woff2_path = path.with_suffix(".woff2")
    if woff2_path.exists() and woff2_path.stat().st_size:
        woff2_path = unique_path(woff2_path)
    try:
        woff2.compress(path, woff2_path)
        print(f"{path.name} converted.")
        path.unlink()
    except:
        cprint(f"error convering {path.name}")


def main() -> None:
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".ttf", ".otf"])
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
