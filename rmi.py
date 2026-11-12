#!/data/data/com.termux/files/usr/bin/env python
"""Remove invisible and bidirectional control characters from text files.
Strips zero-width spaces and joiners, non-breaking spaces, soft hyphens, the BOM and the bidi overrides from each text file (default: every non-binary file below the current directory) and reports how many characters went away.
Usage: rmi [files ...] Examples: $ rmi page.html Runs as a script (``python rmi.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import get_nobinary

INVISIBLE_CHARS: set[str] = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\xa0",
    "\xad",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def clean_text(text: str) -> str:
    cleaned = ""
    for c in text:
        if ord(c) == 8204:
            continue
        if c == "\n":
            cleaned += c
            continue
        if c in INVISIBLE_CHARS:
            continue
        cleaned += c
    return cleaned


def process_file(path: Path) -> None:
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    cleaned = clean_text(text)
    removed = len(text) - len(cleaned)
    if removed:
        print(f"{removed} invisible characters removed")
        path.write_text(cleaned, encoding="utf-8")
        return
    print("No invisible characters found")
    return


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_nobinary(cwd)
    for f in files:
        process_file(f)


if __name__ == "__main__":
    raise SystemExit(main())
