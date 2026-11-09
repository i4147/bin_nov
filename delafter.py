#!/data/data/com.termux/files/usr/bin/env python
"""Cut each line of a file at the first occurrence of a marker character.
Keeps the text before the marker (the marker itself is dropped), leaves lines without it untouched and drops empty lines before rewriting the file in place.
Usage: delafter FILE MARKER Examples: $ delafter links.txt '?' Runs as a script (``python delafter.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


if __name__ == "__main__":
    file_name = Path(sys.argv[1])
    nl = []
    target_char = sys.argv[2]
    for line in read_lines(file_name):
        stripped = line.strip()
        if stripped and target_char in stripped:
            indx = stripped.index(target_char)
            cleaned = stripped[:indx]
            nl.append(cleaned)
        elif stripped:
            nl.append(stripped)
    if nl:
        file_name.write_text("\n".join(nl), encoding="utf-8")
