#!/data/data/com.termux/files/usr/bin/env python
"""Fix typos in text files with learned substitution patterns.
Learns the keyboard-adjacency and common-substitution patterns from a JSON database, proposes the corrections for the files of a directory and can apply them, optionally asking for confirmation and learning from each decision; ``--show-patterns`` dumps what was learned.
Usage: autofix_typo [--apply] [--interactive] [--dir DIR] [--db FILE] Options: --apply Actually apply fixes --interactive Interactive mode (learn from each fix) --dir Directory to process --db Learning database file --show-patterns Show learned patterns and exit Examples: $ autofix_typo --dir notes/ --interactive Command-line script: run ``python autofix_typo.py`` or the ``autofix_typo`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Sequence

from dh import PY_KEYWORDS

COMMON_SUBSTITUTIONS: dict[str, str] = {
    "0": "p",
    "1": "q",
    "2": "w",
    "3": "e",
    "4": "r",
    "5": "t",
    "6": "y",
    "7": "u",
    "8": "i",
    "9": "o",
    "a": "s",
    "b": "n",
    "e": "3",
    "y": "6",
    "l": "p",
    "n": "b",
    "s": "a",
    "t": "y",
    "u": "i",
    "z": "x",
}
QWERTY_ADJACENT: dict[str, str] = {
    "q": "w",
    "w": "qe",
    "e": "wr",
    "r": "et",
    "t": "ry",
    "y": "tu",
    "u": "yi",
    "i": "uo",
    "o": "ip",
    "p": "o",
    "a": "s",
    "s": "ad",
    "d": "sf",
    "f": "dg",
    "g": "fh",
    "h": "gj",
    "j": "hk",
    "k": "jl",
    "l": "k",
    "z": "x",
    "x": "zc",
    "c": "xv",
    "v": "cb",
    "b": "vn",
    "n": "bm",
    "m": "n",
}


class PatternLearner:
    def __init__(self, learning_db_path: str = "typo_patterns.json") -> None:
        self.learning_db_path = learning_db_path
        self.substitution_patterns = dict(COMMON_SUBSTITUTIONS)
        self.learned_corrections = {}
        self.error_frequency = defaultdict(int)
        self.context_rules = []
        self._load_learning_db()

    def _load_learning_db(self) -> None:
        if Path(self.learning_db_path).exists():
            try:
                with open(self.learning_db_path) as f:
                    data = json.load(f)
                    self.learned_corrections = data.get("corrections", {})
                    self.error_frequency.update(data.get("frequencies", {}))
                    self.context_rules = data.get("context_rules", [])
                print(
                    f"Loaded {len(self.learned_corrections)} learned corrections",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"Error loading learning DB: {e}", file=sys.stderr)

    def save(self) -> None:
        data = {
            "corrections": self.learned_corrections,
            "frequencies": dict(self.error_frequency),
            "context_rules": self.context_rules,
            "last_updated": datetime.now().isoformat(),
        }
        with open(self.learning_db_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved learning patterns to {self.learning_db_path}", file=sys.stderr)

    def apply_substitutions(self, word: str) -> str:
        candidates = []
        if word in self.learned_corrections:
            candidates.append((self.learned_corrections[word], 1.0))
        for i, char in enumerate(word):
            if char in self.substitution_patterns:
                corrected = word[:i] + self.substitution_patterns[char] + word[i + 1 :]
                candidates.append((corrected, 0.9))
        multi_corrected = word
        multi_changes = 0
        for i, char in enumerate(word):
            if char in self.substitution_patterns:
                multi_corrected = multi_corrected[:i] + self.substitution_patterns[char] + multi_corrected[i + 1 :]
                multi_changes += 1
        if multi_changes > 0 and multi_corrected != word:
            candidates.append((multi_corrected, 0.8 - multi_changes * 0.1))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
        return word

    def learn_from_correction(self, wrong: str, correct: str) -> None:
        if wrong == correct:
            return
        self.learned_corrections[wrong] = correct
        self.error_frequency[wrong] += 1
        if len(wrong) == len(correct):
            for w_char, c_char in zip(wrong, correct, strict=False):
                if w_char != c_char:
                    pattern = f"{w_char}→{c_char}"
                    self.error_frequency[pattern] += 1
                    if self.error_frequency[pattern] > 2:
                        self.substitution_patterns[w_char] = c_char
                        print(
                            f"  Learned pattern: '{w_char}' → '{c_char}'",
                            file=sys.stderr,
                        )
        elif len(wrong) == len(correct) + 1:
            for i in range(len(wrong)):
                if wrong[:i] + wrong[i + 1 :] == correct:
                    pattern = f"delete '{wrong[i]}'"
                    self.error_frequency[pattern] += 1
        elif len(wrong) == len(correct) - 1:
            for i in range(len(correct)):
                if correct[:i] + correct[i + 1 :] == wrong:
                    pattern = f"insert '{correct[i]}'"
                    self.error_frequency[pattern] += 1
        self.save()


class TypoFixerWithLearning:
    def __init__(self, preview: bool = True, learning_db: str = "typo_patterns.json") -> None:
        self.preview = preview
        self.learner = PatternLearner(learning_db)
        self.changes_made = 0
        self.files_processed = 0
        self.interactive_mode = False
        self.valid_words = set()
        self._load_word_list()
        self.valid_words.update(PY_KEYWORDS)

    def _load_word_list(self) -> None:
        try:
            import nltk
            from nltk.corpus import words

            try:
                nltk.data.find("corpora/words.zip")
            except LookupError:
                nltk.download("words", quiet=True)
            self.valid_words.update(w.lower() for w in words.words())
            print(f"Loaded {len(self.valid_words)} dictionary words", file=sys.stderr)
        except:
            print("Warning: Could not load NLTK words", file=sys.stderr)
            common = {
                "the",
                "be",
                "to",
                "of",
                "and",
                "a",
                "in",
                "that",
                "have",
                "i",
                "it",
                "for",
                "not",
                "on",
                "with",
                "he",
                "as",
                "you",
                "do",
                "at",
            }
            self.valid_words.update(common)

    def is_valid_word(self, word: str) -> bool:
        if len(word) < 3:
            return True
        if word.isupper() and 2 <= len(word) <= 5:
            return True
        return word.lower() in self.valid_words

    def suggest_correction(self, word: str, context: str = "") -> str | None:
        if self.is_valid_word(word):
            return None
        pattern_corrected = self.learner.apply_substitutions(word)
        if pattern_corrected != word and self.is_valid_word(pattern_corrected):
            return pattern_corrected
        for i, char in enumerate(word):
            if char in QWERTY_ADJACENT:
                for replacement in QWERTY_ADJACENT[char]:
                    corrected = word[:i] + replacement + word[i + 1 :]
                    if self.is_valid_word(corrected):
                        return corrected
        if word in self.learner.learned_corrections:
            return self.learner.learned_corrections[word]
        try:
            from difflib import get_close_matches

            matches = get_close_matches(word.lower(), self.valid_words, n=1, cutoff=0.75)
            if matches:
                if word[0].isupper():
                    return matches[0].capitalize()
                return matches[0]
        except:
            pass
        return None

    def interactive_fix(self, word: str, line: str) -> str:
        print(f"\nUnknown word: '{word}'")
        print(f"Context: {line.strip()}")
        suggestion = self.suggest_correction(word)
        if suggestion:
            print(f"Suggested: '{suggestion}'", file=sys.stderr)
        while True:
            choice = input(f"Fix '{word}'? [y(es)/n(o)/e(dit)/l(earn)]: ").lower()
            if choice == "y" and suggestion:
                self.learner.learn_from_correction(word, suggestion)
                return suggestion
            elif (choice == "y" and not suggestion) or choice == "e":
                manual = input("Enter correction: ")
                self.learner.learn_from_correction(word, manual)
                return manual
            elif choice == "l":
                print(f"Learning '{word}' as valid word")
                self.valid_words.add(word.lower())
                self.learner.learned_corrections[word] = word
                return word
            elif choice == "n":
                return word

    def fix_file(self, path: Path) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"  Error reading {path}: {e}", file=sys.stderr)
            return False
        fixed_lines = []
        changes = 0
        word_pattern = re.compile(r"\b([a-zA-Z]+(?:[-\'][a-zA-Z]+)*)\b")
        for line_num, line in enumerate(lines, 1):
            fixed_line = line

            def replace_word(match: re.Match[str]):
                nonlocal changes
                word = match.group(1)
                if len(word) <= 2 or word.isupper():
                    return word
                if not self.is_valid_word(word):
                    correction = self.suggest_correction(word, line)
                    if correction:
                        changes += 1
                        if self.preview:
                            print(
                                f"  Line {line_num}: '{word}' → '{correction}'",
                                file=sys.stderr,
                            )
                        return correction
                    elif self.interactive_mode:
                        correction = self.interactive_fix(word, line)
                        if correction != word:
                            changes += 1
                        return correction
                return word

            fixed_line = word_pattern.sub(replace_word, line)
            fixed_lines.append(fixed_line)
        if changes > 0 and not self.preview:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(fixed_lines)
            print(f"  Fixed {changes} typo(s) in {path}", file=sys.stderr)
        elif changes > 0 and self.preview:
            print(f"  Would fix {changes} typo(s) in {path}", file=sys.stderr)
        self.changes_made += changes
        return changes > 0

    def process_directory(self, cwd: str) -> None:
        root_path = Path(cwd)
        extensions = {".md", ".py", ".toml", ".json", ".html", ".css", ".js", ".txt"}
        for ext in extensions:
            for path in root_path.rglob(f"*{ext}"):
                if path.is_file():
                    self.files_processed += 1
                    print(f"\nProcessing: {path}", file=sys.stderr)
                    self.fix_file(path)
        print(f"\nSummary: Processed {self.files_processed} files", file=sys.stderr)
        print(
            f"Active patterns: {len(self.learner.substitution_patterns)}",
            file=sys.stderr,
        )
        print(
            f"Learned corrections: {len(self.learner.learned_corrections)}",
            file=sys.stderr,
        )
        if self.changes_made > 0:
            self.learner.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-fix typos with pattern learning")
    parser.add_argument("--apply", action="store_true", help="Actually apply fixes")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive mode (learn from each fix)",
    )
    parser.add_argument("--dir", type=str, default=".", help="Directory to process")
    parser.add_argument("--db", type=str, default="typo_patterns.json", help="Learning database file")
    parser.add_argument("--show-patterns", action="store_true", help="Show learned patterns and exit")
    args = parser.parse_args()
    if args.show_patterns:
        learner = PatternLearner(args.db)
        print(f"\nLearned Corrections ({len(learner.learned_corrections)}):")
        for wrong, correct in sorted(learner.learned_corrections.items()):
            print(f"  {wrong} → {correct} (seen {learner.error_frequency[wrong]} times)")
        print(f"\nActive Substitution Patterns ({len(learner.substitution_patterns)}):")
        for wrong, correct in sorted(learner.substitution_patterns.items()):
            if wrong in COMMON_SUBSTITUTIONS:
                print(f"  {wrong} → {correct} (default)")
            else:
                print(f"  {wrong} → {correct} (learned)")
        return
    fixer = TypoFixerWithLearning(preview=not args.apply, learning_db=args.db)
    fixer.interactive_mode = args.interactive
    fixer.process_directory(args.dir)


if __name__ == "__main__":
    raise SystemExit(main())
