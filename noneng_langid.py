#!/data/data/com.termux/files/usr/bin/env python
"""Report every line of text that is not English, using py3langid.
Decodes each text file, classifies the lines above the minimum length with ``py3langid`` and prints the lines whose detected language is not English with their confidence, colourising the output and skipping binary extensions.
Usage: noneng_langid Examples: $ noneng_langid Runs as a script (``python noneng_langid.py``) in the directory it should act on; the module can also be imported.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Any, Iterator

from dh import SKIP_DIRS
import py3langid as langid

ROOT = Path.cwd()
SKIP_EXTS: set[str] = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".ico",
}
MIN_LEN: int = 12
MIN_CONF: float = 0.65
RESET: str = "\033[0m"
BOLD: str = "\033[1m"
YELLOW: str = "\033[33m"
MAGENTA: str = "\033[35m"


def walk_files(root: Path) -> Iterator[Path]:
    for path in root.iterdir():
        if path.is_dir():
            if path.name in SKIP_DIRS or path.name.startswith("."):
                continue
            yield from walk_files(path)
        elif path.is_file():
            if path.suffix.lower() in SKIP_EXTS:
                continue
            yield path


def decode_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def iter_lines(text: str) -> Iterator[str]:
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if len(line) >= MIN_LEN and any(ch.isalpha() for ch in line):
            yield lineno, line


def detect(text: str):
    results = langid.rank(text)
    if not results:
        return None, 0.0
    top_lang, top_score = results[0]
    if len(results) >= 2:
        second_score = results[1][1]
        try:
            conf = 1.0 / (1.0 + math.exp(second_score - top_score))
        except OverflowError:
            conf = 0.0
    else:
        conf = 1.0
    return top_lang, conf


def main() -> None:
    print(f"{BOLD}Scanning: {ROOT}{RESET}")
    for path in walk_files(ROOT):
        text = decode_text(path)
        if text is None:
            continue
        rel = path.relative_to(ROOT)
        for lineno, line in iter_lines(text):
            lang, conf = detect(line)
            if lang and lang != "en" and conf >= MIN_CONF:
                print(f"{YELLOW}{rel}:{lineno}{RESET} {MAGENTA}[{lang} {conf:.2f}]{RESET} {line}")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
