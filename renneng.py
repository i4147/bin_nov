#!/data/data/com.termux/files/usr/bin/env python
"""Translate the non-English file and directory names of the current directory to English.
Walks the tree bottom-up, translates every name that contains non-ASCII characters with Google Translate and renames the entry, adding a numeric suffix when the translated name already exists.
Usage: renneng Examples: $ renneng Runs as a script (``python renneng.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import re

from deep_translator import GoogleTranslator


DIRECTORY = "."
non_english_pattern = re.compile(r"[^\x00-\x7F]")


def translate_if_needed(name: str) -> str:
    base, ext = (str(Path(name).with_suffix("")), Path(name).suffix)
    if not non_english_pattern.search(base):
        return name
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(base)
        return translated + ext
    except Exception as e:
        print(f"Translation error for '{name}': {e}")
        return name


def get_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.stem
    ext = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new_path = parent / f"{base}_{counter}{ext}"
        if not new_path.exists():
            return new_path
        counter += 1


def rename_files(directory: str) -> None:
    for dirpath, dirnames, filenames in os.walk(directory, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            new_name = translate_if_needed(path.name)
            if new_name == path.name:
                continue
            new_path = path.parent / new_name
            new_path = get_unique_path(new_path)
            Path(path).rename(new_path)
            print(f"File renamed: {path.name} -> {new_path.name}")
        for name in dirnames:
            path = Path(dirpath) / name
            new_name = translate_if_needed(path.name)
            if new_name == path.name:
                continue
            new_path = path.parent / new_name
            new_path = get_unique_path(new_path)
            Path(path).rename(new_path)
            print(f"Directory renamed: {path.name} -> {new_path.name}")


if __name__ == "__main__":
    rename_files(DIRECTORY)
