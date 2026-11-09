#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file in resumable chunks.
Splits the text into ~2500-character chunks, translates them with DeepL, deep_translator, translate, Bing, googletrans or pygoogletranslation (automatic fallback), saves the ``chunks.json`` progress periodically, throttles the requests and lists the failed chunk indexes.
Usage: dt1 [-i input.txt] [-o chunks.json] [-s en] [-t fr] Options: -i, --input Input text file.
Default: input.txt -o, --output Output JSON file.
Default: chunks.json --failed Failed chunk-index file.
Default: failed.txt -s, --source Source language code.
Default: en -t, --target Target language code.
Default: fr -b, --backend Translation backend.
Default: automatic fallback order.
-d, --delay Seconds between requests globally.
Default: 0.5 --chunk-size Maximum chunk length in characters.
Default: 2500 --save-every Save after this many completed chunks.
Default: 10 --no-continue Ignore an existing output file and start from scratch.
Examples: $ dt1 -i notes.txt -t de Command-line script: run ``python dt1.py`` or the ``dt1`` command.
The module is importable as well."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

from atomicio import atomic_save
from dh import MAX_WORKERS
from loguru import logger


Translator = Callable[[str], str]
FALLBACK_BACKENDS: tuple[str, ...] = (
    "deepl",
    "deep_translator",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
)
LANGUAGE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh": "zh-CN",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "he": "iw",
    "jv": "jw",
    "pt-br": "pt",
    "pt_br": "pt",
}


class TranslationError(RuntimeError):
    pass


class RequestLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._next_request_time = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_for = self._next_request_time - now
            if wait_for > 0:
                time.sleep(wait_for)
            self._next_request_time = time.monotonic() + self.delay


class SerializedTranslator:
    def __init__(self, translator: Translator) -> None:
        self._translator = translator
        self._lock = threading.Lock()

    def __call__(self, text: str) -> str:
        with self._lock:
            return self._translator(text)


def normalize_language(language: str) -> str:
    cleaned = language.strip()
    return LANGUAGE_ALIASES.get(cleaned.lower(), cleaned)


def _deep_translator_language(language: str) -> str:
    code = normalize_language(language)
    return {
        "zh-CN": "zh-CN",
        "zh-TW": "zh-TW",
    }.get(code, code)


def _deepl_language(language: str) -> str:
    code = normalize_language(language).upper()
    return {
        "EN": "EN-US",
        "PT": "PT-PT",
        "ZH-CN": "ZH-HANS",
        "ZH-TW": "ZH-HANT",
    }.get(code, code)


def _google_language(language: str) -> str:
    return normalize_language(language).lower().replace("_", "-")


def _make_deep_translator(source: str, target: str) -> Translator:
    from deep_translator import GoogleTranslator

    source_code = _deep_translator_language(source)
    target_code = _deep_translator_language(target)

    def translate(text: str) -> str:
        client = GoogleTranslator(source=source_code, target=target_code)
        return str(client.translate(text))

    return translate


def _make_deepl(source: str, target: str) -> Translator:
    import deepl

    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        msg = "DEEPL_API_KEY is not set"
        raise RuntimeError(msg)
    client = deepl.DeepLClient(api_key)
    target_code = _deepl_language(target)
    source_base = normalize_language(source).replace("_", "-").upper().split("-", 1)[0]
    source_code = None if source_base == "AUTO" else {"IW": "HE"}.get(source_base, source_base)

    def translate(text: str) -> str:
        result = client.translate_text(text, source_lang=source_code, target_lang=target_code)
        return str(result.text if hasattr(result, "text") else result)

    return translate


def _make_translate(source: str, target: str) -> Translator:
    from translate import Translator as TranslateClient

    source_code = normalize_language(source)
    target_code = normalize_language(target)

    def translate_text(text: str) -> str:
        client = TranslateClient(
            from_lang=source_code,
            to_lang=target_code,
        )
        return str(client.translate(text))

    return translate_text


def _make_translators_bing(source: str, target: str) -> Translator:
    import translators

    source_code = _google_language(source)
    target_code = _google_language(target)

    def translate_text(text: str) -> str:
        result = translators.translate_text(
            query=text,
            translator="bing",
            from_language=source_code,
            to_language=target_code,
        )
        return str(result)

    return translate_text


def _make_googletrans(source: str, target: str) -> Translator:
    from googletrans import Translator as GoogleTransClient

    source_code = _google_language(source)
    target_code = _google_language(target)

    def translate_text(text: str) -> str:
        client = GoogleTransClient()
        result = client.translate(text, src=source_code, dest=target_code)
        return str(result.text)

    return SerializedTranslator(translate_text)


def _make_pygoogletranslation(source: str, target: str) -> Translator:
    from pygoogletranslation import Translator as PyGoogleTranslator

    source_code = _google_language(source)
    target_code = _google_language(target)

    def translate_text(text: str) -> str:
        client = PyGoogleTranslator()
        result = client.translate(text, src=source_code, dest=target_code)
        return str(getattr(result, "text", result))

    return SerializedTranslator(translate_text)


BACKEND_FACTORIES: dict[str, Callable[[str, str], Translator]] = {
    "deep_translator": _make_deep_translator,
    "deepl": _make_deepl,
    "translate": _make_translate,
    "translators_bing": _make_translators_bing,
    "googletrans": _make_googletrans,
    "pygoogletranslation": _make_pygoogletranslation,
}


def configure_logging() -> None:
    logger.remove()
    logger.add(
        "translate_chunks.log",
        level="DEBUG",
        rotation="5 MB",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        sys.stderr,
        level="ERROR",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


def split_into_chunks(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        msg = "chunk size must be greater than zero"
        raise ValueError(msg)
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            candidate = remaining
            remaining = ""
        else:
            boundary = remaining.rfind(None if False else " ", 0, chunk_size + 1)
            whitespace_boundary = max(
                boundary,
                remaining.rfind("\t", 0, chunk_size + 1),
                remaining.rfind("\n", 0, chunk_size + 1),
                remaining.rfind("\r", 0, chunk_size + 1),
            )
            if whitespace_boundary > 0:
                candidate = remaining[:whitespace_boundary]
                remaining = remaining[whitespace_boundary:]
            else:
                candidate = remaining[:chunk_size]
                remaining = remaining[chunk_size:]
        candidate = candidate.strip()
        if candidate:
            chunks.append(candidate)
        remaining = remaining.lstrip()
    return chunks


def load_existing_output(path: Path, continue_run: bool) -> dict[str, str]:
    if not continue_run or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read existing output {path}: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(data, dict):
        msg = f"existing output {path} must contain a JSON object"
        raise RuntimeError(msg)
    return {str(index): str(value) for index, value in data.items() if isinstance(value, str)}


def append_failed(path: Path, chunk_index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"{chunk_index}\n")


def is_identity_translation(source: str, translated: str) -> bool:
    return source.strip().casefold() == translated.strip().casefold()


def choose_backend(requested: str | None) -> str:
    if requested:
        if requested == "auto":
            requested = None
        else:
            if requested not in BACKEND_FACTORIES:
                valid = ", ".join(sorted(BACKEND_FACTORIES))
                msg = f"unsupported backend {requested!r}; choose one of: {valid}"
                raise RuntimeError(msg)
            if requested == "deepl" and not os.environ.get("DEEPL_API_KEY"):
                msg = "backend 'deepl' requires the DEEPL_API_KEY environment variable"
                raise RuntimeError(msg)
            return requested
    for backend in FALLBACK_BACKENDS:
        if backend == "deepl" and not os.environ.get("DEEPL_API_KEY"):
            continue
        try:
            if backend == "deep_translator":
                import deep_translator
            elif backend == "deepl":
                import deepl
            elif backend == "translate":
                import translate
            elif backend == "translators_bing":
                import translators
            elif backend == "googletrans":
                import googletrans
            elif backend == "pygoogletranslation":
                import pygoogletranslation
        except ImportError:
            logger.debug("Backend {} is unavailable", backend)
            continue
        return backend
    packages = ", ".join(FALLBACK_BACKENDS)
    msg = f"no translation backend is installed; install one of: {packages}"
    raise RuntimeError(msg)


def translate_one(
    index: int,
    text: str,
    translator: Translator,
    limiter: RequestLimiter,
    attempts: int = 3,
) -> tuple[int, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            limiter.wait()
            translated = str(translator(text)).strip()
            if not translated:
                msg = "backend returned an empty translation"
                raise TranslationError(msg)
            if is_identity_translation(text, translated):
                msg = "backend returned the source text unchanged"
                raise TranslationError(msg)
            logger.debug(
                "Chunk {} translated successfully on attempt {}",
                index,
                attempt,
            )
            return index, translated
        except Exception as exc:
            last_error = exc
            logger.debug(
                "Chunk {} attempt {}/{} failed: {}",
                index,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    msg = f"chunk {index} failed after {attempts} attempts: {last_error}"
    raise TranslationError(msg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate a text file in resumable chunks.")
    parser.add_argument(
        "-i",
        "--input",
        default="input.txt",
        help="Input text file. Default: input.txt",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="chunks.json",
        help="Output JSON file. Default: chunks.json",
    )
    parser.add_argument(
        "--failed",
        default="failed.txt",
        help="Failed chunk-index file. Default: failed.txt",
    )
    parser.add_argument(
        "-s",
        "--source",
        default="en",
        help="Source language code. Default: en",
    )
    parser.add_argument(
        "-t",
        "--target",
        default="fr",
        help="Target language code. Default: fr",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=["auto", *BACKEND_FACTORIES.keys()],
        default="auto",
        help="Translation backend. Default: automatic fallback order.",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between requests globally. Default: 0.5",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2500,
        help="Maximum chunk length in characters. Default: 2500",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Save after this many completed chunks. Default: 10",
    )
    parser.add_argument(
        "--no-continue",
        action="store_true",
        help="Ignore an existing output file and start from scratch.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= MAX_WORKERS <= 2:
        msg = "--workers must be between 1 and 2"
        raise ValueError(msg)
    if args.delay < 0:
        msg = "--delay cannot be negative"
        raise ValueError(msg)
    if args.chunk_size <= 0:
        msg = "--chunk-size must be greater than zero"
        raise ValueError(msg)
    if args.save_every <= 0:
        msg = "--save-every must be greater than zero"
        raise ValueError(msg)


def read_input(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read input file {path}: {exc}"
        raise RuntimeError(msg) from exc


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    input_path = Path(args.input)
    output_path = Path(args.output)
    failed_path = Path(args.failed)
    source_text = read_input(input_path)
    chunks = split_into_chunks(source_text, args.chunk_size)
    translations = load_existing_output(
        output_path,
        continue_run=not args.no_continue,
    )
    translations = {
        index: value for index, value in translations.items() if index.isdigit() and int(index) < len(chunks)
    }
    pending = [(index, chunk) for index, chunk in enumerate(chunks) if str(index) not in translations]
    logger.debug(
        "Input produced {} chunks; {} already translated; {} pending",
        len(chunks),
        len(translations),
        len(pending),
    )
    if not pending:
        atomic_save(output_path, translations)
        logger.debug("Nothing to translate")
        return 0
    backend = choose_backend(None if args.backend == "auto" else args.backend)
    logger.debug("Using backend {}", backend)
    translator = BACKEND_FACTORIES[backend](
        normalize_language(args.source),
        normalize_language(args.target),
    )
    limiter = RequestLimiter(args.delay)
    completed_since_save = 0
    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="translator",
    ) as executor:
        future_to_index: dict[Future[tuple[int, str]], int] = {
            executor.submit(
                translate_one,
                index,
                chunk,
                translator,
                limiter,
            ): index
            for index, chunk in pending
        }
        try:
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    completed_index, translated = future.result()
                    translations[str(completed_index)] = translated
                    completed_since_save += 1
                    logger.debug(
                        "Completed chunk {} ({}/{})",
                        completed_index,
                        len(translations),
                        len(chunks),
                    )
                    if completed_since_save >= args.save_every:
                        atomic_save(output_path, translations)
                        completed_since_save = 0
                except Exception as exc:
                    logger.error("Chunk {} failed: {}", index, exc)
                    append_failed(failed_path, index)
        except KeyboardInterrupt:
            logger.error("Interrupted; saving completed translations")
            for future in future_to_index:
                future.cancel()
            raise
    atomic_save(output_path, translations)
    failed_count = len(chunks) - len(translations)
    if failed_count:
        logger.error(
            "Finished with {} failed or incomplete chunks; see {}",
            failed_count,
            failed_path,
        )
        return 1
    logger.debug("All {} chunks translated successfully", len(chunks))
    return 0


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        return run(args)
    except KeyboardInterrupt:
        logger.error("Interrupted. Existing completed translations were saved.")
        return 130
    except Exception as exc:
        logger.error("Fatal error: {}", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
