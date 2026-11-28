#!/data/data/com.termux/files/usr/bin/env python
"""Display a random quote from /sdcard/data/quotes/quotes.json.

Renders the picked quote rainbow-wrapped in the terminal with the author line; silently does nothing when the file is missing, invalid or empty.
"""

import json
import random
from pathlib import Path
from shutil import get_terminal_size

from dh import cprint, rainbow

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
    rainbow("\n" + "─" * N + "\n")  # , color=(244, 179, 147))
    cprint(
        f"{quote_text if quote_text[-1] != '.' else quote_text[:-2]}",
        color=(62, 227, 240),
    )
    cprint(f" — {author_text}", color=(206, 236, 131))
    rainbow("─" * N + "\n")  # , color=(244, 179, 147))


if __name__ == "__main__":
    display_random_quote()
