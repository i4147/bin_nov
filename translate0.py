#!/data/data/com.termux/files/usr/bin/env python
"""Translate lines in a text file with persistent caching."""


import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
from typing import Final

from deep_translator import GoogleTranslator
from dh import MAX_WORKERS
from loguru import logger


RETRY_ATTEMPTS: Final[int] = 4
RETRY_DELAY: Final[float] = 0.6
MAX_CHUNK_SIZE: Final[int] = 2000


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF\u0500-\u052F\u2DE0-\u2DFF\uA640-\uA69F\u1C80-\u1C8F]", text))


def create_chunks(lines: list[str], max_chunk_size: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
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
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
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

    def get_many(self, texts: list[str], source_lang: str, target_lang: str) -> dict[str, str]:
        if not texts:
            return {}
        with self.lock:
            placeholders = ",".join(["?"] * len(texts))
            query = f"""
                SELECT source_text, translated_text FROM translations
                WHERE source_lang = ? AND target_lang = ? AND source_text IN ({placeholders})
            """
            params = [source_lang, target_lang] + texts
            cur = self.conn.execute(query, params)
            rows = cur.fetchall()
            return {row[0]: row[1] for row in rows}

    def set_many(self, translations: dict[str, str], source_lang: str, target_lang: str) -> None:
        if not translations:
            return
        with self.lock:
            data = [(src, source_lang, target_lang, tgt) for src, tgt in translations.items()]
            self.conn.executemany(
                """
                INSERT INTO translations (source_text, source_lang, target_lang, translated_text, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_text, source_lang, target_lang) DO UPDATE SET
                    translated_text=excluded.translated_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                data,
            )
            self.conn.commit()

    def stats(self) -> dict:
        with self.lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM translations")
            total = cur.fetchone()[0] or 0
            cur = self.conn.execute("SELECT MAX(updated_at) FROM translations")
            last = cur.fetchone()[0]
            cur = self.conn.execute("""
                SELECT source_lang, target_lang, COUNT(*) as cnt
                FROM translations
                GROUP BY source_lang, target_lang
                ORDER BY cnt DESC
                LIMIT 100
                """)
            pairs = cur.fetchall()
            pairs_list = [{"source": r[0], "target": r[1], "count": r[2]} for r in pairs]
            return {"total_entries": total, "last_updated": last, "pairs": pairs_list}

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()


def translate_chunk_factory(source_lang: str, target_lang: str):
    def translate_chunk(chunk: list[str]) -> tuple[list[str], str | None]:
        chunk_text = "\n".join(chunk)
        translator = GoogleTranslator(source=source_lang, target=target_lang)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                translated = translator.translate(chunk_text)
                if translated is not None:
                    return (chunk, translated)
            except Exception as e:
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                jitter = random.uniform(0, delay * 0.25)
                sleep_time = delay + jitter
                logger.warning(
                    "Translate attempt {}/{} failed for chunk starting '{}...': {}. Retrying in {}s",
                    attempt,
                    RETRY_ATTEMPTS,
                    (chunk[0][:60] + "...") if chunk else "",
                    e,
                    sleep_time,
                )
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(sleep_time)
        return (chunk, None)

    return translate_chunk


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate lines in a text file with persistent caching.")
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
    parser.add_argument("--cache-stats", action="store_true", help="Show cache statistics and exit")
    args = parser.parse_args()
    db_path = Path(Path(args.db).expanduser())
    cache = TranslationCache(db_path)
    if args.cache_stats:
        stats = cache.stats()
        print("Translation cache stats")
        print("-----------------------")
        print(f"DB path: {db_path}")
        print(f"Total entries: {stats['total_entries']}")
        print(f"Last updated: {stats['last_updated']}")
        print("Top language pairs:")
        if stats["pairs"]:
            for p in stats["pairs"]:
                print(f"  {p['source']} -> {p['target']}: {p['count']}")
        else:
            print("  (no entries)")
        cache.close()
        return
    if not args.input:
        parser.error("the following arguments are required: -i/--input (unless --cache-stats is used)")
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: {}", input_path)
        cache.close()
        return
    try:
        with input_path.open(encoding="utf-8") as f:
            all_lines = [w.rstrip("\n") for w in f if w.strip() != ""]
    except Exception as e:
        logger.error("Error reading input file: {}", e)
        cache.close()
        return
    if not all_lines:
        print("No non-empty lines found in %s", input_path.name)
        cache.close()
        return
    source_lang = args.source
    target_lang = args.target
    if source_lang.lower() == "ru" or source_lang.lower().startswith("ru"):
        to_translate_raw = [line for line in all_lines if contains_cyrillic(line)]
        skipped_lines = [line for line in all_lines if not contains_cyrillic(line)]
    else:
        to_translate_raw = list(all_lines)
        skipped_lines = []
    print(
        "Loaded %d lines: %d flagged for translation, %d skipped",
        len(all_lines),
        len(to_translate_raw),
        len(skipped_lines),
    )
    if not to_translate_raw:
        print("No lines to translate for source_lang=%s", source_lang)
        cache.close()
        return
    seen: set[str] = set()
    to_translate_unique: list[str] = []
    for l in to_translate_raw:
        if l not in seen:
            seen.add(l)
            to_translate_unique.append(l)
    print(
        "Deduplicated: %d unique lines to translate (from %d total flagged)",
        len(to_translate_unique),
        len(to_translate_raw),
    )
    cached = cache.get_many(to_translate_unique, source_lang, target_lang)
    print("Cache hit: %d/%d", len(cached), len(to_translate_unique))
    results: dict[str, str] = dict(cached)
    remaining_to_translate = [l for l in to_translate_unique if l not in results]
    if remaining_to_translate:
        chunks = create_chunks(remaining_to_translate, args.max_chunk_size)
        num_workers = min(max(1, MAX_WORKERS), len(chunks))
        print(
            "Created %d chunk(s) from %d remaining lines (max %d chars per chunk), using %d worker(s)",
            len(chunks),
            len(remaining_to_translate),
            args.max_chunk_size,
            num_workers,
        )
        translate_chunk = translate_chunk_factory(source_lang, target_lang)
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_chunk = {executor.submit(translate_chunk, chunk): chunk for chunk in chunks}
            completed = 0
            total = len(future_to_chunk)
            to_cache: dict[str, str] = {}
            for future in as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                completed += 1
                try:
                    original_lines, translated_text = future.result()
                    if translated_text:
                        translated_lines = translated_text.splitlines()
                        if len(translated_lines) == len(original_lines):
                            for i, original_line in enumerate(original_lines):
                                tgt = translated_lines[i]
                                results[original_line] = tgt
                                to_cache[original_line] = tgt
                        else:
                            logger.warning(
                                "Line-count mismatch in chunk ({} original vs {} translated). Falling back to per-line translation for this chunk.",
                                len(original_lines),
                                len(translated_lines),
                            )
                            for line in original_lines:
                                try:
                                    per_line_translator = GoogleTranslator(source=source_lang, target=target_lang)
                                    t = per_line_translator.translate(line)
                                    if t is None:
                                        t = line
                                    results[line] = t
                                    to_cache[line] = t
                                except Exception as e:
                                    logger.error(
                                        "Per-line fallback failed for '{}': {}",
                                        line[:50],
                                        e,
                                    )
                                    results[line] = line
                                    to_cache[line] = line
                        print(
                            "Translated chunk %d/%d (sample: '%s' → '%s')",
                            completed,
                            total,
                            original_lines[0][:40] + ("..." if len(original_lines[0]) > 40 else ""),
                            results.get(original_lines[0], "")[:60],
                        )
                    else:
                        logger.error(
                            "Failed to translate chunk starting with: {}",
                            (chunk[0][:60] + "...") if chunk else "",
                        )
                        for line in chunk:
                            try:
                                t = GoogleTranslator(source=source_lang, target=target_lang).translate(line)
                                if t is None:
                                    t = line
                                results[line] = t
                                to_cache[line] = t
                            except Exception as e:
                                logger.error("Per-line retry failed for '{}': {}", line[:50], e)
                                results[line] = line
                                to_cache[line] = line
                except Exception as e:
                    logger.error(
                        "Unexpected error processing chunk starting with '{}': {}",
                        (chunk[0][:60] + "...") if chunk else "",
                        e,
                    )
            if to_cache:
                cache.set_many(to_cache, source_lang, target_lang)
                print("Saved %d new translations to cache", len(to_cache))
    else:
        print("Nothing left to translate after cache lookup.")
    output_path = input_path.with_name(f"{input_path.stem}_{target_lang}{input_path.suffix}")
    try:
        with output_path.open("w", encoding="utf-8") as f:
            translated_count = 0
            for line in all_lines:
                if line in results:
                    f.write(f"{results[line]}\n")
                    translated_count += 1
                else:
                    f.write(f"{line}\n")
        print(
            "Wrote %s: translated %d lines, kept %d lines unchanged",
            output_path.name,
            translated_count,
            len(all_lines) - translated_count,
        )
    except Exception as e:
        logger.error("Error writing output file: {}", e)
    cache.close()


if __name__ == "__main__":
    main()
