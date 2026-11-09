#!/data/data/com.termux/files/usr/bin/env python
"""Keep lines containing mkv or mp4 and a selected resolution in a file.
Pass a file path as the first argument.
The selected string is 480 if it appears anywhere in the file, otherwise 720.
Rewrite only if lines match."""

from pathlib import Path
import sys


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    content = fn.read_text(encoding="utf-8")
    lines = content.splitlines()
    lowest = "480" if "480" in content else "720"
    nl = [line for line in lines if line.strip() and ("mkv" in line or "mp4" in line) and (lowest in line)]
    if nl:
        fn.write_text("\n".join(nl), encoding="utf-8")
    print(f"{len(nl)} links found.")
