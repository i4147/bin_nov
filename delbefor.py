#!/data/data/com.termux/files/usr/bin/env python
"""Cut each line of a file after the first occurrence of a marker character.
Keeps everything after the marker, leaves lines without it untouched and drops empty lines before rewriting the file in place.
Usage: delbefor FILE MARKER Examples: $ delbefor list.txt '=' Runs as a script (``python delbefor.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys


def read_lines(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield line


if __name__ == "__main__":
    file_name = Path(sys.argv[1])
    nl = []
    target_char = sys.argv[2]
    for line in read_lines(file_name):
        stripped = line.strip()
        if stripped and target_char in stripped:
            indx = stripped.index(target_char)
            cleaned = stripped[indx + 1 :]
            nl.append(cleaned)
        elif stripped:
            nl.append(stripped)
    if nl:
        file_name.write_text("\n".join(nl), encoding="utf-8")
