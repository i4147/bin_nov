#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file in chunks while saving the progress.
Splits the input into 2500-character chunks, translates them with the chosen backend (deep_translator, DeepL, translate, Bing, googletrans, pygoogletranslation), saves ``chunks.json`` every ten chunks, keeps a ``failed.txt`` list and ``--no-continue`` starts from scratch.
Usage: dt4 [-i input.txt] [-o chunks.json] [-s en] [-t fr] Options: -i, --input Input text file -o, --output Output JSON file --failed Failed chunk indices file -s, --source Source language code -t, --target Target language code -b, --backend Translator backend -d, --delay Delay between requests in seconds --chunk-size Characters per chunk --save-every Save JSON every N chunks --no-continue Start fresh, ignore existing output Examples: $ dt4 -i book.txt -t en Command-line script: run ``python dt4.py`` or the ``dt4`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import sys
from threading import Lock
import time
from typing import Any, Callable, Iterable, Optional, TextIO

from deep_translator import GoogleTranslator
from deep_translator.exceptions import NotValidPayload, TranslationNotFound
from dh import MAX_WORKERS
from loguru import logger

DEFAULT_INPUT: str = "input.txt"
DEFAULT_OUTPUT: str = "chunks.json"
DEFAULT_FAILED: str = "failed.txt"
DEFAULT_SOURCE: str = "en"
DEFAULT_TARGET: str = "fr"
DEFAULT_DELAY: float = 0.5
DEFAULT_CHUNK_SIZE: int = 2500
DEFAULT_SAVE_EVERY: int = 10
MAX_RETRIES: int = 3
BACKOFF_BASE: int = 2
LANG_MAPPING: dict[str, dict[str, str]] = {
    "deep_translator": {
        "en": "english",
        "fr": "french",
        "es": "spanish",
        "de": "german",
        "it": "italian",
        "pt": "portuguese",
        "ru": "russian",
        "zh": "chinese",
        "ja": "japanese",
        "ar": "arabic",
    },
    "deepl": {
        "en": "EN",
        "fr": "FR",
        "es": "ES",
        "de": "DE",
        "it": "IT",
        "pt": "PT",
        "ru": "RU",
        "zh": "ZH",
        "ja": "JA",
        "ar": "AR",
    },
    "translate": {
        "en": "en",
        "fr": "fr",
        "es": "es",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh",
        "ja": "ja",
        "ar": "ar",
    },
    "translators_bing": {
        "en": "en",
        "fr": "fr",
        "es": "es",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh-CHS",
        "ja": "ja",
        "ar": "ar",
    },
    "googletrans": {
        "en": "en",
        "fr": "fr",
        "es": "es",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh-cn",
        "ja": "ja",
        "ar": "ar",
    },
    "pygoogletranslation": {
        "en": "en",
        "fr": "fr",
        "es": "es",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh",
        "ja": "ja",
        "ar": "ar",
    },
}
shutdown_flag: bool = False


def setup_logging(log_file: str = "translate_chunks.log") -> None:
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    logger.add(log_file, level="DEBUG", rotation="10 MB")


def signal_handler(sig: int, frame: Any) -> None:
    global shutdown_flag
    shutdown_flag = True
    logger.info("Shutdown signal received. Waiting for current tasks to complete...")


def split_into_chunks(text: str, chunk_size: int) -> list[str]:
    if not text:
        return []
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length and not text[end].isspace():
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end if end != start else end + 1
    return chunks


def load_existing_output(output_path: Path) -> dict[str, str]:
    if output_path.exists():
        try:
            with output_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Failed to load existing output: {e}")
    return {}


def save_json_atomic(data: dict[str, str], output_path: Path) -> None:
    temp_path = output_path.with_suffix(".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(output_path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise


def is_identity_translation(text: str, translated: str) -> bool:
    norm_orig = text.lower().strip()
    norm_trans = translated.lower().strip()
    return norm_orig == norm_trans


def make_deep_translator(source: str, target: str) -> Callable[[str], str]:
    source_lang = LANG_MAPPING["deep_translator"].get(source, source)
    target_lang = LANG_MAPPING["deep_translator"].get(target, target)

    def translate(text: str) -> str:
        try:
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            return translator.translate(text)
        except (TranslationNotFound, NotValidPayload) as e:
            logger.error(f"Translation failed for deep_translator: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in deep_translator: {e}")
            raise

    return translate


def make_deepl_translator(source: str, target: str) -> Callable[[str], str]:
    try:
        import deepl
    except ImportError:
        msg = "deepl package not installed"
        raise ImportError(msg)
    source_lang = LANG_MAPPING["deepl"].get(source, source)
    target_lang = LANG_MAPPING["deepl"].get(target, target)

    def translate(text: str) -> str:
        try:
            auth_key = os.getenv("DEEPL_API_KEY")
            if not auth_key:
                msg = "DEEPL_API_KEY environment variable not set"
                raise ValueError(msg)
            translator = deepl.Translator(auth_key)
            result = translator.translate_text(text, target_lang=target_lang, source_lang=source_lang)
            return result.text
        except Exception as e:
            logger.error(f"Translation failed for deepl: {e}")
            raise

    return translate


def make_translate_translator(source: str, target: str) -> Callable[[str], str]:
    try:
        from translate import Translator
    except ImportError:
        msg = "translate package not installed"
        raise ImportError(msg)
    source_lang = LANG_MAPPING["translate"].get(source, source)
    target_lang = LANG_MAPPING["translate"].get(target, target)

    def translate(text: str) -> str:
        try:
            translator = Translator(from_lang=source_lang, to_lang=target_lang)
            return translator.translate(text)
        except Exception as e:
            logger.error(f"Translation failed for translate: {e}")
            raise

    return translate


def make_translators_bing_translator(source: str, target: str) -> Callable[[str], str]:
    try:
        import translators as ts
    except ImportError:
        msg = "translators package not installed"
        raise ImportError(msg)
    source_lang = LANG_MAPPING["translators_bing"].get(source, source)
    target_lang = LANG_MAPPING["translators_bing"].get(target, target)

    def translate(text: str) -> str:
        try:
            return ts.translate_text(
                text,
                translator="bing",
                from_language=source_lang,
                to_language=target_lang,
            )
        except Exception as e:
            logger.error(f"Translation failed for translators_bing: {e}")
            raise

    return translate


def make_googletrans_translator(source: str, target: str) -> Callable[[str], str]:
    try:
        from googletrans import Translator
    except ImportError:
        msg = "googletrans package not installed"
        raise ImportError(msg)
    source_lang = LANG_MAPPING["googletrans"].get(source, source)
    target_lang = LANG_MAPPING["googletrans"].get(target, target)
    lock = Lock()

    def translate(text: str) -> str:
        try:
            with lock:
                translator = Translator()
                result = translator.translate(text, src=source_lang, dest=target_lang)
                return result.text
        except Exception as e:
            logger.error(f"Translation failed for googletrans: {e}")
            raise

    return translate


def make_pygoogletranslation_translator(source: str, target: str) -> Callable[[str], str]:
    try:
        from pygoogletranslation import Translator
    except ImportError:
        msg = "pygoogletranslation package not installed"
        raise ImportError(msg)
    source_lang = LANG_MAPPING["pygoogletranslation"].get(source, source)
    target_lang = LANG_MAPPING["pygoogletranslation"].get(target, target)
    lock = Lock()

    def translate(text: str) -> str:
        try:
            with lock:
                translator = Translator()
                return translator.translate(text, source=source_lang, target=target_lang)
        except Exception as e:
            logger.error(f"Translation failed for pygoogletranslation: {e}")
            raise

    return translate


def select_backend(source: str, target: str, backend: Optional[str] = None) -> Callable[[str], str]:
    backends = [
        ("deepl", make_deepl_translator),
        ("deep_translator", make_deep_translator),
        ("translate", make_translate_translator),
        ("translators_bing", make_translators_bing_translator),
        ("googletrans", make_googletrans_translator),
        ("pygoogletranslation", make_pygoogletranslation_translator),
    ]
    if backend:
        for name, factory in backends:
            if name == backend:
                try:
                    return factory(source, target)
                except ImportError:
                    logger.warning(f"Backend {backend} not available")
                    break
        msg = f"Specified backend {backend} not available"
        raise ValueError(msg)
    for name, factory in backends:
        try:
            return factory(source, target)
        except ImportError:
            logger.debug(f"Backend {name} not available")
            continue
    msg = "No translation backend available"
    raise RuntimeError(msg)


def translate_chunk(
    chunk: str,
    index: int,
    translator: Callable[[str], str],
    delay: float,
    max_retries: int = MAX_RETRIES,
    backoff_base: int = BACKOFF_BASE,
) -> tuple[int, Optional[str]]:
    if shutdown_flag:
        return index, None
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                backoff = backoff_base**attempt
                logger.info(f"Retry {attempt + 1}/{max_retries} for chunk {index} after {backoff}s delay")
                time.sleep(backoff)
            translated = translator(chunk)
            time.sleep(delay)
            if is_identity_translation(chunk, translated):
                logger.warning(f"Identity translation detected for chunk {index}, retrying")
                msg = "Identity translation detected"
                raise ValueError(msg)
            return index, translated
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed for chunk {index}: {e}")
            if attempt == max_retries - 1:
                return index, None
    return index, None


def process_chunks(
    chunks: list[str],
    translator: Callable[[str], str],
    workers: int,
    delay: float,
    save_every: int,
    output_path: Path,
    failed_path: Path,
    existing_output: dict[str, str],
) -> None:
    results = existing_output.copy()
    failed_indices = set()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(translate_chunk, chunk, i, translator, delay): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            if shutdown_flag:
                break
            index = futures[future]
            try:
                chunk_index, translated = future.result()
                if translated is not None:
                    results[str(chunk_index)] = translated
                    logger.info(f"Translated chunk {chunk_index}")
                    if len(results) % save_every == 0:
                        save_json_atomic(results, output_path)
                        logger.info(f"Periodic save after {len(results)} chunks")
                else:
                    failed_indices.add(chunk_index)
                    logger.error(f"Failed to translate chunk {chunk_index}")
            except Exception as e:
                failed_indices.add(index)
                logger.error(f"Unexpected error processing chunk {index}: {e}")
    if results and not shutdown_flag:
        save_json_atomic(results, output_path)
    if failed_indices:
        with failed_path.open("a", encoding="utf-8") as f:
            for index in sorted(failed_indices):
                f.write(f"{index}\n")
        logger.error(f"Saved {len(failed_indices)} failed chunk indices to {failed_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate text file in chunks")
    parser.add_argument("-i", "--input", default=DEFAULT_INPUT, help="Input text file")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="Output JSON file")
    parser.add_argument("--failed", default=DEFAULT_FAILED, help="Failed chunk indices file")
    parser.add_argument("-s", "--source", default=DEFAULT_SOURCE, help="Source language code")
    parser.add_argument("-t", "--target", default=DEFAULT_TARGET, help="Target language code")
    parser.add_argument("-b", "--backend", help="Translator backend")
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Delay between requests in seconds",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Characters per chunk",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=DEFAULT_SAVE_EVERY,
        help="Save JSON every N chunks",
    )
    parser.add_argument("--no-continue", action="store_true", help="Start fresh, ignore existing output")
    args = parser.parse_args()
    if MAX_WORKERS < 1 or MAX_WORKERS > 2:
        parser.error("Workers must be between 1 and 2")
    setup_logging()
    logger.info(f"Starting translation with args: {vars(args)}")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    input_path = Path(args.input)
    output_path = Path(args.output)
    failed_path = Path(args.failed)
    if not input_path.exists():
        logger.error(f"Input file {input_path} does not exist")
        sys.exit(1)
    existing_output = {}
    if not args.no_continue and output_path.exists():
        existing_output = load_existing_output(output_path)
        logger.info(f"Loaded existing output with {len(existing_output)} chunks")
    try:
        with input_path.open("r", encoding="utf-8") as f:
            text = f.read()
        logger.info(f"Read {len(text)} characters from {input_path}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        sys.exit(1)
    chunks = split_into_chunks(text, args.chunk_size)
    logger.info(f"Split text into {len(chunks)} chunks")
    if existing_output:
        chunks = [chunk for i, chunk in enumerate(chunks) if str(i) not in existing_output]
        logger.info(f"Skipping {len(existing_output)} already translated chunks, processing {len(chunks)} remaining")
    if not chunks:
        logger.info("No new chunks to process")
        return
    try:
        translator = select_backend(args.source, args.target, args.backend)
        logger.info(f"Using {translator.__module__} backend")
    except Exception as e:
        logger.error(f"Failed to initialize translator: {e}")
        sys.exit(1)
    try:
        process_chunks(
            chunks=chunks,
            translator=translator,
            workers=MAX_WORKERS,
            delay=args.delay,
            save_every=args.save_every,
            output_path=output_path,
            failed_path=failed_path,
            existing_output=existing_output,
        )
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        sys.exit(1)
    logger.info("Translation completed successfully")


if __name__ == "__main__":
    main()
