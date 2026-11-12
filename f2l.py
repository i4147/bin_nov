#!/data/data/com.termux/files/usr/bin/env python
"""Turn a one-item-per-line file into a Python set literal and copy it to the clipboard.
Quotes each line (choosing the quote character that does not clash with the content) and writes ``{"a", "b", ...}`` back to the file, then sends the text to ``termux-clipboard-set`` when that command is available.
Usage: f2l FILE Examples: $ f2l words.txt Runs as a script (``python f2l.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import subprocess
import sys


def main() -> None:
    fn = sys.argv[1]
    path = Path(fn)
    with path.open(encoding="utf-8") as f:
        lines = [line.strip() for line in f]
    items = []
    for line in lines:
        quote_char = "'" if '"' in line else '"'
        items.append(f"{quote_char}{line}{quote_char}")
    formatted_content = "{" + ", ".join(items) + "}"
    path.write_text(formatted_content, encoding="utf-8")
    try:
        subprocess.run(
            ["termux-clipboard-set"],
            input=formatted_content,
            text=True,
            capture_output=True,
        )
        print(f"✓ Updated and copied: {path}")
    except FileNotFoundError:
        print(f"✓ File updated: {path}")
        print("⚠ Install termux-api for clipboard support")


if __name__ == "__main__":
    raise SystemExit(main())
