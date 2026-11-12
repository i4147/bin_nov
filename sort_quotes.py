#!/data/data/com.termux/files/usr/bin/env python
"""Deduplicate a quotes JSON file and sort it by author.
Loads the list of ``{"quote", "author"}`` objects, drops the duplicate quotes (compared case-insensitively) and writes the list back sorted by author.
Usage: sort_quotes QUOTES.json Examples: $ sort_quotes quotes.json Runs as a script (``python sort_quotes.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys
from typing import Any, Iterable


def dedup_quotes(quotes: Iterable[Any]):
    seen = set()
    unique = []
    for q in quotes:
        key = q["quote"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def sort_quotes_by_author(path: Path) -> None:
    if not Path(path).exists():
        print(f"Error: '{path}' could not be found.")
        return
    with open(path, "r", encoding="utf-8") as f:
        try:
            quotes = json.load(f)
        except json.JSONDecodeError:
            print("Error: 'quotes.json' is empty or contains invalid formatting.")
            return
    uniques = dedup_quotes(quotes)
    uniques.sort(key=lambda item: item.get("author", "").lower())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(uniques, f, indent=2, ensure_ascii=False)
    print("Success: Sorted")


if __name__ == "__main__":
    fn = sys.argv[1]
    sort_quotes_by_author(fn)
