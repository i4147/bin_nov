#!/data/data/com.termux/files/usr/bin/env python
"""Build a word-frequency JSON from the files below the current directory.
Tokenizes the text files (regex by default, or nltk, blingfire, sacremoses, spacy, pyonmttok) in a pool, counts the words and writes them to ``word_freq.json`` for the ``~/.personal_dict`` used by the spelling tools.
Usage: update_custom_dict [--tokenizer regex|nltk|blingfire|sacremoses|spacy|pyonmttok] Options: -t, --tokenizer Examples: $ update_custom_dict -t nltk Command-line script: run ``python update_custom_dict.py`` or the ``update_custom_dict`` command.
The module is importable as well."""

import argparse
from collections import Counter
import contextlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Iterator

from atomicio import atomic_write
from dh import SKIP_DIRS, BINARY_EXT


CWD = Path.cwd()
DICT_PATH = Path.home() / ".personal_dict"
JSON_PATH = CWD / "word_freq.json"
MAX_BYTES: int = 5 * 1024 * 1024
WORD_RE = re.compile(r"[A-Za-z]{2,}")
_TOKENIZER = None


def _ensure_punkt() -> None:
    import nltk

    for res in ("tokenizers/punkt", "tokenizers/punkt_tab"):
        try:
            nltk.data.find(res)
        except LookupError:
            with contextlib.suppress(Exception):
                nltk.download(res.split("/")[-1], quiet=True)


def _make_regex() -> Callable[..., Any]:
    def tok(text: str) -> list[str]:
        return [m.group().lower() for m in WORD_RE.finditer(text)]

    return tok


def _make_nltk() -> Callable[..., Any]:
    from nltk.tokenize import word_tokenize

    _ensure_punkt()

    def tok(text: str) -> list[str]:
        try:
            return [t.lower() for t in word_tokenize(text) if t.isalpha() and len(t) > 1]
        except Exception:
            return []

    return tok


def _make_blingfire() -> Callable[..., Any]:
    import blingfire

    def tok(text: str) -> list[str]:
        try:
            raw = blingfire.text_to_words(text)
        except Exception:
            return []
        return [w.lower() for w in raw.split() if w.isalpha() and len(w) > 1]

    return tok


def _make_sacremoses() -> Callable[..., Any]:
    from sacremoses import MosesTokenizer

    mt = MosesTokenizer(lang="en")

    def tok(text: str) -> list[str]:
        try:
            toks = mt.tokenize(text, return_str=False)
        except Exception:
            return []
        return [w.lower() for w in toks if w.isalpha() and len(w) > 1]

    return tok


def _make_spacy() -> Callable[..., Any]:
    import spacy

    try:
        nlp = spacy.load(
            "en_core_web_sm",
            disable=["ner", "parser", "lemmatizer", "tagger", "attribute_ruler"],
        )
    except OSError:
        nlp = spacy.blank("en")

    def tok(text: str) -> list[str]:
        try:
            doc = nlp(text)
        except Exception:
            return []
        return [t.text.lower() for t in doc if t.is_alpha and len(t.text) > 1]

    return tok


def _make_pyonmttok() -> Callable[..., Any]:
    import pyonmttok

    tokenizer = pyonmttok.Tokenizer("conservative", joiner_annotate=False)

    def tok(text: str) -> list[str]:
        try:
            tokens, _ = tokenizer.tokenize(text)
        except Exception:
            return []
        return [w.lower() for w in tokens if w.isalpha() and len(w) > 1]

    return tok


TOKENIZER_FACTORIES: dict[str, Any] = {
    "regex": _make_regex,
    "nltk": _make_nltk,
    "blingfire": _make_blingfire,
    "sacremoses": _make_sacremoses,
    "spacy": _make_spacy,
    "pyonmttok": _make_pyonmttok,
}


def _init_worker(mode: str) -> None:
    global _TOKENIZER
    _TOKENIZER = TOKENIZER_FACTORIES[mode]()


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def walk(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in SKIP_DIRS:
                                continue
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            yield Path(e.path)
                    except OSError:
                        continue
        except OSError:
            continue


def process_file(path_str: str):
    try:
        data = Path(path_str).read_bytes()
    except OSError:
        return Counter()
    if b"\x00" in data[:8192]:
        return Counter()
    text = decode_bytes(data)
    if not text:
        return Counter()
    return Counter(_TOKENIZER(text))


def write_json_stream(path: Path, pairs: Iterable[Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    n = len(pairs)
    with tmp.open("w", encoding="utf-8") as f:
        f.write("[\n")
        for i, (w, c) in enumerate(pairs):
            f.write("  ")
            f.write(json.dumps([w, c], ensure_ascii=False))
            if i < n - 1:
                f.write(",")
            f.write("\n")
        f.write("]\n")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t",
        "--tokenizer",
        choices=tuple(TOKENIZER_FACTORIES),
        default="nltk",
    )
    args = parser.parse_args()
    skip = {DICT_PATH.resolve(), JSON_PATH.resolve()}
    candidates = []
    for p in walk(CWD):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in skip:
            continue
        if p.suffix.lower() in BINARY_EXT:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > MAX_BYTES:
            continue
        candidates.append((str(p), size))
    candidates.sort(key=lambda x: -x[1])
    paths = [c[0] for c in candidates]
    counter = Counter()
    if paths:
        procs = min(8, mp.cpu_count() or 1)
        with mp.Pool(
            processes=procs,
            initializer=_init_worker,
            initargs=(args.tokenizer,),
        ) as pool:
            for c in pool.imap_unordered(process_file, paths, chunksize=1):
                counter.update(c)
    from spellchecker import SpellChecker

    spell = SpellChecker()
    known = spell.word_frequency.words()
    custom = {w: c for w, c in counter.items() if w not in known}
    words_sorted = sorted(custom)
    atomic_write(
        DICT_PATH,
        "\n".join(words_sorted) + ("\n" if words_sorted else ""),
    )
    pairs = sorted(custom.items(), key=lambda kv: (-kv[1], kv[0]))
    write_json_stream(JSON_PATH, pairs)
    print(f"Tokenizer:                        {args.tokenizer}")
    print(f"Files scanned:                    {len(paths)}")
    print(f"Unique tokens found:              {len(counter)}")
    print(f"Words added to custom dictionary: {len(words_sorted)}")
    print(f"Dictionary written to:            {DICT_PATH}")
    print(f"Word+freq JSON written to:        {JSON_PATH}")


if __name__ == "__main__":
    main()
