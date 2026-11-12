#!/data/data/com.termux/files/usr/bin/env python
"""Print a random quote from ``/sdcard/data/quotes/quotes.json``.
Reads the JSON list of ``{"quote", "author"}`` objects, picks one at random and prints it in a box that spans the terminal width, with the quote and the author in different colours.
Usage: rquote Examples: $ rquote Runs as a script (``python rquote.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import random
from shutil import get_terminal_size

FILE_NAME: str = "/sdcard/data/quotes/quotes.json"


def display_random_quote() -> None:
    if not Path(FILE_NAME).exists():
        return
    with open(FILE_NAME, "r", encoding="utf-8") as f:
        try:
            quotes = json.load(f)
        except json.JSONDecodeError:
            return
    if not quotes:
        return
    selected = random.choice(quotes)
    quote_text = selected.get("quote", "No quote content.")
    author_text = selected.get("author", "Unknown Author")
    N = get_terminal_size()[0]
    print("\n" + "─" * N)
    print(f'\033[5;96m"{quote_text}"\033[0m')
    print(f"\033[5;94m  — {author_text}\033[0m")
    print("─" * N + "\n")


if __name__ == "__main__":
    display_random_quote()
