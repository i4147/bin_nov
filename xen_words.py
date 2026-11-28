#!/data/data/com.termux/files/usr/bin/env python
"""Extract English words from files with NLTK.

Tokenizes each file with word_tokenize, keeps tokens present in the NLTK words corpus and aggregates per-file sets with a process pool.
"""

from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import tempfile

from loguru import logger
import nltk

# Import specific NLTK submodules directly to avoid circular import
from nltk.corpus import words as nltk_words
from nltk.tokenize import word_tokenize
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


nltk.download("punkt", quiet=True)
nltk.download("words", quiet=True)

ENGLISH_WORDS = set(nltk_words.words())


@dataclass(slots=True)
class FileProcessor:
    def process_file(self, file_path: Path) -> set[str]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            tokens = word_tokenize(text.lower())
            return {tok for tok in tokens if tok.isalpha() and tok in ENGLISH_WORDS}
        except (OSError, UnicodeDecodeError):
            logger.warning(f"Skipping {file_path}")
            return set()


def get_text_files(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".txt", ".md", ".py", ".json", ".csv", ".html", ".xml"}
    ]


def main() -> None:
    root = Path()
    files = get_text_files(root)
    if not files:
        logger.info("No text files found")
        return

    output_path = Path("en.txt")
    temp_output = Path(tempfile.gettempdir()) / "en_unique_words.txt"

    progress_bar = Progress(
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
    )

    all_words: set[str] = set()

    with progress_bar as p:
        task = p.add_task("Processing files", total=len(files))
        with ProcessPoolExecutor() as executor:
            futures = {
                executor.submit(FileProcessor().process_file, f): f for f in files
            }
            for future in as_completed(futures):
                word_set = future.result()
                all_words.update(word_set)
                p.advance(task)

    with temp_output.open("w", encoding="utf-8") as f:
        f.write("\n".join(sorted(all_words)))

    output_path.write_text(temp_output.read_text(encoding="utf-8"), encoding="utf-8")
    temp_output.unlink(missing_ok=True)
    logger.info(f"Saved {len(all_words)} unique English words to {output_path}")


if __name__ == "__main__":
    main()
