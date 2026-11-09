#!/data/data/com.termux/files/usr/bin/env python
"""Move a range of lines from one file to the end of another.
Appends lines ``start`` through ``end`` (one-based, inclusive) to the destination file and removes them from the source, both edited in place.
Usage: mvl SRC_FILE START_LINE END_LINE DEST_FILE Examples: $ mvl notes.txt 10 20 archive.txt Runs as a script (``python mvl.py``) in the directory it should act on; the module can also be imported."""

import sys


def move_lines(src_file: str, start_line: int, end_line: int, dest_file: str) -> None:
    try:
        with open(src_file, encoding="utf-8") as f:
            lines = f.readlines()
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        lines_to_move = lines[start_idx:end_idx]
        with open(dest_file, "a", encoding="utf-8") as f:
            f.writelines(lines_to_move)
        del lines[start_idx:end_idx]
        with open(src_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"Successfully moved lines {start_line}-{end_line} from {src_file} to {dest_file}.")
    except FileNotFoundError:
        print(f"Error: The file {src_file} was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: python move_lines.py <src_file> <start_line> <end_line> <dest_file>")
    else:
        src = sys.argv[1]
        start = int(sys.argv[2])
        end = int(sys.argv[3])
        dest = sys.argv[4]
        move_lines(src, start, end, dest)
