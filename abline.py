#!/data/data/com.termux/files/usr/bin/env python
"""Keep text before or near a marker in each nonempty input line.
Pass mode, file path, and marker as arguments; nonempty results replace the file.
Mode -a selects text before the first marker; other modes select from one character after its start."""

from pathlib import Path
import sys


def read_lines(path: Path) -> list[str]:
    """Read UTF-8 lines from path, retaining line endings."""
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def process_a(line: str, target_char: str) -> str:
    """Return stripped text before the first delimiter, or the whole text."""
    stripped = line.strip()
    if stripped and target_char in stripped:
        indx = stripped.index(target_char)
        return stripped[:indx]
    elif stripped:
        return stripped
    return ""


def process_b(line: str, target_char: str) -> str:
    """Return text from one character after the first marker, or all text."""
    stripped = line.strip()
    if stripped and target_char in stripped:
        indx = stripped.index(target_char)
        return stripped[indx + 1 :]
    elif stripped:
        return stripped
    return ""


if __name__ == "__main__":
    mode = sys.argv[1]
    file_name = Path(sys.argv[2])
    target_char = sys.argv[3]
    nl: list[str] = []
    for line in read_lines(file_name):
        if mode == "-a":
            result = process_a(line, target_char)
        else:
            result = process_b(line, target_char)
        if result:
            nl.append(result)
    if nl:
        file_name.write_text("\n".join(nl), encoding="utf-8")
