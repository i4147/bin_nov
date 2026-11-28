#!/data/data/com.termux/files/usr/bin/env python
"""Translate text files in place with googletrans.

Chunks text into 2,000-character segments with retries and delay, persists progress in SQLite, processes files with a thread pool and interrupts cleanly on SIGINT.
"""

from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import random
import re
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Sequence

from dh import MAX_WORKERS, contains_asian
from googletrans import Translator
from loguru import logger


RETRY_ATTEMPTS: int = 4
RETRY_DELAY: float = 0.6
MAX_CHUNK_SIZE: int = 2000
SAVE_INTERVAL: int = 10
interrupted: bool = False


def signal_handler(signum: int, frame: Any) -> None:
    global interrupted
    interrupted = True
    logger.warning("Received interrupt signal. Saving progress and exiting gracefully...")


def create_chunks(lines: list[str], max_chunk_size: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_size: int = 0

    for line in lines:
        line_size: int = len(line) + 1

        if line_size > max_chunk_size:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0

            chunks.append([line])
            continue

        if current_size + line_size > max_chunk_size and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

        current_chunk.append(line)
        current_size += line_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


class TranslationCache:
    def __init__(self, db_path: Path) -> None:
        self.db_path: Path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                id INTEGER PRIMARY KEY,
                source_text TEXT NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_text, source_lang, target_lang)
            )
            """)
        self.conn.commit()
        self.lock: threading.Lock = threading.Lock()

    def get_many(self, texts: Sequence[str], source_lang: str, target_lang: str) -> dict[str, str]:
        found: dict[str, str] = {}

        with self.lock:
            for start in range(0, len(texts), 900):
                batch: list[str] = list(texts[start : start + 900])
                placeholders: str = ",".join(["?"] * len(batch))
                query: str = f"""
                    SELECT source_text, translated_text FROM translations
                    WHERE source_lang = ? AND target_lang = ?
                    AND source_text IN ({placeholders})
                """
                rows = self.conn.execute(query, [source_lang, target_lang, *batch])
                found.update(rows.fetchall())

        return found

    def set_many(self, translations: dict[str, str], source_lang: str, target_lang: str) -> None:
        if not translations:
            return

        with self.lock:
            data: list[tuple[str, str, str, str]] = [
                (source, source_lang, target_lang, target) for source, target in translations.items()
            ]
            self.conn.executemany(
                """
                INSERT INTO translations
                    (source_text, source_lang, target_lang, translated_text, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_text, source_lang, target_lang) DO UPDATE SET
                    translated_text=excluded.translated_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                data,
            )
            self.conn.commit()

    def stats(self) -> dict[str, Any]:
        with self.lock:
            total: int = int(self.conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] or 0)
            last: str | None = self.conn.execute("SELECT MAX(updated_at) FROM translations").fetchone()[0]
            pairs: list[tuple[str, str, int]] = self.conn.execute("""
                SELECT source_lang, target_lang, COUNT(*) AS cnt
                FROM translations
                GROUP BY source_lang, target_lang
                ORDER BY cnt DESC
                LIMIT 100
                """).fetchall()

            return {
                "total_entries": total,
                "last_updated": last,
                "pairs": [{"source": source, "target": target, "count": count} for source, target, count in pairs],
            }

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()


def translate_text(text: str, source_lang: str, target_lang: str) -> str | None:
    translator: Translator | None = None

    try:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if interrupted:
                return None

            try:
                if translator is None:
                    translator = Translator()

                translated = translator.translate(text, src=source_lang, dest=target_lang)

                if translated is None or not isinstance(translated.text, str) or not translated.text:
                    msg: str = "Translator returned an empty result"
                    raise ValueError(msg)

                print(f"{text} -> {translated.text}")
                return translated.text

            except Exception as exc:
                if interrupted:
                    return None

                if attempt == RETRY_ATTEMPTS:
                    logger.error(
                        "Translation failed after {} attempts for '{}': {}",
                        attempt,
                        text[:60],
                        exc,
                    )
                    break

                delay: float = RETRY_DELAY * (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.25)
                logger.warning(
                    "Translate attempt {}/{} failed for '{}': {}; retrying in {}s",
                    attempt,
                    RETRY_ATTEMPTS,
                    text[:60],
                    exc,
                    delay,
                )
                time.sleep(delay)

        return None

    finally:
        if translator is not None:
            try:
                translator.client.close()
            except Exception as exc:
                logger.debug("Could not close translator client: {}", exc)


def translate_chunk_factory(source_lang: str, target_lang: str) -> Callable[[list[str]], tuple[list[str], str | None]]:
    def translate_chunk(
        chunk: list[str],
    ) -> tuple[list[str], str | None]:
        return chunk, translate_text("\n".join(chunk), source_lang, target_lang)

    return translate_chunk


def save_json(
    all_lines: list[str],
    results: dict[str, str],
    json_path: Path,
) -> None:
    try:
        translations: dict[str, str] = {line: results.get(line, line) for line in all_lines}

        with json_path.open("w", encoding="utf-8") as file:
            json.dump(translations, file, ensure_ascii=False, indent=2)
            file.write("\n")

        logger.info("JSON saved: {}", json_path.name)

    except Exception as exc:
        logger.error("Error saving JSON: {}", exc)


def main() -> int | None:
    global interrupted

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Translate lines in a text file with persistent caching and save a headerless JSON translation map."
        )
    )
    parser.add_argument("-i", "--input", help="Input text file (one phrase per line)")
    parser.add_argument("-s", "--source", default="ru", help="Source language code (default: ru)")
    parser.add_argument("-t", "--target", default="en", help="Target language code (default: en)")
    parser.add_argument(
        "--db",
        default="~/.translate/translate.db",
        help="SQLite DB path for cache (default: ~/.translate/translate.db)",
    )
    parser.add_argument(
        "--max-chunk-size",
        type=int,
        default=MAX_CHUNK_SIZE,
        help=f"Max characters per chunk (default: {MAX_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--save-interval",
        type=int,
        default=SAVE_INTERVAL,
        help=f"Save progress interval in seconds (default: {SAVE_INTERVAL})",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show cache statistics and exit",
    )
    args: argparse.Namespace = parser.parse_args()

    if not args.input and not args.cache_stats:
        parser.error("-i/--input is required unless --cache-stats is used")

    if MAX_WORKERS < 1 or args.max_chunk_size < 1 or args.save_interval < 1:
        parser.error("--max-workers, --max-chunk-size, and --save-interval must be positive")

    db_path: Path = Path(args.db).expanduser()
    cache: TranslationCache = TranslationCache(db_path)

    try:
        if args.cache_stats:
            stats: dict[str, Any] = cache.stats()
            print("Translation cache stats")
            print("-----------------------")
            print(f"DB path: {db_path}")
            print(f"Total entries: {stats['total_entries']}")
            print(f"Last updated: {stats['last_updated']}")
            print("Top language pairs:")

            if stats["pairs"]:
                for pair in stats["pairs"]:
                    print(f"  {pair['source']} -> {pair['target']}: {pair['count']}")
            else:
                print("  (no entries)")

            return 0

        input_path: Path = Path(args.input)

        if not input_path.exists():
            logger.error("Input file not found: {}", input_path)
            return 1

        try:
            with input_path.open(encoding="utf-8") as file:
                all_lines: list[str] = [line.rstrip("\n") for line in file if line.strip()]
        except OSError as exc:
            logger.error("Error reading input file: {}", exc)
            return 1

        if not all_lines:
            logger.info("No non-empty lines found in {}", input_path.name)
            return 0

        source_lang: str = args.source
        target_lang: str = args.target

        if source_lang.lower().startswith(("ru", "ja", "fa", "ko", "zh", "auto")):
            to_translate_raw: list[str] = [line for line in all_lines if contains_asian(line)]
        else:
            to_translate_raw = list(all_lines)

        logger.info(
            "Loaded {} lines: {} flagged for translation, {} skipped",
            len(all_lines),
            len(to_translate_raw),
            len(all_lines) - len(to_translate_raw),
        )

        if not to_translate_raw:
            logger.info("No lines to translate for source_lang={}", source_lang)
            return 0

        to_translate_unique: list[str] = list(dict.fromkeys(to_translate_raw))
        logger.info(
            "Deduplicated: {} unique lines to translate (from {} total flagged)",
            len(to_translate_unique),
            len(to_translate_raw),
        )

        results: dict[str, str] = cache.get_many(to_translate_unique, source_lang, target_lang)
        logger.info("Cache hit: {}/{}", len(results), len(to_translate_unique))

        remaining: list[str] = [line for line in to_translate_unique if line not in results]

        json_path: Path = input_path.with_name(f"{input_path.stem}_{target_lang}.json")

        results_lock: threading.Lock = threading.Lock()
        stop_saving: threading.Event = threading.Event()
        to_cache: dict[str, str] = {}

        def save() -> None:
            with results_lock:
                save_json(all_lines, results, json_path)

        save()

        if remaining and not interrupted:
            chunks: list[list[str]] = create_chunks(remaining, args.max_chunk_size)
            num_workers: int = min(MAX_WORKERS, len(chunks))
            logger.info(
                "Created {} chunk(s) from {} remaining lines (max {} chars per chunk), using {} worker(s)",
                len(chunks),
                len(remaining),
                args.max_chunk_size,
                num_workers,
            )

            def periodic_save() -> None:
                while not stop_saving.wait(args.save_interval):
                    save()

            save_thread: threading.Thread = threading.Thread(target=periodic_save, daemon=True)
            save_thread.start()

            try:
                translate_chunk: Callable[[list[str]], tuple[list[str], str | None]] = translate_chunk_factory(
                    source_lang, target_lang
                )

                with ThreadPoolExecutor(max_workers=num_workers) as executor:
                    future_to_chunk: dict[Any, list[str]] = {
                        executor.submit(translate_chunk, chunk): chunk for chunk in chunks
                    }
                    total: int = len(future_to_chunk)

                    for completed, future in enumerate(as_completed(future_to_chunk), 1):
                        if interrupted:
                            logger.info("Interrupted. Waiting for running tasks to complete...")

                            for pending_future in future_to_chunk:
                                pending_future.cancel()

                            break

                        chunk: list[str] = future_to_chunk[future]

                        try:
                            original_lines: list[str]
                            translated_text: str | None
                            original_lines, translated_text = future.result()

                            translated_lines: list[str] = translated_text.splitlines() if translated_text else []

                            translations: dict[str, str] = {}

                            if translated_text and len(translated_lines) == len(original_lines):
                                translations = dict(zip(original_lines, translated_lines))
                            else:
                                if translated_text:
                                    logger.warning(
                                        "Line-count mismatch "
                                        "({} original vs {} translated); "
                                        "falling back to per-line translation",
                                        len(original_lines),
                                        len(translated_lines),
                                    )
                                else:
                                    logger.warning("Chunk translation failed; falling back to per-line translation")

                                for line in original_lines:
                                    if interrupted:
                                        break

                                    text: str | None = translate_text(line, source_lang, target_lang)

                                    if text is not None:
                                        translations[line] = text
                                    else:
                                        logger.error(
                                            "Per-line translation failed for '{}'",
                                            line[:50],
                                        )

                            with results_lock:
                                results.update(translations)

                            to_cache.update(translations)
                            logger.info(
                                "Processed chunk {}/{} ({}/{} lines translated)",
                                completed,
                                total,
                                len(translations),
                                len(chunk),
                            )

                            if len(to_cache) >= 50 or completed == total:
                                cache.set_many(to_cache, source_lang, target_lang)
                                to_cache.clear()

                        except Exception as exc:
                            logger.error(
                                "Unexpected error processing chunk starting with '{}': {}",
                                chunk[0][:60],
                                exc,
                            )

            finally:
                stop_saving.set()
                save_thread.join()

                if to_cache:
                    cache.set_many(to_cache, source_lang, target_lang)
                    logger.info(
                        "Saved {} new translations to cache",
                        len(to_cache),
                    )

        save()

        if interrupted:
            logger.warning("Process was interrupted. Progress has been saved; rerun to resume.")
            return 130

        translated_count: int = sum(1 for line in all_lines if line in results and results[line] != line)
        logger.info(
            "Translation complete: {}/{} lines changed",
            translated_count,
            len(all_lines),
        )
        return 0

    finally:
        cache.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Exiting...")
        sys.exit(1)
