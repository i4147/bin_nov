#!/data/data/com.termux/files/usr/bin/env python
"""Join all non-empty lines of a file into a single line.
Strips the line endings, concatenates the remaining text and appends one final newline, rewriting the file in place.
Usage: joinlines FILE Examples: $ joinlines words.txt Runs as a script (``python joinlines.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
from sys import argv


def main() -> None:
    nl = ""
    with Path(argv[1]).open(encoding="utf-8") as f:
        lines = f.readlines()
        for line in lines:
            if line.strip():
                nl += line.strip("\n")
    Path(argv[1]).write_text(nl + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
