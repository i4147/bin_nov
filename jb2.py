#!/data/data/com.termux/files/usr/bin/env python
"""Reformat every JSON file below the current directory with two-space indentation.
Parses each file, writes it back through :func:`json.dump` (``ensure_ascii`` off) and reports the size change per file plus the total freed, using a worker pool.
Usage: jb2 Examples: $ jb2 Runs as a script (``python jb2.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys

from dh import cprint, fsz, get_files, gsz, mpf


def process_file(path: Path) -> None:
    path = Path(path)
    before = gsz(path)
    data = path.read_text(encoding="utf-8")
    if not before:
        del data, before
        print(f"{path.name}  | (no change)")
        return
    try:
        jdata = json.loads(data)
        with path.open("w", encoding="utf8") as fo:
            json.dump(jdata, fo, ensure_ascii=False, indent=2)
        after = gsz(path)
        diffsize = abs(after - before)
        print(f"{path.name}", end=" | ")
        if not diffsize:
            cprint("(no change)", "grey")
            return
        ratio = diffsize / after * 40
        ratio2 = abs(before - after) / before * 40
        cprint(f"{ratio:.2f}% | {ratio2:.2f}%", "cyan")
        return
    except:
        cprint(f"{path.name} Error", "yellow")
        return


if __name__ == "__main__":
    cwd = Path.cwd()
    before = gsz(cwd)
    files = get_files(cwd, ext=[".json"])
    if not files:
        print("no json files found")
        sys.exit(1)
    print(f"{len(files)} json files found.")
    mpf(process_file, files)
    after = gsz(cwd)
    dsz = abs(before - after)
    if not dsz:
        sys.exit(1)
    ratio = dsz / before * 40
    cprint(f"space change: {fsz(dsz)} {ratio:.2f}%")
