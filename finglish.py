#!/data/data/com.termux/files/usr/bin/env python
"""Rename Persian file names below the current directory to Finglish (Latin script).
Maps the Persian letters to their usual Latin transliterations, normalises the result with Unicode NFKD and renames the files that contain non-ASCII characters.
Usage: finglish Examples: $ finglish Runs as a script (``python finglish.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import unicodedata


def custom_persian_to_finglish(text: str) -> str:
    persian_map = {
        "ا": "a",
        "آ": "a",
        "ب": "b",
        "پ": "p",
        "ت": "t",
        "ث": "s",
        "ج": "j",
        "چ": "ch",
        "ح": "h",
        "خ": "kh",
        "د": "d",
        "ذ": "z",
        "ر": "r",
        "ز": "z",
        "ژ": "zh",
        "س": "s",
        "ش": "sh",
        "ص": "s",
        "ض": "z",
        "ط": "t",
        "ظ": "z",
        "ع": "a",
        "غ": "gh",
        "ف": "f",
        "ق": "gh",
        "ک": "k",
        "گ": "g",
        "ل": "l",
        "م": "m",
        "ن": "n",
        "ه": "h",
    }
    words = text.split(" ")
    processed_words = []
    for word in words:
        if not word:
            processed_words.append("")
            continue
        processed_word = ""
        chars = list(word)
        for i, char in enumerate(chars):
            if char == "و":
                if i == 0:
                    processed_word += "v"
                else:
                    processed_word += "o"
            elif char == "ی":
                if i == 0 or i == len(chars) - 1:
                    processed_word += "y"
                else:
                    processed_word += "i"
            else:
                processed_word += persian_map.get(char, char)
        processed_words.append(processed_word)
    return "_".join(processed_words)


def convert_filenames_with_pathlib(directory: str = ".") -> None:
    start_path = Path(directory)
    for path in start_path.rglob("*"):
        original_filename_stem = path.stem
        original_extension = path.suffix
        normalized_stem = unicodedata.normalize("NFKD", original_filename_stem)
        finglish_stem = custom_persian_to_finglish(normalized_stem)
        finglish_stem_cleaned = "_".join(filter(None, finglish_stem.split("_")))
        new_filename = finglish_stem_cleaned + original_extension
        if new_filename != path.name:
            new_path = path.with_name(new_filename)
            try:
                path.rename(new_path)
                print(f"Renamed: {path} -> {new_path}")
            except OSError as e:
                print(f"Error renaming {path}: {e}")


if __name__ == "__main__":
    convert_filenames_with_pathlib()
