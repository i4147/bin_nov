#!/data/data/com.termux/files/usr/bin/env python
"""extract_fa.py — extract unique Persian words from text files.

Walks the current directory (".") with a generator-style filesystem walker,
skips binary files (via the `binaryornot` package), extracts Persian words
with the `spasy` package tokenizer, and writes the sorted unique words to
``fa.txt`` (one word per line).

Files are processed in parallel by 4 worker processes; a progress bar is
rendered with `rich`.

Usage:
    python extract_fa.py                 # walk ".", write ./fa.txt
    python extract_fa.py -o out/fa.txt   # custom output path
    python extract_fa.py -j 4            # number of workers (default 4)
    python extract_fa.py --min-len 2     # drop 1-char tokens

Requirements:
    pip install spasy binaryornot rich
"""

from __future__ import annotations
import argparse
from concurrent.futures import (
    ProcessPoolExecutor,
    as_completed,
)
import sys
from typing import Iterator

from binaryornot.check import is_binary
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


try:
    import spasy
except ImportError:
    sys.exit("spasy is not installed:  pip install spasy  (or see README)")


# --------------------------------------------------------------------------
# worker (runs in a child process)
# --------------------------------------------------------------------------


def process_file(path: str, min_len: int) -> set:
    """Tokenize one file and return the set of Persian words in it.

    Uses a blank fa pipeline (tokenizer only) — no tagging/NER needed for
    word extraction, which keeps it fast.
    """
    nlp = _get_nlp()
    words = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return words
    if not text:
        return words
    doc = nlp(text)
    for tok in doc:
        if len(tok.text) < min_len:
            continue
        if _is_persian_word(tok.text):
            words.add(tok.text)
    return words


def _get_nlp() -> None:
    """One tokenizer per worker process (cached at module level)."""
    global _NLP
    if _NLP is None:
        _NLP = spasy.blank("fa")
    return _NLP


_NLP = None


def _is_persian_word(text: str) -> bool:
    """A word counts as Persian when ALL of its letters are Persian/Arabic
    script (digits, ZWNJ and punctuation alone don't count)."""
    from spasy.util import is_persian_char

    letters = 0
    for ch in text:
        if is_persian_char(ch):
            letters += 1
        elif ch.isalpha():
            return False  # mixed-script token (e.g. latin) -> not Persian
        # digits, ZWNJ, punctuation, spaces: neutral, ignored
    return letters > 0


# --------------------------------------------------------------------------
# generator-style fs walker
# --------------------------------------------------------------------------


def iter_text_files(root: str) -> Iterator[str]:
    """Yield text file paths under `root` (generator, skips binaries)."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        # prune hidden dirs and common VCS dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            if fname.startswith(".") or fname == OUTPUT_DEFAULT:
                continue
            path = os.path.join(dirpath, fname)
            if is_binary(path):
                continue
            yield path


OUTPUT_DEFAULT: str = "fa.txt"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract unique Persian words from text files under a directory.")
    ap.add_argument("root", nargs="?", default=".", help="directory to walk (default: .)")
    ap.add_argument("-o", "--output", default=OUTPUT_DEFAULT, help="output file (default: fa.txt)")
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="number of worker processes (default: 4)",
    )
    ap.add_argument(
        "--min-len",
        type=int,
        default=1,
        help="minimum word length to keep (default: 1)",
    )
    args = ap.parse_args(argv)

    # collect the file list first so the progress bar has a total; the
    # walker itself is a generator (memory-safe for huge trees)
    files = list(iter_text_files(args.root))
    if not files:
        print("no text files found under %r" % args.root)
        return 0

    unique_words = set()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )
    with progress:
        task = progress.add_task("extracting Persian words", total=len(files))
        # 4 parallel workers
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(process_file, path, args.min_len): path for path in files}
            for future in as_completed(futures):
                try:
                    unique_words |= future.result()
                except Exception as exc:  # never lose the whole run
                    progress.console.print("[red]error in %s: %s[/red]" % (futures[future], exc))
                progress.advance(task)

    with open(args.output, "w", encoding="utf-8") as f:
        f.writelines(word + "\n" for word in sorted(unique_words))

    print("done: %d files scanned, %d unique Persian words -> %s" % (len(files), len(unique_words), args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
