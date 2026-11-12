#!/data/data/com.termux/files/usr/bin/env python
"""Rename non-English filenames to English (translate or transliterate).
Analysis / deduplication ------------------------ - ``renm.py``, ``renneng.py`` and ``translate_noneng_filenames.py`` are the same program three times (Google Translate via ``deep-translator``, private ``fastwalk`` walking, unique-path disambiguation); later copies only dropped the cache/progress bar.
They are one ``translate`` command that caches translations, renames deepest paths first (so parent dirs can be renamed too), and is dry-run by default - the originals renamed immediately.
- ``finglish.py`` transliterates Persian filenames to Finglish (Latin) locally with no network -> ``finglish`` command.
Translation needs the optional ``deep-translator`` package and network access; transliteration is fully local.
Examples -------- python3 filename_translate_tool.py translate ~/Downloads python3 filename_translate_tool.py translate ~/Downloads --apply python3 filename_translate_tool.py finglish ~/Music --apply
"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import sys
import unicodedata

from dh import MAX_WORKERS

NON_ASCII: re.Pattern[str] = re.compile(r"[^\x00-\x7F]")

PERSIAN_MAP: dict[str, str] = {
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


def is_ascii_text(text: str) -> bool:
    """True when the text is pure ASCII (treated as 'English')."""
    return not NON_ASCII.search(text)


def sanitize(name: str, space_replacement: str) -> str:
    """Make a translated stem filesystem-friendly."""
    cleaned: str = name.strip()
    if space_replacement != " ":
        cleaned = cleaned.replace(" ", space_replacement)
    cleaned = re.sub(r"[/\\:*?\"<>|]", "_", cleaned)
    return cleaned.strip("._ ") or "renamed"


def unique_path(path: Path) -> Path:
    """Return *path* or ``name_N.suffix`` when it exists."""
    candidate: Path = path
    counter: int = 1
    while candidate.exists():
        candidate = path.parent / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    return candidate


def gather_paths(root: Path, include_dirs: bool) -> list[Path]:
    """All rename candidates, deepest first (safe for dir renames)."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() or (include_dirs and path.is_dir()):
            found.append(path)
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def execute_renames(plan: list[tuple[Path, Path]], apply: bool) -> int:
    """Print/apply rename pairs; return how many were applied/planned."""
    done: int = 0
    for src, dst in plan:
        target: Path = unique_path(dst) if apply else dst
        print(f"{'rename' if apply else 'plan'}: {src.name} -> {target.name}")
        if apply:
            try:
                src.rename(target)
                done += 1
            except OSError as exc:
                print(f"  error: {exc}", file=sys.stderr)
        else:
            done += 1
    if not apply and plan:
        print(f"Dry run: {len(plan)} rename(s) planned. Re-run with --apply.")
    return done


# --------------------------------------------------------------- translate
class Translator:
    """GoogleTranslator wrapper with a stem-level cache."""

    def __init__(self, source: str, target: str) -> None:
        try:
            from deep_translator import GoogleTranslator
        except ImportError:
            msg = "Missing optional dependency: pip install deep-translator"
            raise SystemExit(msg) from None
        self._translator: object = GoogleTranslator(source=source, target=target)
        self._cache: dict[str, str] = {}

    def translate_stem(self, stem: str) -> str:
        """Translate one filename stem (unchanged when ASCII/failing)."""
        if is_ascii_text(stem):
            return stem
        if stem not in self._cache:
            try:
                self._cache[stem] = str(self._translator.translate(stem))  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"warning: translation failed for {stem!r}: {exc}", file=sys.stderr)
                self._cache[stem] = stem
        return self._cache[stem]


def cmd_translate(args: argparse.Namespace) -> int:
    """Translate non-ASCII filenames to the target language."""
    translator: Translator = Translator(args.source_lang, args.target_lang)
    candidates: list[Path] = [p for p in gather_paths(args.root, args.include_dirs) if not is_ascii_text(p.name)]
    if not candidates:
        print("No non-English filenames found.")
        return 0
    unique_stems: list[str] = sorted({p.stem for p in candidates})
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        translated: dict[str, str] = dict(
            zip(unique_stems, pool.map(translator.translate_stem, unique_stems), strict=True)
        )
    plan: list[tuple[Path, Path]] = []
    for path in candidates:
        new_stem: str = sanitize(translated.get(path.stem, path.stem), args.space)
        new_name: str = new_stem + path.suffix
        if new_name != path.name:
            plan.append((path, path.with_name(new_name)))
    execute_renames(plan, args.apply)
    return 0


# ---------------------------------------------------------------- finglish
def persian_to_finglish(text: str) -> str:
    """Transliterate Persian text to Finglish (Latin letters)."""
    words_out: list[str] = []
    for word in text.split(" "):
        if not word:
            continue
        chars: list[str] = list(word)
        pieces: list[str] = []
        for index, char in enumerate(chars):
            if char == "و":
                pieces.append("v" if index == 0 else "o")
            elif char == "ی":
                pieces.append("y" if index in (0, len(chars) - 1) else "i")
            else:
                pieces.append(PERSIAN_MAP.get(char, char))
        words_out.append("".join(pieces))
    return "_".join(w for w in words_out if w)


def cmd_finglish(args: argparse.Namespace) -> int:
    """Transliterate Persian filenames to Finglish locally."""
    plan: list[tuple[Path, Path]] = []
    for path in gather_paths(args.root, args.include_dirs):
        normalized: str = unicodedata.normalize("NFKD", path.stem)
        new_stem: str = "_".join(filter(None, persian_to_finglish(normalized).split("_")))
        new_name: str = new_stem + path.suffix
        if new_stem and new_name != path.name:
            plan.append((path, path.with_name(new_name)))
    if not plan:
        print("Nothing to transliterate.")
        return 0
    execute_renames(plan, args.apply)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Rename non-English filenames.")
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("translate", help="Google-Translate filenames to English")
    tr.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    tr.add_argument("--source-lang", default="auto")
    tr.add_argument("--target-lang", default="en")
    tr.add_argument("--space", default=" ", metavar="CHAR", help="Replace spaces with CHAR (e.g. _)")
    tr.add_argument("--include-dirs", action="store_true", help="Also rename directories")
    tr.add_argument("--apply", action="store_true", help="Actually rename (default: dry run)")
    tr.set_defaults(handler=cmd_translate)
    fi = sub.add_parser("finglish", help="Persian filenames -> Finglish (local)")
    fi.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    fi.add_argument("--include-dirs", action="store_true")
    fi.add_argument("--apply", action="store_true")
    fi.set_defaults(handler=cmd_finglish)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
