#!/data/data/com.termux/files/usr/bin/env python
"""Convert OpenType fonts to WOFF2.
Loads each ``.otf`` file with fontTools, sets the ``woff2`` flavour and saves ``<name>.woff2`` next to it (a unique name when that file already exists).
Files named on the command line or found below the current directory are converted in parallel.
Usage: otf2woff2 [fonts ...] Examples: $ otf2woff2 fonts/ Runs as a script (``python otf2woff2.py``) in the directory it should act on; the module can also be imported.
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
    except:
        cprint(f"error convering {path.name}")


def main() -> None:
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".otf"])
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
