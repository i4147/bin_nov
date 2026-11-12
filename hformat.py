#!/data/data/com.termux/files/usr/bin/env python
"""Re-indent HTML files with BeautifulSoup's pretty printer.
Parses each file (the arguments, or every ``.html`` file below the current directory) and rewrites it with ``soup.prettify()`` only when the formatting changed.
Files are handled in parallel.
Usage: hformat [files ...] Examples: $ hformat index.html Runs as a script (``python hformat.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from bs4 import BeautifulSoup
from dh import get_files, mpf


def process_file(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8") as file:
            content = file.read()
        soup = BeautifulSoup(content, "html.parser")
        beautified_content = soup.prettify()
        if content != beautified_content:
            with open(path, "w", encoding="utf-8") as file:
                file.write(beautified_content)
    except Exception as e:
        print(f"Error beautifying HTML file {path}: {e}")
        return False
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    cwd = Path.cwd()
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".html"])
    mpf(process_file, files)
