#!/data/data/com.termux/files/usr/bin/env python
"""Transliterate non-ASCII file/dir names in place."""

import argparse
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any

from dh import SKIP_DIRS


try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None
try:
    import pykakasi

    _KAKASI = pykakasi.kakasi()
except ImportError:
    _KAKASI = None
try:
    from unidecode import unidecode
except ImportError:
    unidecode = None
MAX_NAME: int = 255
KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ebef]+")
FA_MARKS = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed]")
_RU: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
    "ў": "u",
}
RU_TABLE: dict[str, Any] = {}
for _k, _v in _RU.items():
    RU_TABLE[ord(_k)] = _v
    RU_TABLE[ord(_k.upper())] = _v.capitalize()
_FA: dict[str, str] = {
    "ا": "a",
    "آ": "a",
    "أ": "a",
    "إ": "e",
    "ء": "",
    "ئ": "",
    "ؤ": "",
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
    "ع": "",
    "غ": "gh",
    "ف": "f",
    "ق": "gh",
    "ک": "k",
    "ك": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "و": "v",
    "ه": "h",
    "ة": "h",
    "ی": "y",
    "ي": "y",
    "ى": "y",
    "\u200c": "_",
    "\u200d": "",
    "\u0640": "",
    "،": ",",
    "؛": ";",
    "؟": "",
}
for _i in range(10):
    _FA[chr(0x06F0 + _i)] = str(_i)
    _FA[chr(0x0660 + _i)] = str(_i)
FA_TABLE: dict[int, Any] = {ord(k): v for k, v in _FA.items()}
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED: set[Any] = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _japanese(text: str) -> str:
    parts = []
    for item in _KAKASI.convert(text):
        orig = item["orig"]
        parts.append(orig if orig.isascii() else item["hepburn"] + "_")
    return "".join(parts)


def _chinese(text: str) -> str:
    return HAN.sub(lambda m: "_" + "_".join(lazy_pinyin(m.group())) + "_", text)


def _ascii_fallback(text: str) -> str:
    if unidecode:
        return unidecode(text)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c if c.isascii() else "_" for c in decomposed if not unicodedata.combining(c))


def _clean(name: str, original: str) -> str:
    name = BAD_CHARS.sub("_", name)
    name = re.sub(r"_{2,}", "_", name)
    name = re.sub(r"\s{2,}", " ", name)
    name = re.sub(r"_+(?=\.)", "", name)
    if not original.startswith("_"):
        name = name.lstrip("_")
    if not original.endswith("_"):
        name = name.rstrip("_")
    name = name.rstrip(" .")
    if not name:
        name = "unnamed"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    if len(name) > MAX_NAME:
        suffix = Path(name).suffix[:20]
        name = name[: MAX_NAME - len(suffix)] + suffix
    return name


def transliterate(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    text = FA_MARKS.sub("", text)
    if KANA.search(text) and _KAKASI:
        text = _japanese(text)
    elif HAN.search(text) and lazy_pinyin:
        text = _chinese(text)
    text = text.translate(RU_TABLE).translate(FA_TABLE)
    text = _ascii_fallback(text)
    return _clean(text, name)


def _key(path: Path) -> str:
    return str(path).casefold()


def unique_target(path: Path, claimed: set) -> Path:
    def free(p: Path) -> bool:
        return _key(p) not in claimed and not p.exists() and not p.is_symlink()

    if free(path):
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if free(candidate):
            return candidate
        n += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Transliterate non-ASCII file/dir names in place.")
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("-n", "--dry-run", action="store_true", help="show renames without doing them")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
        sys.stderr.reconfigure(errors="backslashreplace")
    for lib, label in (
        (lazy_pinyin, "pypinyin (Chinese)"),
        (_KAKASI, "pykakasi (Japanese)"),
    ):
        if lib is None:
            print(
                f"warning: {label} not installed; falling back to a rougher conversion",
                file=sys.stderr,
            )
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1
    entries = [p for p in root.rglob("*") if not SKIP_DIRS.intersection(p.relative_to(root).parts)]
    entries.sort(key=lambda p: len(p.parts), reverse=True)
    claimed: set = set()
    renamed = failed = 0
    for old in entries:
        if old.name.isascii():
            continue
        new_name = transliterate(old.name)
        if new_name == old.name:
            continue
        new = unique_target(old.with_name(new_name), claimed)
        claimed.add(_key(new))
        print(f"{old.relative_to(root)}  ->  {new.name}")
        if args.dry_run:
            renamed += 1
            continue
        try:
            old.rename(new)
            renamed += 1
        except OSError as exc:
            failed += 1
            print(f"  error: {exc}", file=sys.stderr)
    verb = "would rename" if args.dry_run else "renamed"
    print(f"\n{verb} {renamed} item(s), {failed} error(s).")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
