#!/data/data/com.termux/files/usr/bin/env python
"""Count the word frequencies of the files below the current directory.
Matches the lowercase words of each text file, counts them in a process pool and writes the totals with a timestamp to ``counter.json``.
Usage: word_freq_counter Examples: $ word_freq_counter Runs as a script (``python word_freq_counter.py``) in the directory it should act on; the module can also be imported."""

from collections import Counter
import json
from multiprocessing import Pool
from pathlib import Path
import re
from typing import Any

from dh import MAX_WORKERS, get_nobinary
from loguru import logger


WORD_PATTERN: re.Pattern[str] = re.compile(r"\b[a-z]+\b")
OUTPUT_FILE: Path = Path("counter.json")


def process_file(path: Path) -> Counter[str]:
    word_counter: Counter[str] = Counter()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content: str = f.read()
        words: list[str] = WORD_PATTERN.findall(content.lower())
        word_counter.update(words)
        logger.debug(f"Processed {path.name}: {len(words)} words found")
    except Exception as e:
        logger.warning(f"Failed to process {path}: {e}")
    return word_counter


def collect_text_files(directory: Path | None = None) -> list[Path]:
    if directory is None:
        directory = Path.cwd()
    text_files: list[Path] = get_nobinary(directory)
    print(f"Found {len(text_files)} text files to process")
    return text_files


def process_files_parallel(paths: list[Path]) -> Counter[str]:
    total_counter: Counter[str] = Counter()
    with Pool(processes=MAX_WORKERS) as pool:
        async_results: list[Any] = [pool.apply_async(process_file, (path,)) for path in paths]
        for path, async_result in zip(paths, async_results):
            try:
                file_counter: Counter[str] = async_result.get()
                total_counter.update(file_counter)
                logger.debug(f"Completed processing {path.name}")
            except Exception as e:
                logger.error(f"Error processing {path}: {e}")
    return total_counter


def _now_isoformat() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


def save_results_json(counter: Counter[str], output_file: Path) -> None:
    sorted_words: dict[str, int] = dict(sorted(counter.items(), key=lambda x: (-x[1], x[0])))
    results: dict[str, Any] = {
        "metadata": {
            "total_words": sum(counter.values()),
            "unique_words": len(counter),
            "timestamp": _now_isoformat(),
        },
        "word_counts": sorted_words,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Results saved to {output_file}")


def main() -> int:
    directory: Path = Path.cwd()
    print(f"Starting word frequency analysis in {directory}")
    text_files: list[Path] = collect_text_files(directory)
    if not text_files:
        logger.warning("No text files found in the current directory!")
        save_results_json(Counter(), OUTPUT_FILE)
        return 0
    print(f"Processing {len(text_files)} files using parallel processing...")
    total_counter: Counter[str] = process_files_parallel(text_files)
    unique_words: int = len(total_counter)
    total_words: int = sum(total_counter.values())
    save_results_json(total_counter, OUTPUT_FILE)
    print("Analysis complete!")
    print(f"Total words found: {total_words}")
    print(f"Unique words found: {unique_words}")
    print("=" * 40)
    print("Top 10 Most Common Words:")
    print("-" * 40)
    for word, count in total_counter.most_common(10):
        print(f"{word:<20} {count:>8}")
    print("-" * 40)
    print(f"Full results saved to: {OUTPUT_FILE.absolute()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
