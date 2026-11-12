#!/data/data/com.termux/files/usr/bin/env python
"""Print the number of characters in a text file.
Reads the file as UTF-8 and reports ``len(text)``; a missing file prints an error and exits with status 1.
Usage: count_chars FILE Examples: $ count_chars article.txt Runs as a script (``python count_chars.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python count_chars_of_input_file.py <input_file>")
        sys.exit(1)
    input_file = sys.argv[1]
    try:
        with Path(input_file).open(encoding="utf-8") as file:
            content = file.read()
            char_count = len(content)
            print(f"Number of characters in '{input_file}': {char_count}")
    except FileNotFoundError:
        print(f"Error: File '{input_file}' not found.")
        sys.exit(1)
