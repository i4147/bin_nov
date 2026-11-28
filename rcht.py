#!/data/data/com.termux/files/usr/bin/env python
"""Print a random cheatsheet from ~/.cheats, rendered as Markdown.

Picks a random file from the ~/.cheats tree and renders it to the terminal with rich; exits with an error when the directory or its files are missing.
"""

from __future__ import annotations
from pathlib import Path
import random
from secrets import randbelow
import sys

from dh import rainbow
from rich.console import Console
from rich.markdown import Markdown


def select_random_cheat() -> str:
    cheat_dir = Path("~/.cheats").expanduser()

    if not cheat_dir.exists() or not cheat_dir.is_dir():
        print(f"Error: The directory '{cheat_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    cheat_files = [f for f in cheat_dir.rglob("*") if f.is_file()]

    if not cheat_files:
        print(f"Error: No cheat files found in '{cheat_dir}'.", file=sys.stderr)
        sys.exit(1)

    selected_cheat = random.choice(cheat_files)

    try:
        return selected_cheat.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"Error reading '{selected_cheat}': {e}", file=sys.stderr)
        sys.exit(1)


def rich_print(content: str) -> None:

    console = Console()
    console.print(Markdown(content, code_theme="monokai"))


def rainbow_cheat(text: str) -> None:
    rainbow(text)


if __name__ == "__main__":
    choosen = randbelow(10_000)
    txt = select_random_cheat()
    if choosen % 2:
        rich_print(txt)
    else:
        rainbow_cheat(txt)
