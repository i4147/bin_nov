#!/data/data/com.termux/files/usr/bin/env python
"""Extract unique Persian words from files."""

import argparse
from collections import Counter
from functools import partial
from multiprocessing import Pool, cpu_count
from pathlib import Path
import re
import sys
from typing import Any, Generator, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger


try:
    import nltk
    from nltk.probability import FreqDist

    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False
    FreqDist = None
try:
    from hazm import Normalizer, WordTokenizer

    HAZM_AVAILABLE = True
except ImportError:
    HAZM_AVAILABLE = False
TEXT_EXT: set[str] = {
    ".txt",
    ".py",
    ".md",
    ".html",
    ".htm",
    ".xml",
    ".json",
    ".csv",
    ".log",
    ".doc",
    ".docx",
    ".rtf",
    ".tex",
    ".js",
    ".ts",
    ".sql",
}
PERSIAN_UNICODE_RANGE: str = "\u0600-\u06ff\u0670\u064b-\u065f\u200c"
STOPWORDS: set[str] = {
    "و",
    "به",
    "از",
    "در",
    "با",
    "برای",
    "روی",
    "درباره",
    "این",
    "آن",
    "که",
    "شد",
    "بود",
    "است",
    "ندارند",
    "نشد",
    "نمود",
    "ی",
    "یک",
    "چه",
    "کدام",
    "کس",
    "کی",
    "کجا",
    "چرا",
    "چگونه",
    "هم",
    "همین",
    "همان",
    "همه",
    "ها",
    "های",
    "ان",
    "ین",
    "ار",
    "ای",
}
DEFAULT_OUTPUT: str = "fa.txt"
DEFAULT_MIN_LENGTH: int = 0


def setup_logger(verbose: bool) -> Any:
    logger.remove()
    logger.add(sys.stdout, level="DEBUG" if verbose else "INFO")
    return logger


def ensure_nltk_data(logger: Any) -> None:
    if not NLTK_AVAILABLE:
        return
    try:
        nltk.data.find("corpora/stopwords")
    except LookupError:
        logger.debug("NLTK stopwords corpus missing; attempting download...")
        try:
            nltk.download("stopwords", quiet=True)
        except Exception as exc:
            logger.debug(f"NLTK stopwords download failed: {exc}")


def build_combined_stopwords() -> set[str]:
    words = set(STOPWORDS)
    if NLTK_AVAILABLE:
        try:
            from nltk.corpus import stopwords as nltk_stopwords

            for lang in ("persian", "arabic"):
                try:
                    words.update(nltk_stopwords.words(lang))
                except (LookupError, OSError):
                    continue
        except Exception:
            pass
    return words


class PersianTokenizer:
    def __init__(self) -> None:
        if HAZM_AVAILABLE:
            self.normalizer = Normalizer()
            self.tokenizer = WordTokenizer()
        else:
            self.normalizer = None
            self.tokenizer = None
        self.persian_pattern = re.compile(f"^[{PERSIAN_UNICODE_RANGE}]+$")

    def clean_and_tokenize(self, text: str, min_len: int = 0) -> Generator[str, None, None]:
        if HAZM_AVAILABLE:
            normalized_text = self.normalizer.normalize(text)
            raw_tokens = self.tokenizer.tokenize(normalized_text)
        else:
            raw_tokens = text.split()
        for token in raw_tokens:
            token = token.strip("،؛:؟!.,()[]{}<>«»\"'-_*+=")
            if len(token) >= min_len and self._is_persian_word(token):
                yield token

    def _is_persian_word(self, word: str) -> bool:
        return bool(self.persian_pattern.match(word))


def is_valid_text_file(file_path: Path) -> bool:
    if not file_path.is_file():
        return False
    if file_path.suffix.lower() not in TEXT_EXT:
        return False
    try:
        if file_path.stat().st_size == 0:
            return False
    except OSError:
        return False
    return True


def read_file_content(file_path: Path) -> Optional[str]:
    for enc in ("utf-8", "windows-1256", "latin-1"):
        try:
            content = file_path.read_text(encoding=enc, errors="ignore")
            if content:
                return content
        except (IOError, OSError, UnicodeDecodeError):
            continue
    return None


def process_file(path_str: str, min_length: int) -> list[str]:
    file_path = Path(path_str)
    if not is_valid_text_file(file_path):
        return []
    try:
        content = read_file_content(file_path)
        if not content:
            return []
        tokenizer = PersianTokenizer()
        return list(tokenizer.clean_and_tokenize(content, min_length))
    except Exception as e:
        logger.opt(exception=True).debug(
            f"Error processing {file_path}: {e}",
        )
        return []


def collect_files(directory: Path) -> list[str]:
    return [str(f) for f in directory.rglob("*") if f.is_file()]


def print_statistics(counter: Counter, file_count: int) -> None:
    total_tokens = counter.total()
    unique = len(counter)
    print("\n" + "=" * 55)
    print(" Extraction Statistics")
    print("=" * 55)
    print(f" Files processed : {file_count}")
    print(f" Raw tokens      : {total_tokens}")
    print(f" Unique words    : {unique}")
    if NLTK_AVAILABLE and FreqDist is not None:
        fd = FreqDist(counter)
        print(f" NLTK FreqDist N : {fd.N()}")
        print(f" NLTK FreqDist B : {fd.B()}")
        print(" Top 10 words    :")
        for word, count in fd.most_common(10):
            print(f"   {word}  ({count})")
    else:
        print(" (NLTK not available; skipping FreqDist details)")
    print("=" * 55 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract unique Persian words from files.")
    parser.add_argument("source", type=str, help="Input directory or single file to scan")
    parser.add_argument(
        "--min-length", type=int, default=DEFAULT_MIN_LENGTH, help="Minimum word length to include (default: 0)"
    )
    parser.add_argument(
        "--max-length", type=int, default=0, help="Maximum word length to include (default: 0 = no limit)"
    )
    parser.add_argument(
        "--output", type=str, default=DEFAULT_OUTPUT, help=f"Output file path (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--stats", action="store_true", help="Print statistics after extraction")
    parser.add_argument(
        "--remove-stopwords", action="store_true", help="Filter out common Persian/stopwords (built-in + NLTK)"
    )
    args = parser.parse_args()
    logger = setup_logger(args.verbose)
    ensure_nltk_data(logger)
    if not HAZM_AVAILABLE:
        logger.warning(
            "Hazm package is missing. Run 'pip install hazm' for accurate Persian tokenization. Falling back to basic spaces."
        )
    input_path = Path(args.source).expanduser().resolve()
    if not input_path.exists():
        logger.error(f"Input path does not exist: {input_path}")
        sys.exit(1)
    files = [str(input_path)] if input_path.is_file() else collect_files(input_path)
    if not files:
        logger.warning("No files found to process.")
        sys.exit(0)
    valid_files = [f for f in files if is_valid_text_file(Path(f))]
    if not valid_files:
        logger.warning("No valid text files found.")
        sys.exit(0)
    worker_count = MAX_WORKERS or cpu_count()
    logger.info(f"Processing {len(valid_files)} file(s) with {worker_count} worker(s)...")
    counter: Counter = Counter()
    worker = partial(process_file, min_length=args.min_length)
    try:
        with Pool(processes=worker_count) as pool:
            for words in pool.imap_unordered(worker, valid_files, chunksize=8):
                counter.update(words)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        sys.exit(130)
    if not counter:
        logger.warning("No Persian words extracted.")
        sys.exit(0)
    if args.max_length and args.max_length > 0:
        counter = Counter({w: c for w, c in counter.items() if len(w) <= args.max_length})
    if args.remove_stopwords:
        stops = build_combined_stopwords()
        counter = Counter({w: c for w, c in counter.items() if w not in stops})
    output_path = Path(args.output)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.writelines(f"{word}\n" for word, _ in counter.most_common())
        logger.info(f"Successfully extracted words to: {output_path}")
    except Exception as e:
        logger.error(f"Failed to write output file: {e}")
    if args.stats:
        print_statistics(counter, len(valid_files))


if __name__ == "__main__":
    main()
