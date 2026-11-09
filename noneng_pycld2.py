#!/data/data/com.termux/files/usr/bin/env python
"""Detect the language of every text file with pycld2.
Walks the current directory (hidden entries ignored), guesses the language of each readable file with pycld2 and prints a table of file, language, probability and a short snippet.
Usage: python noneng_pycld2.py Examples: $ python noneng_pycld2.py Runs its scan at import time, so start it in the tree to inspect."""

import os
from pathlib import Path

import pycld2 as cld2


def detect_language(text):
    try:
        _is_reliable, _text_bytes_found, details = cld2.detect(text)
        lang = details[0][1] if details else "unknown"
        prob = details[0][2] if details else 0.0
    except Exception as e:
        lang, prob = f"error ({e!s})", 0.0
    return lang, prob


def scan_and_detect(root_dir: Path):
    print(f"{'File Path':<40} | {'pycld2':<15} | Snippet", flush=True)

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file in files:
            if file.startswith("."):
                continue
            file_path = Path(root).joinpath(file)
            if ".git" in file_path.parts or file_path.is_symlink():
                continue
            try:
                with file_path.open(encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                if not content:
                    continue

                lang, prob = detect_language(content)
                snippet = content[:40].replace("\n", " ") + ("..." if len(content) > 40 else "")

                print(f"{file_path:<40} | {lang:<7} ({prob:2.0f}%) | {snippet}", flush=True)

            except Exception:
                pass


if __name__ == "__main__":
    cwd = Path.cwd()
    scan_and_detect(cwd)
