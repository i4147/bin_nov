#!/data/data/com.termux/files/usr/bin/env python
"""Detect and optionally fix the misspelled words of a text file with Hunspell.
Checks each line against the Hunspell dictionary (``-d``/``--affix``, or the ones it discovers with ``--list-dicts``) in a process pool, prints the unknown words and with ``-a`` replaces them with the first suggestion.
Usage: hunspeller FILE [-a] [-d DIC] [--affix AFF] [--list-dicts] Positional arguments: file Path to the text file to check Options: -a, --autofix Automatically correct misspelled words in the file -d, --dictionary Path to Hunspell dictionary file (.dic) --affix Path to Hunspell affix file (.aff) --list-dicts List available Hunspell dictionaries and exit Examples: $ hunspeller notes.txt -a Command-line script: run ``python hunspeller.py`` or the ``hunspeller`` command.
The module is importable as well."""

import argparse
from multiprocessing import Pool, cpu_count
from pathlib import Path
import re
import sys

from dh import MAX_WORKERS
import hunspell


def process_line(line: str, autofix: bool = False) -> tuple:
    h = hunspell.HunSpell("/usr/share/hunspell/en_US.dic", "/usr/share/hunspell/en_US.aff")
    misspelled_count = 0
    fixed_count = 0
    suggestions_dict = {}

    def check_and_replace(match: re.Match[str]):
        nonlocal misspelled_count, fixed_count
        word = match.group(0)
        clean_word = word.strip("'")
        if not clean_word.isalpha():
            return word
        if not h.spell(clean_word):
            misspelled_count += 1
            suggestions = h.suggest(clean_word)
            if autofix and suggestions:
                correction = suggestions[0]
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
                if suggestions:
                    suggestions_dict[word] = suggestions
                else:
                    suggestions_dict[word] = ["No suggestions"]
                return word
        return word

    updated_line = re.sub(r"[a-zA-Z']+", check_and_replace, line)
    return updated_line, misspelled_count, fixed_count, suggestions_dict


def process_file(
    path: str,
    autofix: bool = False,
    num_processes: int | None = None,
    dic_path: str = "/usr/share/hunspell/en_US.dic",
    aff_path: str = "/usr/share/hunspell/en_US.aff",
) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Error: The file '{path}' was not found.")
        return
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    if not Path(dic_path).exists() or not Path(aff_path).exists():
        print(f"Error: Hunspell dictionary files not found at:")
        print(f"  Dictionary: {dic_path}")
        print(f"  Affix file: {aff_path}")
        print("Please install hunspell dictionaries or specify correct paths.")
        return
    if num_processes is None:
        num_processes = min(cpu_count(), len(lines))
    num_processes = max(1, num_processes)
    print(f"Processing {len(lines)} lines using {num_processes} processes...")
    print(f"Using Hunspell dictionary: {dic_path}")
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
    all_suggestions = {}
    for updated_line, misspelled, fixed, suggestions in results:
        updated_lines.append(updated_line)
        total_misspelled += misspelled
        total_fixed += fixed
        all_suggestions.update(suggestions)
    if autofix:
        if total_fixed > 0:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(updated_lines)
                print(f"\n✓ Autofixed {total_fixed} misspelled word(s) in '{path}'.")
                if total_misspelled > total_fixed:
                    skipped = total_misspelled - total_fixed
                    print(f"  ({skipped} misspelled word(s) had no suggestions and were skipped.)")
                    no_suggestion_words = [
                        word for word, suggs in all_suggestions.items() if suggs == ["No suggestions"]
                    ]
                    if no_suggestion_words:
                        print(f"\n  Words with no suggestions: {', '.join(no_suggestion_words[:10])}")
                        if len(no_suggestion_words) > 10:
                            print(f"  ... and {len(no_suggestion_words) - 10} more")
            except Exception as e:
                print(f"Error writing to file: {e}")
        elif total_misspelled == 0:
            print("✓ No misspelled words found to autofix.")
        else:
            print("Found misspelled words, but no automatic corrections were available.")
            print("\nWords and their suggestions:")
            for word, suggs in all_suggestions.items():
                if suggs != ["No suggestions"]:
                    print(f"  '{word}' → {', '.join(suggs[:5])}")
    elif total_misspelled == 0:
        print("✓ No misspelled words found.")
    else:
        print(f"\n✗ Found {total_misspelled} misspelled word(s):\n")
        for word in sorted(all_suggestions.keys()):
            suggestions = all_suggestions[word]
            if suggestions == ["No suggestions"]:
                print(f"  '{word}' → No suggestions available")
            else:
                sugg_str = ", ".join(suggestions[:5])
                if len(suggestions) > 5:
                    sugg_str += f", ... (+{len(suggestions) - 5} more)"
                print(f"  '{word}' → {sugg_str}")
        print(f"\nRun with -a to autofix {total_misspelled} word(s).")


def find_hunspell_dicts():
    import glob

    common_paths = [
        "/usr/share/hunspell/",
        "/usr/share/myspell/",
        "/usr/local/share/hunspell/",
        "/data/data/com.termux/files/usr/share/hunspell/",
    ]
    found_dicts = []
    for path in common_paths:
        if Path(path).exists():
            dic_files = glob.glob(Path(path).joinpath("*.dic"))
            for dic_file in dic_files:
                aff_file = dic_file.replace(".dic", ".aff")
                if Path(aff_file).exists():
                    found_dicts.append((dic_file, aff_file))
    return found_dicts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Detect and optionally autofix misspelled words in a file using Hunspell with parallel processing.",
        epilog="Example: %(prog)s file.txt -a -p 4",
    )
    parser.add_argument("file", help="Path to the text file to check")
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Automatically correct misspelled words in the file",
    )
    parser.add_argument(
        "-d",
        "--dictionary",
        help="Path to Hunspell dictionary file (.dic)",
    )
    parser.add_argument(
        "--affix",
        help="Path to Hunspell affix file (.aff)",
    )
    parser.add_argument(
        "--list-dicts",
        action="store_true",
        help="List available Hunspell dictionaries and exit",
    )
    args = parser.parse_args()
    if args.list_dicts:
        dicts = find_hunspell_dicts()
        if dicts:
            print("Available Hunspell dictionaries:")
            for dic, _aff in dicts:
                lang = Path(dic).name.replace(".dic", "")
                print(f"  {lang}: {dic}")
        else:
            print("No Hunspell dictionaries found in common locations.")
        sys.exit(0)
    if args.dictionary and args.affix:
        dic_path = args.dictionary
        aff_path = args.affix
    elif args.dictionary:
        dic_path = args.dictionary
        aff_path = args.dictionary.replace(".dic", ".aff")
    else:
        dicts = find_hunspell_dicts()
        if dicts:
            dic_path, aff_path = dicts[0]
        else:
            dic_path = "/usr/share/hunspell/en_US.dic"
            aff_path = "/usr/share/hunspell/en_US.aff"
    process_file(args.file, args.autofix, MAX_WORKERS, dic_path, aff_path)
