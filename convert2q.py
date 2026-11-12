#!/data/data/com.termux/files/usr/bin/env python
"""Convert declarative sentences found in text files into questions written to qmade.txt."""

from __future__ import annotations


from pathlib import Path
import re

EXTENSIONS: set[str] = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log"}

AUXILIARIES: tuple[str, ...] = (
    "is",
    "are",
    "am",
    "was",
    "were",
    "has",
    "have",
    "had",
    "can",
    "could",
    "will",
    "would",
    "shall",
    "should",
    "may",
    "might",
    "must",
)


def to_question(sentence: str) -> str | None:
    sentence = sentence.strip()

    if not sentence:
        return None

    if sentence.endswith("?"):
        return None

    sentence = re.sub(r"[.!;]+$", "", sentence).strip()

    if not sentence:
        return None

    words = sentence.split()
    first = words[0].lower()

    if first in AUXILIARIES:
        return sentence[0].lower() + sentence[1:] + "?"

    if first.endswith(("n't", "'re", "'ve", "'ll", "'d")):
        return sentence[0].lower() + sentence[1:] + "?"

    if len(words) >= 2:
        subject = words[0]
        verb = words[1]

        if verb.lower() in {
            "went",
            "came",
            "saw",
            "made",
            "took",
            "gave",
            "found",
            "knew",
            "bought",
            "wrote",
            "ate",
            "ran",
            "spoke",
        }:
            return f"Did {sentence[0].lower() + sentence[1:]}?"

        if verb.lower().endswith("ed"):
            return f"Did {sentence[0].lower() + sentence[1:]}?"

    if len(words) >= 2:
        verb = words[1].lower()

        if verb.endswith(("s", "es")) and verb not in {"is", "has", "does"}:
            subject = words[0]
            rest = " ".join(words[1:])
            return f"Does {subject} {rest}?"

    return "Is it true that " + sentence[0].lower() + sentence[1:] + "?"


def main() -> None:
    results = []

    for path in Path(".").rglob("*"):
        if not path.is_file():
            continue
        if path.name in {"qmade.txt"}:
            continue

        if path.suffix.lower() not in EXTENSIONS:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        sentences = re.split(r"(?<=[.!?])\s+", text)

        for sentence in sentences:
            question = to_question(sentence)

            if question:
                results.append(question)

    results = list(dict.fromkeys(results))

    Path("qmade.txt").write_text("\n".join(results), encoding="utf-8")

    print(f"Converted {len(results)} sentences.")
    print("Saved to qmade.txt")


if __name__ == "__main__":
    main()
