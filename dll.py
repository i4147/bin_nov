#!/data/data/com.termux/files/usr/bin/env python
"""Delete a range of lines from a file.
Without an end line everything from the start line to the end of the file is removed; line numbers are one-based and the file is rewritten in place, printing how many lines remained.
Usage: dll FILE START_LINE [END_LINE] Examples: $ dll notes.txt 10 20 Runs as a script (``python dll.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


def delete_lines_from_file() -> None:
    if len(sys.argv) not in (3, 4):
        msg = f"Usage: {sys.argv[0]} <file> <start_line> [end_line]"
        raise SystemExit(msg)
    filename = sys.argv[1]
    path = Path(filename)
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        fromline = int(sys.argv[2])
        toline = int(sys.argv[3]) if len(sys.argv) == 4 else len(lines)
    except ValueError:
        msg = "Line numbers must be integers"
        raise SystemExit(msg) from None
    if fromline < 1 or toline < fromline:
        msg = "Line range must start at 1 or greater and end on or after the start"
        raise SystemExit(msg)
    new_lines = lines[: fromline - 1] + lines[toline:]
    path.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"remained: {len(new_lines)} lines")


if __name__ == "__main__":
    delete_lines_from_file()
