#!/data/data/com.termux/files/usr/bin/env python
"""Detect misspelled words in filenames recursively."""


import argparse
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import hunspell


DICT_PATHS: list[tuple[Any, ...]] = [
    (
        "/data/data/com.termux/files/usr/share/hunspell/en_US.dic",
        "/data/data/com.termux/files/usr/share/hunspell/en_US.aff",
    ),
    (
        "/data/data/com.termux/files/home/.local/share/hunspell/fa_IR.dic",
        "/data/data/com.termux/files/home/.local/share/hunspell/fa_IR.aff",
    ),
]
WORD_SPLIT_RE = re.compile(r"[^A-Za-z]+")
CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def load_spellchecker():
    for dic, aff in DICT_PATHS:
        if Path(dic).exists() and Path(aff).exists():
            return hunspell.HunSpell(dic, aff)
    print(
        "Error: could not find a hunspell dictionary (en_US.dic/.aff). "
        "Install one, e.g.: sudo apt-get install hunspell-en-us",
        file=sys.stderr,
    )
    sys.exit(1)


def split_words(stem: str):
    words = []
    for chunk in WORD_SPLIT_RE.split(stem):
        if not chunk:
            continue
        for sub in CAMEL_SPLIT_RE.split(chunk):
            if sub:
                words.append(sub)
    return words


def find_misspelled(words: list[Any], checker: Any):
    misspelled = []
    for w in words:
        if len(w) <= 2 or w.isdigit():
            continue
        if not checker.spell(w):
            misspelled.append(w)
    return misspelled


def suggest_fix(stem: str, misspelled_words: Iterable[Any], checker: Any) -> str:
    new_stem = stem
    for w in misspelled_words:
        suggestions = checker.suggest(w)
        if suggestions:
            best = suggestions[0]
            if w.isupper():
                best = best.upper()
            elif w[0].isupper():
                best = best.capitalize()
            new_stem = re.sub(re.escape(w), best, new_stem, count=1)
    return new_stem


def scan(root: Path, checker: Any, autofix: bool) -> None:
    found_any = False
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stem = path.stem
        words = split_words(stem)
        misspelled = find_misspelled(words, checker)
        if misspelled:
            found_any = True
            print(f"{path.name}")
            if autofix:
                new_stem = suggest_fix(stem, misspelled, checker)
                if new_stem != stem:
                    new_path = path.with_name(new_stem + path.suffix)
                    if new_path.exists():
                        print(
                            f"  [skip] target already exists: {new_path.name}",
                            file=sys.stderr,
                        )
                    else:
                        path.rename(new_path)
                        print(f"  -> renamed to: {new_path.name}")
    if not found_any:
        print("No misspelled filenames found.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect misspelled words in filenames recursively.")
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root directory to scan (default: current directory)",
    )
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically rename files using best spelling suggestions",
    )
    args = parser.parse_args()
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)
    checker = load_spellchecker()
    scan(root, checker, args.autofix)


if __name__ == "__main__":
    main()
