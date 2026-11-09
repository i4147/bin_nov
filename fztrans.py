#!/data/data/com.termux/files/usr/bin/env python
"""Offline Persian to English (and back) dictionary lookup.
Searches ``~/dic.json`` for a word, lists the entries by ``--prefix``, does a typo-tolerant ``--fuzzy`` search and, unless ``--no-fzf`` is given, lets fzf pick among the candidates; the results are printed with the Persian shaping of ``faprint``.
Usage: fztrans WORD [--prefix P] [--fuzzy] [--no-fzf] Positional arguments: word ...
Word to translate Options: --prefix List words starting with prefix --fuzzy Fuzzy search (typo tolerant) --no-fzf Disable interactive fzf selector Examples: $ fztrans کتاب --fuzzy Command-line script: run ``python fztrans.py`` or the ``fztrans`` command.
The module is importable as well."""

import argparse
from difflib import get_close_matches
import json
from pathlib import Path
import readline
import shutil
import subprocess
import sys
from typing import Final, Iterable

from faprint import faprint
from loguru import logger


DICT_FILE: Final[str] = Path("~/dic.json").expanduser()


def load_dictionary(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        logger.error("Error: Dictionary file {} not found", path)
        sys.exit(1)
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        fa_en = {str(k).strip(): str(v).strip() for k, v in data.items()}
        en_fa = {v: k for k, v in fa_en.items()}
        return (fa_en, en_fa)
    except Exception as e:
        logger.error("Error loading dictionary: {}", e)
        sys.exit(1)


def setup_readline(words: Iterable[str]) -> None:
    sorted_words = sorted(words)

    def completer(text: str, state: int) -> str | None:
        matches = [w for w in sorted_words if w.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n")


def translate(word: str, fa_en: dict[str, str], en_fa: dict[str, str]) -> str | None:
    return fa_en.get(word) or en_fa.get(word)


def fuzzy_search(word: str, all_words: set[str], limit: int = 5, cutoff: float = 0.6) -> list[str]:
    return get_close_matches(word, all_words, n=limit, cutoff=cutoff)


def fzf_select(all_words: set[str]) -> str | None:
    if not shutil.which("fzf"):
        logger.warning("fzf not found in PATH. Falling back to normal mode.")
        return None
    try:
        proc = subprocess.run(
            [
                "fzf",
                "--prompt=Select word: ",
                "--height=40%",
                "--layout=reverse",
                "--border",
            ],
            input="\n".join(sorted(all_words)),
            text=True,
            capture_output=True,
            check=False,
        )
        selection = proc.stdout.strip()
        return selection or None
    except Exception as e:
        logger.error("fzf execution error: {}", e)
        return None


def interactive_mode(fa_en: dict[str, str], en_fa: dict[str, str]) -> None:
    all_words = set(fa_en) | set(en_fa)
    setup_readline(all_words)
    print("\n🌐 Offline Persian ↔ English Translator")
    print("⌨  TAB for suggestions, Ctrl+C to exit\n")
    while True:
        try:
            word = input("> ").strip()
            if not word:
                continue
            result = translate(word, fa_en, en_fa)
            if result:
                print(f"✅ {result}")
            else:
                matches = fuzzy_search(word, all_words)
                if matches:
                    print(f"❓ Not found. Did you mean: {', '.join(matches)}?")
                else:
                    print("❌ Not found")
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Bye.")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Persian ↔ English translator")
    parser.add_argument("word", nargs="*", help="Word to translate")
    parser.add_argument("--prefix", help="List words starting with prefix")
    parser.add_argument("--fuzzy", help="Fuzzy search (typo tolerant)")
    parser.add_argument("--no-fzf", action="store_true", help="Disable interactive fzf selector")
    args = parser.parse_args()
    fa_en, en_fa = load_dictionary(Path(DICT_FILE))
    all_words = set(fa_en) | set(en_fa)
    if not args.no_fzf and (not args.word) and (not args.prefix) and (not args.fuzzy):
        selected = fzf_select(all_words)
        if selected:
            result = translate(selected, fa_en, en_fa)
            print(f"{selected} → {result}")
            sys.exit(0)
        elif shutil.which("fzf"):
            sys.exit(0)
    if args.prefix:
        matches = sorted(w for w in all_words if w.startswith(args.prefix))
        if matches:
            print("\n".join(matches))
            sys.exit(0)
        print("No matches found for prefix: %s", args.prefix)
        sys.exit(1)
    if args.fuzzy:
        matches = fuzzy_search(args.fuzzy, all_words)
        if matches:
            print("\n".join(matches))
            sys.exit(0)
        print("No close matches found for: %s", args.fuzzy)
        sys.exit(1)
    if args.word:
        word = " ".join(args.word).strip()
        result = translate(word, fa_en, en_fa)
        if result:
            faprint(result)
            sys.exit(0)
        else:
            matches = fuzzy_search(word, all_words)
            if matches:
                faprint(f"Not found. Did you mean: {', '.join(matches)}?", file=sys.stderr)
            else:
                print("Not found", file=sys.stderr)
            sys.exit(1)
    interactive_mode(fa_en, en_fa)


if __name__ == "__main__":
    raise SystemExit(main())
