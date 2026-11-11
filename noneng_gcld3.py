#!/data/data/com.termux/files/usr/bin/env python
"""Detect the language of the text files below the current directory with gcld3.
Identifies each file with ``gcld3`` (falling back to ``py3langid`` for short samples) and prints the detected language with its confidence for every file whose language is not English.
Usage: noneng_gcld3 Examples: $ noneng_gcld3 Runs as a script (``python noneng_gcld3.py``) in the directory it should act on; the module can also be imported."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

import gcld3
import py3langid as langid


MIN_PROBABILITY: float = 0.5
_detector: gcld3.NNetLanguageIdentifier = gcld3.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=1000)


@dataclass(frozen=True, slots=True)
class Detection:
    language: str
    probability: float
    source: str


def detect_with_gcld3(text: str) -> Optional[Detection]:
    try:
        result = _detector.FindLanguage(text)
        if not result.is_reliable or result.probability < MIN_PROBABILITY:
            return None
        return Detection(str(result.language), float(result.probability), "gcld3")
    except Exception:
        return None


def detect_with_langid(text: str) -> Optional[Detection]:
    try:
        lang, score = langid.classify(text)
        return Detection(str(lang), float(score), "py3langid")
    except Exception:
        return None


def detect_language(text: str) -> Detection:
    primary = detect_with_gcld3(text)
    if primary is not None:
        return primary
    fallback = detect_with_langid(text)
    if fallback is not None:
        return fallback
    return Detection("unknown", 0.0, "none")


def scan_and_detect(root_dir: str = ".") -> None:
    print(f"Scanning directory: {Path(root_dir).absolute()}\n")
    print(f"{'File Path':<40} | {'Language':<12} | {'Prob':>6} | {'Source':<10} | Snippet")
    print("-" * 110)
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file in files:
            if file.startswith("."):
                continue
            file_path = Path(root).joinpath(file)
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                if not content:
                    continue
                det = detect_language(content)
                if det.language != "en":
                    snippet = content[:40].replace("\n", " ") + ("..." if len(content) > 40 else "")
                    print(
                        f"{file_path:<40} | {det.language:<12} | "
                        f"{det.probability * 100:5.0f}% | {det.source:<10} | {snippet}"
                    )
            except Exception:
                continue


if __name__ == "__main__":
    scan_and_detect(".")
