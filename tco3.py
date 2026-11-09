#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file in independently processed chunks."""


import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import importlib
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
REQUEST_LOCK = threading.Lock()
GOOGLETRANS_LOCK = threading.Lock()
PYGOOGLETRANSLATION_LOCK = threading.Lock()
LAST_REQUEST_TIME: float = 0.0


def configure_logging() -> None:
    logger.remove()
    logger.add(
        "translate_chunks.log",
        level="DEBUG",
        encoding="utf-8",
        rotation="10 MB",
        retention=3,
    )
    logger.add(sys.stderr, level="ERROR")


def normalize_language(language: str) -> str:
    return language.strip().replace("_", "-").lower()


def language_aliases(language: str) -> list[str]:
    normalized = normalize_language(language)
    base = normalized.split("-", 1)[0]
    aliases = [normalized, base]
    if base == "zh":
        aliases.extend(["zh-cn", "zh-CN", "zh"])
    elif base == "he":
        aliases.extend(["iw", "he"])
    elif base == "id":
        aliases.extend(["in", "id"])
    elif base == "pt":
        aliases.extend(["pt-br", "pt-BR", "pt"])
    elif base == "en":
        aliases.extend(["en-us", "en-US", "en"])
    elif base == "fr":
        aliases.extend(["fr-fr", "fr-FR", "fr"])
    return list(dict.fromkeys(aliases))


def backend_language(language: str, backend: str) -> str:
    normalized = normalize_language(language)
    base = normalized.split("-", 1)[0]
    maps: dict[str, dict[str, str]] = {
        "deepl": {
            "en": "EN",
            "fr": "FR",
            "de": "DE",
            "es": "ES",
            "it": "IT",
            "pt": "PT-PT",
            "pt-br": "PT-BR",
            "nl": "NL",
            "pl": "PL",
            "ru": "RU",
            "ja": "JA",
            "zh": "ZH",
            "ko": "KO",
            "da": "DA",
            "sv": "SV",
            "no": "NB",
            "fi": "FI",
            "el": "EL",
            "cs": "CS",
            "ro": "RO",
            "hu": "HU",
            "uk": "UK",
            "bg": "BG",
            "sk": "SK",
            "sl": "SL",
            "et": "ET",
            "lv": "LV",
            "lt": "LT",
            "tr": "TR",
        },
        "translate": {
            "zh": "zh",
            "no": "no",
        },
        "translators_bing": {
            "zh": "zh-Hans",
            "zh-cn": "zh-Hans",
            "zh-tw": "zh-Hant",
            "no": "nb",
        },
        "googletrans": {
            "zh": "zh-cn",
            "zh-cn": "zh-cn",
            "zh-tw": "zh-tw",
            "no": "no",
        },
        "pygoogletranslation": {
            "zh": "zh-cn",
            "zh-cn": "zh-cn",
            "zh-tw": "zh-tw",
            "no": "no",
        },
    }
    backend_map = maps.get(backend, {})
    return backend_map.get(normalized, backend_map.get(base, base))


def import_optional(module_name: str) -> Any:
    return importlib.import_module(module_name)


def _make_deep_translator(source: str, target: str) -> Translator:
    from_lang = backend_language(source, "deep_translator")
    to_lang = backend_language(target, "deep_translator")

    def translate(text: str) -> str:
        module = import_optional("deep_translator")
        translator = module.GoogleTranslator(source=from_lang, target=to_lang)
        result = translator.translate(text)
        return str(result)

    return translate


def _make_deepl(source: str, target: str) -> Translator:
    api_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not api_key:
        msg = "DEEPL_API_KEY is not set"
        raise RuntimeError(msg)
    source_lang = backend_language(source, "deepl")
    target_lang = backend_language(target, "deepl")

    def translate(text: str) -> str:
        module = import_optional("deepl")
        client = module.Translator(api_key)
        result = client.translate_text(
            text,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        return str(result)

    return translate


def _make_translate(source: str, target: str) -> Translator:
    from_lang = backend_language(source, "translate")
    to_lang = backend_language(target, "translate")

    def translate(text: str) -> str:
        module = import_optional("translate")
        translator = module.Translator(
            from_lang=from_lang,
            to_lang=to_lang,
        )
        return str(translator.translate(text))

    return translate


def _make_translators_bing(source: str, target: str) -> Translator:
    from_language = backend_language(source, "translators_bing")
    to_language = backend_language(target, "translators_bing")

    def translate(text: str) -> str:
        module = import_optional("translators")
        result = module.translate_text(
            text,
            translator="bing",
            from_language=from_language,
            to_language=to_language,
        )
        return str(result)

    return translate


def _make_googletrans(source: str, target: str) -> Translator:
    source_lang = backend_language(source, "googletrans")
    target_lang = backend_language(target, "googletrans")

    def translate(text: str) -> str:
        module = import_optional("googletrans")
        with GOOGLETRANS_LOCK:
            client = module.Translator()
            result = client.translate(
                text,
                src=source_lang,
                dest=target_lang,
            )
            return str(result.text)

    return translate


def _make_pygoogletranslation(source: str, target: str) -> Translator:
    source_lang = backend_language(source, "pygoogletranslation")
    target_lang = backend_language(target, "pygoogletranslation")

    def translate(text: str) -> str:
        module = import_optional("pygoogletranslation")
        with PYGOOGLETRANSLATION_LOCK:
            translator_class = getattr(module, "Translator", None)
            if translator_class is None:
                msg = "pygoogletranslation does not expose Translator"
                raise RuntimeError(msg)
            client = translator_class()
            result = client.translate(
                text,
                src=source_lang,
                dest=target_lang,
            )
            if hasattr(result, "text"):
                return str(result.text)
            return str(result)

    return translate


def _make_boto3(source: str, target: str) -> Translator:
    def translate(text: str) -> str:
        module = import_optional("boto3")
        client = module.client("translate")
        result = client.translate_text(
            Text=text,
            SourceLanguageCode=source,
            TargetLanguageCode=target,
        )
        return str(result["TranslatedText"])

    return translate


def _make_baidu(source: str, target: str) -> Translator:
    def translate(text: str) -> str:
        module = import_optional("baidu")
        if hasattr(module, "translate"):
            return str(module.translate(text, source, target))
        msg = "Unsupported baidu package API"
        raise RuntimeError(msg)

    return translate


def _make_alibaba(source: str, target: str) -> Translator:
    def translate(text: str) -> str:
        module = import_optional("alibaba")
        if hasattr(module, "translate"):
            return str(module.translate(text, source, target))
        msg = "Unsupported alibaba package API"
        raise RuntimeError(msg)

    return translate


def _make_watson(source: str, target: str) -> Translator:
    def translate(text: str) -> str:
        module = import_optional("ibm_watson")
        msg = f"Unsupported watson package API: {module.__name__}"
        raise RuntimeError(msg)

    return translate


def _make_azure(source: str, target: str) -> Translator:
    def translate(text: str) -> str:
        import_optional("requests")
        endpoint = os.environ.get("AZURE_TRANSLATOR_ENDPOINT", "").strip()
        key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
        region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
        if not endpoint or not key:
            msg = "AZURE_TRANSLATOR_ENDPOINT and AZURE_TRANSLATOR_KEY are required"
            raise RuntimeError(msg)
        import requests

        response = requests.post(
            f"{endpoint.rstrip('/')}/translate",
            params={"api-version": "3.0", "from": source, "to": target},
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Ocp-Apim-Subscription-Region": region,
                "Content-Type": "application/json",
            },
            json=[{"Text": text}],
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload[0]["translations"][0]["text"])

    return translate


FACTORIES: dict[str, Callable[[str, str], Translator]] = {
    "deep_translator": _make_deep_translator,
    "deepl": _make_deepl,
    "translate": _make_translate,
    "translators_bing": _make_translators_bing,
    "googletrans": _make_googletrans,
    "pygoogletranslation": _make_pygoogletranslation,
    "boto3": _make_boto3,
    "baidu": _make_baidu,
    "alibaba": _make_alibaba,
    "watson": _make_watson,
    "azure": _make_azure,
}
FALLBACK_ORDER: list[str] = [
    "deepl",
    "deep_translator",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
]


def wait_for_request_slot(delay: float) -> None:
    global LAST_REQUEST_TIME
    with REQUEST_LOCK:
        now = time.monotonic()
        remaining = delay - (now - LAST_REQUEST_TIME)
        if remaining > 0:
            time.sleep(remaining)
        LAST_REQUEST_TIME = time.monotonic()


def select_backend(
    requested: str | None,
    source: str,
    target: str,
) -> tuple[str, Translator]:
    candidates = [requested] if requested else FALLBACK_ORDER
    failures: list[str] = []
    for name in candidates:
        if name is None:
            continue
        backend = name.strip().lower()
        factory = FACTORIES.get(backend)
        if factory is None:
            failures.append(f"{backend}: unsupported backend")
            continue
        try:
            translator = factory(source, target)
            if (
                backend == "deepl"
                and not os.environ.get(
                    "DEEPL_API_KEY",
                    "",
                ).strip()
            ):
                msg = "DEEPL_API_KEY is not set"
                raise RuntimeError(msg)
            logger.info("Using translator backend: {}", backend)
            return backend, translator
        except Exception as exc:
            failures.append(f"{backend}: {exc}")
            logger.debug("Backend unavailable: {}", failures[-1])
            if requested:
                break
    details = "; ".join(failures)
    msg = f"No usable translation backend found: {details}"
    raise RuntimeError(msg)


def split_chunks(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        msg = "chunk size must be greater than zero"
        raise ValueError(msg)
    chunks: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        end = min(position + chunk_size, length)
        if end < length:
            boundary = text.rfind(" ", position, end)
            if boundary > position:
                end = boundary
        chunk = text[position:end].strip()
        if chunk:
            chunks.append(chunk)
        position = end
    return chunks


def load_results(path: Path, continue_mode: bool) -> dict[str, str]:
    if not continue_mode or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            msg = "output JSON must contain an object"
            raise ValueError(msg)
        return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}
    except Exception as exc:
        logger.error("Could not load existing output {}: {}", path, exc)
        return {}


def append_failed(path: Path, index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{index}\n")


def translate_with_retry(
    index: int,
    text: str,
    translator: Translator,
    delay: float,
    attempts: int = 3,
) -> tuple[int, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            wait_for_request_slot(delay)
            translated = translator(text).strip()
            if not translated:
                msg = "translator returned empty text"
                raise RuntimeError(msg)
            if translated.casefold() == text.casefold():
                msg = "translator returned identity output"
                raise RuntimeError(msg)
            logger.debug(
                "Translated chunk {} on attempt {}",
                index,
                attempt,
            )
            return index, translated
        except Exception as exc:
            last_error = exc
            logger.debug(
                "Chunk {} attempt {} failed: {}",
                index,
                attempt,
                exc,
            )
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        msg = "translation failed without an exception"
        raise RuntimeError(msg)
    msg = f"chunk {index} failed after {attempts} attempts: {last_error}"
    raise RuntimeError(msg) from last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a text file in independently processed chunks.",
    )
    parser.add_argument(
        "-i",
        "--input",
        default="input.txt",
        help="Input text file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="chunks.json",
        help="Output JSON file.",
    )
    parser.add_argument(
        "--failed",
        default="failed.txt",
        help="Failed chunk indices file.",
    )
    parser.add_argument(
        "-s",
        "--source",
        default="en",
        help="Source language code.",
    )
    parser.add_argument(
        "-t",
        "--target",
        default="fr",
        help="Target language code.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=sorted(FACTORIES),
        default=None,
        help="Translator backend.",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.5,
        help="Minimum delay between requests.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2500,
        help="Maximum chunk size in characters.",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Save JSON after every N completed chunks.",
    )
    parser.add_argument(
        "--no-continue",
        action="store_true",
        help="Ignore an existing output file.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= MAX_WORKERS <= 2:
        msg = "--workers must be between 1 and 2"
        raise ValueError(msg)
    if args.delay < 0:
        msg = "--delay must not be negative"
        raise ValueError(msg)
    if args.chunk_size <= 0:
        msg = "--chunk-size must be greater than zero"
        raise ValueError(msg)
    if args.save_every <= 0:
        msg = "--save-every must be greater than zero"
        raise ValueError(msg)


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    input_path = Path(args.input)
    output_path = Path(args.output)
    failed_path = Path(args.failed)
    if not input_path.exists():
        msg = f"Input file does not exist: {input_path}"
        raise FileNotFoundError(msg)
    text = input_path.read_text(encoding="utf-8")
    chunks = split_chunks(text, args.chunk_size)
    results = load_results(output_path, not args.no_continue)
    backend_name, translator = select_backend(
        args.backend,
        args.source,
        args.target,
    )
    pending: list[tuple[int, str]] = [(index, chunk) for index, chunk in enumerate(chunks) if str(index) not in results]
    logger.info(
        "Backend={}, chunks={}, already translated={}, pending={}",
        backend_name,
        len(chunks),
        len(results),
        len(pending),
    )
    if not pending:
        atomic_save(output_path, results)
        return 0
    completed_since_save = 0
    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="translator",
    ) as executor:
        futures: dict[Future[tuple[int, str]], int] = {
            executor.submit(
                translate_with_retry,
                index,
                chunk,
                translator,
                args.delay,
            ): index
            for index, chunk in pending
        }
        try:
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result_index, translated = future.result()
                    results[str(result_index)] = translated
                    completed_since_save += 1
                    logger.info(
                        "Completed chunk {}/{}",
                        result_index + 1,
                        len(chunks),
                    )
                    if completed_since_save >= args.save_every:
                        atomic_save(output_path, results)
                        completed_since_save = 0
                except Exception as exc:
                    logger.error("Chunk {} failed: {}", index, exc)
                    append_failed(failed_path, index)
        except KeyboardInterrupt:
            logger.error("Interrupted; cancelling unfinished translations")
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            atomic_save(output_path, results)
            return 130
    atomic_save(output_path, results)
    return 0


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        return run(args)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
