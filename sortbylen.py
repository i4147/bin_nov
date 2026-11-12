#!/data/data/com.termux/files/usr/bin/env python
"""Sort the lines of a file by their length.
Reads the file keeping the line endings, sorts the lines by length (``-r`` for the longest first) and writes them back in place.
Usage: sortbylen [-r] FILE Examples: $ sortbylen words.txt -r Runs as a script (``python sortbylen.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import read_lines


def sort_by_length(lines: list[str], reverse: bool = False) -> list[str]:
    return sorted(lines, key=len, reverse=reverse)


if __name__ == "__main__":
    args = sys.argv[1:]
    reverse = False
    if "-r" in args:
        reverse = True
        args.remove("-r")
    if not args:
        print("Usage: python script.py [-r] <path>")
        sys.exit(1)
    path = Path(args[0].strip())
    lines = read_lines(path, ke=True)
    sorted_lines = sort_by_length(lines, reverse=reverse)
    path.write_text("".join(sorted_lines), encoding="utf8")
    print(f"{path.name} updated (reverse={reverse})")
