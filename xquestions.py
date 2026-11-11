#!/data/data/com.termux/files/usr/bin/env python
"""Extract sentences ending with Persian or English question marks into q.txt."""

from pathlib import Path
import re


# Text-based file extensions to scan
TEXT_EXT: set[str] = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".py", ".yaml", ".yml"}

# Match sentences ending with Persian or English question marks
QUESTION_PATTERN = re.compile(r"[^؟?]*[؟?]")


def main() -> None:
    questions = []

    for path in Path(".").rglob("*"):
        if not path.is_file():
            continue

        if path.name == "q.txt" or path.suffix.lower() not in TEXT_EXT:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for match in QUESTION_PATTERN.finditer(text):
            sentence = match.group().strip()

            if sentence:
                questions.append(sentence)

    # Remove duplicates while preserving order
    questions = list(dict.fromkeys(questions))

    # Save results
    Path("q.txt").write_text("\n".join(questions), encoding="utf-8")

    print(f"Extracted {len(questions)} questions into q.txt")


if __name__ == "__main__":
    main()
