#!/data/data/com.termux/files/usr/bin/env python
"""Delete the lines of a file that are shorter than a minimum length.
Every line whose stripped text is shorter than the threshold (10 characters by default) is dropped and the file is rewritten in place.
Usage: del10 FILE [MIN_LENGTH] Examples: $ del10 links.txt 20 Runs as a script (``python del10.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <filename>")
        sys.exit(1)
    fname = sys.argv[1]
    llen = int(str(sys.argv[2]).strip()) if len(sys.argv) == 3 else 10
    lines = []
    try:
        with Path(fname).open(encoding="utf-8") as f:
            lines = f.readlines()
        filtered = [line for line in lines if len(line.strip()) >= llen]
        with Path(fname).open("w", encoding="utf-8") as f:
            f.writelines(filtered)
    except FileNotFoundError:
        print(f"Error: File '{fname}' not found.")
    except Exception as e:
        print("An error occurred:", e)
