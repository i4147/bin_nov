#!/data/data/com.termux/files/usr/bin/env python
"""Find the installed distributions that depend on a given package.
Scans the ``METADATA`` files of the user ``site-packages`` for a ``Requires-Dist:`` line mentioning the package name and prints the distribution directory names that match.
Usage: find_req PACKAGE Examples: $ find_req requests The scan root is the module constant ``cwd``; adjust it for other Python versions."""

from pathlib import Path
import sys


def process_file(path: Path, text: str) -> None:
    path = Path(path)
    content = path.read_text().lower()
    target1 = "requires-dist: " + text
    if target1 in content:
        print(path.parent.name)


if __name__ == "__main__":
    cwd = Path("/data/data/com.termux/files/home/.local/lib/python3.14/site-packages")
    target = sys.argv[1]
    for path in cwd.rglob("METADATA"):
        process_file(path, target)
