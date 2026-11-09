#!/data/data/com.termux/files/usr/bin/env python
"""Detect and optionally autofix misspelled words in a file using parallel processing."""

import argparse
from multiprocessing import Pool, cpu_count
import re

from dh import MAX_WORKERS
from spellchecker import SpellChecker


def process_line(line: str, autofix: bool = False) -> tuple:
    spell = SpellChecker()
    misspelled_count = 0
    fixed_count = 0

    def check_and_replace(match: re.Match[str]):
        nonlocal misspelled_count, fixed_count
        word = match.group(0)
        clean_word = word.strip("'")
        if not clean_word.isalpha():
            return word
        if clean_word.lower() not in spell:
            misspelled_count += 1
            if autofix:
                correction = spell.correction(clean_word.lower())
                if not correction:
                    return word
                if clean_word.istitle():
                    corrected_word = correction.capitalize()
                elif clean_word.isupper():
                    corrected_word = correction.upper()
                else:
                    corrected_word = correction
                if word.startswith("'"):
                    corrected_word = "'" + corrected_word
                if word.endswith("'"):
                    corrected_word = corrected_word + "'"
                fixed_count += 1
                return corrected_word
            else:
                candidates = spell.candidates(clean_word.lower())
                suggestions = ", ".join(candidates) if candidates else "No suggestions"
                print(f"Misspelled: '{word}' | Suggestions: {suggestions}")
                return word
        return word

    updated_line = re.sub(r"[a-zA-Z']+", check_and_replace, line)
    return updated_line, misspelled_count, fixed_count


def process_file(path: str, autofix: bool = False, num_processes: int | None = None) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Error: The file '{path}' was not found.")
        return
    except Exception as e:
        print(f"Error reading file: {e}")
        return
    if num_processes is None:
        num_processes = min(cpu_count(), len(lines))
    print(f"Processing {len(lines)} lines using {num_processes} processes...")
    args = [(line, autofix) for line in lines]
    try:
        with Pool(processes=num_processes) as pool:
            results = pool.starmap(process_line, args)
    except Exception as e:
        print(f"Error during multiprocessing: {e}")
        return
    updated_lines = []
    total_misspelled = 0
    total_fixed = 0
    for updated_line, misspelled, fixed in results:
        updated_lines.append(updated_line)
        total_misspelled += misspelled
        total_fixed += fixed
    if autofix:
        if total_fixed > 0:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(updated_lines)
                print(f"\nAutofixed {total_fixed} misspelled word(s) in '{path}'.")
                if total_misspelled > total_fixed:
                    skipped = total_misspelled - total_fixed
                    print(f"({skipped} misspelled word(s) had no suggestions and were skipped.)")
            except Exception as e:
                print(f"Error writing to file: {e}")
        elif total_misspelled == 0:
            print("No misspelled words found to autofix.")
        else:
            print("Found misspelled words, but no automatic corrections were available.")
    elif total_misspelled == 0:
        print("No misspelled words found.")
    else:
        print(f"\nFound {total_misspelled} misspelled word(s). Run with -a to autofix.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Detect and optionally autofix misspelled words in a file using parallel processing."
    )
    parser.add_argument("file", help="Path to the text file to check")
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically correct misspelled words in the file",
    )
    args = parser.parse_args()
    process_file(args.file, args.autofix, MAX_WORKERS)
