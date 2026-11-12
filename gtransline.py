#!/data/data/com.termux/files/usr/bin/env python
"""Translate the non-English (mostly Chinese) lines of files in place.
Detects the lines containing CJK or other non-ASCII text, translates them with googletrans or deep_translator (whichever is installed), rewrites the files and processes them in a worker pool with a progress report.
Usage: gtransline [paths ...] [--extensions .txt,.md] [--exclude PATH] Positional arguments: files ...
Files or directories to process Options: --extensions File extensions to process --batch-size Batch size for progress logging --exclude Paths to exclude Examples: $ gtransline docs/ --extensions .md Command-line script: run ``python gtransline.py`` or the ``gtransline`` command.
The module is importable as well."""

import argparse
import multiprocessing as mp
from pathlib import Path
import re
import sys
from typing import Any, Final

from dh import MAX_WORKERS
from loguru import logger

try:
    from googletrans import Translator

    HAS_GOOGLETRANS = True
except ImportError:
    HAS_GOOGLETRANS = False
try:
    from deep_translator import GoogleTranslator

    HAS_DEEP_TRANSLATOR = True
except ImportError:
    HAS_DEEP_TRANSLATOR = False
if not (HAS_GOOGLETRANS or HAS_DEEP_TRANSLATOR):
    logger.error("Please install either googletrans (4.0.0rc1) or deep-translator.")
    sys.exit(1)
CHINESE_PATTERN: Final[re.Pattern] = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u20000-\u2a6df\u2a700-\u2b73f\u2b740-\u2b81f\u2b820-\u2ceaf\uf900-\ufaff]"
)


def is_chinese_text(text: str, threshold: float = 0.3) -> bool:
    clean_text = "".join(text.split())
    if not clean_text:
        return False
    chinese_chars = len(CHINESE_PATTERN.findall(clean_text))
    return chinese_chars / len(clean_text) >= threshold


def is_non_english(text: str) -> bool:
    if not text.strip():
        return False
    return is_chinese_text(text)


class UniversalTranslator:
    def __init__(self) -> None:
        if HAS_DEEP_TRANSLATOR:
            self.translator = GoogleTranslator(source="auto", target="en")
            self.mode = "deep"
        else:
            self.translator = Translator()
            self.mode = "google"

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        try:
            if self.mode == "deep":
                return self.translator.translate(text)
            else:
                return self.translator.translate(text, dest="en").text
        except Exception as e:
            logger.warning("Translation error: {}", e)
            return text


def process_file(path: Path, batch_size: int = 10) -> None:
    print("Processing: %s", path)
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        lines = source.splitlines(keepends=True)
        target_indices = [i for i, line in enumerate(lines) if is_non_english(line)]
        if not target_indices:
            print("  No non-English (Chinese) lines found, skipping.")
            return
        translator = UniversalTranslator()
        translated_count = 0
        for i, idx in enumerate(target_indices):
            line = lines[idx]
            leading_ws = line[: len(line) - len(line.lstrip())]
            trailing_ws = line[len(line.rstrip()) :]
            stripped_line = line.strip()
            translated = translator.translate(stripped_line)
            lines[idx] = f"{leading_ws}{translated}{trailing_ws}"
            translated_count += 1
            if (i + 1) % batch_size == 0:
                print("  Progress: %d/%d lines translated", i + 1, len(target_indices))
        path.write_text("".join(lines), encoding="utf-8", errors="ignore")
        print("  ✓ Completed: %d lines translated", translated_count)
    except Exception as e:
        logger.error("  ✗ Error processing {}: {}", path, e)


def worker(args: tuple[Path, int]) -> None:
    process_file(*args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate non-English (Chinese) lines in-place.")
    parser.add_argument("files", nargs="+", help="Files or directories to process")
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=[".txt", ".md", ".py", ".js", ".html", ".css", ".json", ".xml", ".csv"],
        help="File extensions to process",
    )
    parser.add_argument("--batch-size", type=int, default=10, help="Batch size for progress logging")
    parser.add_argument("--exclude", nargs="+", default=[], help="Paths to exclude")
    args = parser.parse_args()
    exclude_paths = {Path(p).resolve() for p in args.exclude}
    files_to_process: list[Path] = []
    for entry in args.files:
        path = Path(entry)
        if path.is_file():
            if path.resolve() not in exclude_paths:
                files_to_process.append(path)
        elif path.is_dir():
            for ext in args.extensions:
                for path in path.rglob(f"*{ext}"):
                    if (
                        path.is_file()
                        and path.resolve() not in exclude_paths
                        and (not any(part.startswith(".") for part in path.parts))
                    ):
                        files_to_process.append(path)
    if not files_to_process:
        print("No files to process.")
        return
    print("Found %d files. Using %d workers...", len(files_to_process), MAX_WORKERS)
    tasks = [(fp, args.batch_size) for fp in files_to_process]
    if MAX_WORKERS == 1:
        for t in tasks:
            worker(t)
    else:
        with mp.Pool(processes=MAX_WORKERS) as pool:
            pool.map(worker, tasks)
    print("\n✓ All translations completed!")


if __name__ == "__main__":
    raise SystemExit(main())
