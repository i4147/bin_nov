#!/data/data/com.termux/files/usr/bin/env python
"""Translate lines in a text file with persistent caching and progress saving."""

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

from dh import MAX_WORKERS
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


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF\u0500-\u052F\u2DE0-\u2DFF\uA640-\uA69F\u1C80-\u1C8F]", text))


def create_chunks(lines: list[Any], max_chunk_size: int):
    chunks = []
    current_chunk = []
    current_size = 0
    for line in lines:
        line_size = len(line) + 1
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
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
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
        self.lock = threading.Lock()

    def get_many(self, texts: Sequence[Any], source_lang: str, target_lang: str):
        found = {}
        with self.lock:
            for start in range(0, len(texts), 900):
                batch = texts[start : start + 900]
                placeholders = ",".join(["?"] * len(batch))
                query = f"""
                    SELECT source_text, translated_text FROM translations
                    WHERE source_lang = ? AND target_lang = ?
                    AND source_text IN ({placeholders})
                """
                rows = self.conn.execute(query, [source_lang, target_lang] + batch)
                found.update(rows.fetchall())
        return found

    def set_many(self, translations: dict[str, Any], source_lang: str, target_lang: str) -> None:
        if not translations:
            return
        with self.lock:
            data = [(src, source_lang, target_lang, tgt) for src, tgt in translations.items()]
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

    def stats(self):
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] or 0
            last = self.conn.execute("SELECT MAX(updated_at) FROM translations").fetchone()[0]
            pairs = self.conn.execute("""
                SELECT source_lang, target_lang, COUNT(*) AS cnt
                FROM translations GROUP BY source_lang, target_lang
                ORDER BY cnt DESC LIMIT 100
            """).fetchall()
            return {
                "total_entries": total,
                "last_updated": last,
                "pairs": [{"source": r[0], "target": r[1], "count": r[2]} for r in pairs],
            }

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()


def translate_text(text: str, source_lang: str, target_lang: str):
    translator = None
    try:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if interrupted:
                return None
            try:
                if translator is None:
                    translator = Translator()
                translated = translator.translate(text, src=source_lang, dest=target_lang)
                if translated is None or not isinstance(translated.text, str) or not translated.text:
                    msg = "Translator returned an empty result"
                    raise ValueError(msg)
                print(f"{text} -> {translated.text}")
                return translated.text
            except Exception as exc:
                if interrupted:
                    return None
                if attempt == RETRY_ATTEMPTS:
                    logger.error("Translation failed after {} attempts for '{}': {}", attempt, text[:60], exc)
                    break
                delay = RETRY_DELAY * (2 ** (attempt - 1))
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


def translate_chunk_factory(source_lang: str, target_lang: str) -> Callable[..., Any]:
    def translate_chunk(chunk: Any):
        return chunk, translate_text("\n".join(chunk), source_lang, target_lang)

    return translate_chunk


def save_progress(all_lines: list[Any], results: list[Any], output_path: Path, json_path: Path, source_lang: str, target_lang: str) -> None:
    try:
        with output_path.open("w", encoding="utf-8") as f:
            translated_count = 0
            for line in all_lines:
                if line in results:
                    f.write(f"{results[line]}\n")
                    translated_count += 1
                else:
                    f.write(f"{line}\n")
        json_data = {
            "metadata": {
                "source_lang": source_lang,
                "target_lang": target_lang,
                "total_lines": len(all_lines),
                "translated_lines": translated_count,
                "untranslated_lines": len(all_lines) - translated_count,
                "interrupted": interrupted,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "translations": {line: results.get(line, line) for line in all_lines},
        }
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        logger.info(
            "Progress saved: {}/{} lines translated (Output: {}, JSON: {})",
            translated_count,
            len(all_lines),
            output_path.name,
            json_path.name,
        )
    except Exception as exc:
        logger.error("Error saving progress: {}", exc)


def main() -> int | None:
    global interrupted
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    parser = argparse.ArgumentParser(
        description="Translate lines in a text file with persistent caching and progress saving."
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
    parser.add_argument("--cache-stats", action="store_true", help="Show cache statistics and exit")
    args = parser.parse_args()
    if not args.input and not args.cache_stats:
        parser.error("-i/--input is required unless --cache-stats is used")
    if MAX_WORKERS < 1 or args.max_chunk_size < 1 or args.save_interval < 1:
        parser.error("--max-workers, --max-chunk-size, and --save-interval must be positive")
    db_path = Path(Path(args.db).expanduser())
    cache = TranslationCache(db_path)
    try:
        if args.cache_stats:
            stats = cache.stats()
            print("Translation cache stats\n-----------------------")
            print(f"DB path: {db_path}")
            print(f"Total entries: {stats['total_entries']}")
            print(f"Last updated: {stats['last_updated']}")
            print("Top language pairs:")
            if stats["pairs"]:
                for p in stats["pairs"]:
                    print(f"  {p['source']} -> {p['target']}: {p['count']}")
            else:
                print("  (no entries)")
            return 0
        input_path = Path(args.input)
        if not input_path.exists():
            logger.error("Input file not found: {}", input_path)
            return 1
        try:
            with input_path.open(encoding="utf-8") as f:
                all_lines = [line.rstrip("\n") for line in f if line.strip()]
        except OSError as exc:
            logger.error("Error reading input file: {}", exc)
            return 1
        if not all_lines:
            logger.info("No non-empty lines found in {}", input_path.name)
            return 0
        source_lang = args.source
        target_lang = args.target
        if source_lang.lower().startswith("ru"):
            to_translate_raw = [line for line in all_lines if contains_cyrillic(line)]
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
        to_translate_unique = list(dict.fromkeys(to_translate_raw))
        logger.info(
            "Deduplicated: {} unique lines to translate (from {} total flagged)",
            len(to_translate_unique),
            len(to_translate_raw),
        )
        results = cache.get_many(to_translate_unique, source_lang, target_lang)
        logger.info("Cache hit: {}/{}", len(results), len(to_translate_unique))
        remaining = [line for line in to_translate_unique if line not in results]
        output_path = input_path.with_name(f"{input_path.stem}_{target_lang}{input_path.suffix}")
        json_path = input_path.with_name(f"{input_path.stem}_{target_lang}.json")
        results_lock = threading.Lock()
        stop_saving = threading.Event()
        to_cache = {}

        def save() -> None:
            with results_lock:
                save_progress(all_lines, results, output_path, json_path, source_lang, target_lang)

        save()
        if remaining and not interrupted:
            chunks = create_chunks(remaining, args.max_chunk_size)
            num_workers = min(MAX_WORKERS, len(chunks))
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

            save_thread = threading.Thread(target=periodic_save, daemon=True)
            save_thread.start()
            try:
                translate_chunk = translate_chunk_factory(source_lang, target_lang)
                with ThreadPoolExecutor(max_workers=num_workers) as executor:
                    future_to_chunk = {executor.submit(translate_chunk, chunk): chunk for chunk in chunks}
                    total = len(future_to_chunk)
                    for completed, future in enumerate(as_completed(future_to_chunk), 1):
                        if interrupted:
                            logger.info("Interrupted. Waiting for running tasks to complete...")
                            for f in future_to_chunk:
                                f.cancel()
                            break
                        chunk = future_to_chunk[future]
                        try:
                            original_lines, translated_text = future.result()
                            translated_lines = translated_text.splitlines() if translated_text else []
                            if translated_text and len(translated_lines) == len(original_lines):
                                translations = dict(zip(original_lines, translated_lines))
                            else:
                                if translated_text:
                                    logger.warning(
                                        "Line-count mismatch ({} original vs {} translated); falling back to per-line translation",
                                        len(original_lines),
                                        len(translated_lines),
                                    )
                                else:
                                    logger.warning("Chunk translation failed; falling back to per-line translation")
                                translations = {}
                                for line in original_lines:
                                    if interrupted:
                                        break
                                    text = translate_text(line, source_lang, target_lang)
                                    if text is not None:
                                        translations[line] = text
                                    else:
                                        logger.error("Per-line translation failed for '{}'", line[:50])
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
                            logger.error("Unexpected error processing chunk starting with '{}': {}", chunk[0][:60], exc)
            finally:
                stop_saving.set()
                save_thread.join()
                if to_cache:
                    cache.set_many(to_cache, source_lang, target_lang)
                    logger.info("Saved {} new translations to cache", len(to_cache))
        save()
        if interrupted:
            logger.warning("Process was interrupted. Progress has been saved; rerun to resume.")
            return 130
        translated_count = sum(1 for line in all_lines if line in results and results[line] != line)
        logger.info("Translation complete: {}/{} lines changed", translated_count, len(all_lines))
        return 0
    finally:
        cache.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Exiting...")
        sys.exit(1)
