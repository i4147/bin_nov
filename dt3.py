#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file in independently resumable chunks with many backends.
Supports DeepL, deep_translator, translate, Bing, googletrans, pygoogletranslation, boto3, Azure, Baidu, Alibaba and Watson, maps the language codes per backend, atomically saves the chunk results, skips the identity translations and logs to a rotating file.
Usage: dt3 -i input.txt -o out.json [-s auto] [-t en] [-b BACKEND] Options: -i, --input -o, --output --failed -s, --source -t, --target -b, --backend -d, --delay --chunk-size --save-every --no-continue Examples: $ dt3 -i article.txt -o article.json -t en Command-line script: run ``python dt3.py`` or the ``dt3`` command.
The module is importable as well."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import threading
import time
from typing import Callable, TypeAlias

from dh import MAX_WORKERS
from loguru import logger

Translator: TypeAlias = Callable[[str], str]
LANGUAGE_CODES: dict[str, str] = {
    "auto": "auto",
    "ar": "ar",
    "bg": "bg",
    "bn": "bn",
    "ca": "ca",
    "cs": "cs",
    "da": "da",
    "de": "de",
    "el": "el",
    "en": "en",
    "es": "es",
    "et": "et",
    "fa": "fa",
    "fi": "fi",
    "fr": "fr",
    "he": "he",
    "hi": "hi",
    "hr": "hr",
    "hu": "hu",
    "id": "id",
    "it": "it",
    "ja": "ja",
    "ko": "ko",
    "lt": "lt",
    "lv": "lv",
    "ms": "ms",
    "nb": "no",
    "nl": "nl",
    "no": "no",
    "pl": "pl",
    "pt": "pt",
    "pt-br": "pt",
    "ro": "ro",
    "ru": "ru",
    "sk": "sk",
    "sl": "sl",
    "sr": "sr",
    "sv": "sv",
    "sw": "sw",
    "ta": "ta",
    "te": "te",
    "th": "th",
    "tl": "tl",
    "tr": "tr",
    "uk": "uk",
    "ur": "ur",
    "vi": "vi",
    "zh": "zh-cn",
    "zh-cn": "zh-cn",
    "zh-tw": "zh-tw",
}
DEEPL_LANGUAGE_CODES: dict[str, str] = {
    **LANGUAGE_CODES,
    "en": "EN",
    "en-us": "EN-US",
    "en-gb": "EN-GB",
    "pt": "PT-PT",
    "pt-br": "PT-BR",
    "zh": "ZH",
    "zh-cn": "ZH",
}
GOOGLE_LANGUAGE_CODES: dict[str, str] = {
    **LANGUAGE_CODES,
    "zh": "zh-cn",
}
_TRANSLATION_LOCK = threading.Lock()


class RequestLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            remaining = self.delay - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()


def _map_language(
    language: str,
    mapping: dict[str, str],
) -> str:
    normalized = language.strip().lower()
    if normalized not in mapping:
        supported = ", ".join(sorted(mapping))
        msg = f"Unsupported language code {language!r}. Supported codes include: {supported}"
        raise ValueError(msg)
    return mapping[normalized]


def _make_deep_translator(source: str, target: str) -> Translator:
    from deep_translator import GoogleTranslator

    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        translator = GoogleTranslator(
            source=mapped_source,
            target=mapped_target,
        )
        result = translator.translate(text)
        if not isinstance(result, str):
            msg = "deep_translator returned a non-string result"
            raise TypeError(msg)
        return result

    return translate


def _make_deepl(source: str, target: str) -> Translator:
    import deepl

    api_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not api_key:
        msg = "DEEPL_API_KEY is not set"
        raise RuntimeError(msg)
    mapped_source = _map_language(source, DEEPL_LANGUAGE_CODES)
    mapped_target = _map_language(target, DEEPL_LANGUAGE_CODES)

    def translate(text: str) -> str:
        client = deepl.Translator(api_key)
        result = client.translate_text(
            text,
            source_lang=None if mapped_source == "auto" else mapped_source,
            target_lang=mapped_target,
        )
        translated = getattr(result, "text", result)
        if not isinstance(translated, str):
            msg = "deepl returned a non-string result"
            raise TypeError(msg)
        return translated

    return translate


def _make_translate(source: str, target: str) -> Translator:
    from translate import Translator as TranslateClient

    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        client = TranslateClient(
            from_lang=mapped_source,
            to_lang=mapped_target,
        )
        result = client.translate(text)
        if not isinstance(result, str):
            msg = "translate returned a non-string result"
            raise TypeError(msg)
        return result

    return translate


def _make_translators_bing(source: str, target: str) -> Translator:
    import translators

    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        result = translators.translate_text(
            query_text=text,
            translator="bing",
            from_language=mapped_source,
            to_language=mapped_target,
        )
        if not isinstance(result, str):
            msg = "translators returned a non-string result"
            raise TypeError(msg)
        return result

    return translate


def _make_googletrans(source: str, target: str) -> Translator:
    from googletrans import Translator as GoogleTransClient

    mapped_source = _map_language(source, GOOGLE_LANGUAGE_CODES)
    mapped_target = _map_language(target, GOOGLE_LANGUAGE_CODES)

    def translate(text: str) -> str:
        with _TRANSLATION_LOCK:
            client = GoogleTransClient()
            result = client.translate(
                text,
                src=mapped_source,
                dest=mapped_target,
            )
        translated = getattr(result, "text", result)
        if not isinstance(translated, str):
            msg = "googletrans returned a non-string result"
            raise TypeError(msg)
        return translated

    return translate


def _make_pygoogletranslation(source: str, target: str) -> Translator:
    from pygoogletranslation import Translator as PyGoogleClient

    mapped_source = _map_language(source, GOOGLE_LANGUAGE_CODES)
    mapped_target = _map_language(target, GOOGLE_LANGUAGE_CODES)

    def translate(text: str) -> str:
        with _TRANSLATION_LOCK:
            client = PyGoogleClient()
            result = client.translate(
                text,
                src=mapped_source,
                dest=mapped_target,
            )
        translated = getattr(result, "text", result)
        if isinstance(result, dict):
            translated = result.get("translatedText") or result.get("translation") or result.get("text")
        if not isinstance(translated, str):
            msg = "pygoogletranslation returned an unsupported result"
            raise TypeError(msg)
        return translated

    return translate


def _make_boto3(source: str, target: str) -> Translator:
    import boto3

    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        client = boto3.client("translate")
        result = client.translate_text(
            Text=text,
            SourceLanguageCode=mapped_source,
            TargetLanguageCode=mapped_target,
        )
        translated = result.get("TranslatedText")
        if not isinstance(translated, str):
            msg = "boto3 returned no translated text"
            raise TypeError(msg)
        return translated

    return translate


def _make_azure(source: str, target: str) -> Translator:
    import urllib.error
    import urllib.parse
    import urllib.request

    endpoint = os.environ.get(
        "AZURE_TRANSLATOR_ENDPOINT",
        "https://api.cognitive.microsofttranslator.com",
    ).rstrip("/")
    api_key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
    if not api_key:
        msg = "AZURE_TRANSLATOR_KEY is not set"
        raise RuntimeError(msg)
    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        query = urllib.parse.urlencode({
            "api-version": "3.0",
            "from": mapped_source,
            "to": mapped_target,
        })
        body = json.dumps([{"Text": text}]).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint}/translate?{query}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Ocp-Apim-Subscription-Key": api_key,
                "Ocp-Apim-Subscription-Region": region,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            msg = f"Azure request failed: {exc}"
            raise RuntimeError(msg) from exc
        translated = payload[0]["translations"][0]["text"]
        if not isinstance(translated, str):
            msg = "Azure returned no translated text"
            raise TypeError(msg)
        return translated

    return translate


def _make_baidu(source: str, target: str) -> Translator:
    import hashlib
    import random
    import urllib.parse
    import urllib.request

    app_id = os.environ.get("BAIDU_APP_ID", "").strip()
    secret_key = os.environ.get("BAIDU_SECRET_KEY", "").strip()
    if not app_id or not secret_key:
        msg = "BAIDU_APP_ID and BAIDU_SECRET_KEY must both be set"
        raise RuntimeError(msg)
    mapped_source = _map_language(source, LANGUAGE_CODES)
    mapped_target = _map_language(target, LANGUAGE_CODES)

    def translate(text: str) -> str:
        salt = str(random.randint(10000, 99999))
        sign = hashlib.md5(f"{app_id}{text}{salt}{secret_key}".encode("utf-8")).hexdigest()
        payload = urllib.parse.urlencode({
            "q": text,
            "from": mapped_source,
            "to": mapped_target,
            "appid": app_id,
            "salt": salt,
            "sign": sign,
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://fanyi-api.baidu.com/api/trans/vip/translate",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        if "error_code" in result:
            msg = f"Baidu error {result['error_code']}: {result.get('error_msg', 'unknown error')}"
            raise RuntimeError(msg)
        translated = "\n".join(item["dst"] for item in result.get("trans_result", []))
        if not translated:
            msg = "Baidu returned no translated text"
            raise TypeError(msg)
        return translated

    return translate


def _make_alibaba(source: str, target: str) -> Translator:
    msg = "Alibaba backend requires a product-specific API integration and is not configured by this script"
    raise RuntimeError(msg)


def _make_watson(source: str, target: str) -> Translator:
    msg = "Watson backend requires a product-specific API integration and is not configured by this script"
    raise RuntimeError(msg)


BACKEND_FACTORIES: dict[str, Callable[[str, str], Translator]] = {
    "deep_translator": _make_deep_translator,
    "deepl": _make_deepl,
    "translate": _make_translate,
    "translators_bing": _make_translators_bing,
    "googletrans": _make_googletrans,
    "pygoogletranslation": _make_pygoogletranslation,
    "boto3": _make_boto3,
    "azure": _make_azure,
    "baidu": _make_baidu,
    "alibaba": _make_alibaba,
    "watson": _make_watson,
}
FALLBACK_ORDER: tuple[str, ...] = (
    "deepl",
    "deep_translator",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
)


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _select_backend(
    requested: str,
    source: str,
    target: str,
) -> tuple[str, Translator]:
    if requested != "auto":
        if requested not in BACKEND_FACTORIES:
            choices = ", ".join(sorted(BACKEND_FACTORIES))
            msg = f"Unknown backend {requested!r}. Available backends: {choices}"
            raise ValueError(msg)
        return requested, BACKEND_FACTORIES[requested](source, target)
    for backend in FALLBACK_ORDER:
        if (
            backend == "deepl"
            and not os.environ.get(
                "DEEPL_API_KEY",
                "",
            ).strip()
        ):
            continue
        module_name = {
            "deep_translator": "deep_translator",
            "deepl": "deepl",
            "translate": "translate",
            "translators_bing": "translators",
            "googletrans": "googletrans",
            "pygoogletranslation": "pygoogletranslation",
        }[backend]
        if not _module_available(module_name):
            continue
        try:
            translator = BACKEND_FACTORIES[backend](source, target)
        except Exception as exc:
            logger.debug(
                "Backend initialization failed for {}: {}",
                backend,
                exc,
            )
            continue
        return backend, translator
    raise RuntimeError("No usable translation backend was found. Install one of: " + ", ".join(FALLBACK_ORDER))


def _split_chunks(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        msg = "chunk_size must be greater than zero"
        raise ValueError(msg)
    chunks: list[str] = []
    position = 0
    text_length = len(text)
    while position < text_length:
        remaining = text_length - position
        end = min(position + chunk_size, text_length)
        if remaining > chunk_size:
            split_at = text.rfind(None, position, end)
            if split_at < position:
                split_at = end
            else:
                split_at += 1
            if split_at <= position:
                split_at = end
        else:
            split_at = end
        chunk = text[position:split_at].strip()
        if chunk:
            chunks.append(chunk)
        position = split_at
    return chunks


def _load_output(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON in {path}: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{path} must contain a JSON object"
        raise ValueError(msg)
    output: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            output[key] = value
    return output


def _atomic_save(path: Path, translations: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {
        str(index): translations[str(index)]
        for index in sorted(
            (int(key) for key in translations if key.isdigit()),
        )
        if str(index) in translations
    }
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(
            ordered,
            temporary,
            ensure_ascii=False,
            indent=2,
        )
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    temporary_path.replace(path)


def _is_identity_translation(original: str, translated: str) -> bool:
    left = " ".join(original.split()).casefold()
    right = " ".join(translated.split()).casefold()
    if not right:
        return True
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.995


def _translate_with_retry(
    index: int,
    text: str,
    translator: Translator,
    limiter: RequestLimiter,
    source: str,
    target: str,
) -> tuple[int, str]:
    if source.strip().lower() == target.strip().lower():
        return index, text
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            limiter.wait()
            translated = translator(text).strip()
            if _is_identity_translation(text, translated):
                msg = "Translation result appears identical to the source"
                raise RuntimeError(msg)
            logger.debug(
                "Chunk {} translated successfully on attempt {}",
                index,
                attempt,
            )
            return index, translated
        except Exception as exc:
            last_error = exc
            logger.exception(
                "Chunk {} attempt {} failed: {}",
                index,
                attempt,
                exc,
            )
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        last_error = RuntimeError("Unknown translation failure")
    msg = f"Chunk {index} failed after 3 attempts: {last_error}"
    raise RuntimeError(msg) from last_error


def _append_failed(path: Path, index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"{index}\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a text file in independently resumable chunks.",
    )
    parser.add_argument(
        "-i",
        "--input",
        default="input.txt",
        type=Path,
    )
    parser.add_argument(
        "-o",
        "--output",
        default="chunks.json",
        type=Path,
    )
    parser.add_argument(
        "--failed",
        default="failed.txt",
        type=Path,
    )
    parser.add_argument(
        "-s",
        "--source",
        default="en",
    )
    parser.add_argument(
        "-t",
        "--target",
        default="fr",
    )
    parser.add_argument(
        "-b",
        "--backend",
        default="auto",
        choices=sorted((*BACKEND_FACTORIES, "auto")),
    )
    parser.add_argument(
        "-d",
        "--delay",
        default=0.5,
        type=float,
    )
    parser.add_argument(
        "--chunk-size",
        default=2500,
        type=int,
    )
    parser.add_argument(
        "--save-every",
        default=10,
        type=int,
    )
    parser.add_argument(
        "--no-continue",
        action="store_true",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not 1 <= MAX_WORKERS <= 2:
        msg = "--workers must be 1 or 2"
        raise ValueError(msg)
    if args.delay < 0:
        msg = "--delay cannot be negative"
        raise ValueError(msg)
    if args.save_every <= 0:
        msg = "--save-every must be greater than zero"
        raise ValueError(msg)
    if not args.input.exists():
        msg = f"Input file does not exist: {args.input}"
        raise FileNotFoundError(msg)
    text = args.input.read_text(encoding="utf-8")
    chunks = _split_chunks(text, args.chunk_size)
    translations: dict[str, str] = {}
    if not args.no_continue:
        translations = _load_output(args.output)
    pending: list[tuple[int, str]] = [
        (index, chunk) for index, chunk in enumerate(chunks) if str(index) not in translations
    ]
    if not pending:
        _atomic_save(args.output, translations)
        logger.info("All {} chunks are already translated", len(chunks))
        return 0
    backend_name, translator = _select_backend(
        args.backend,
        args.source,
        args.target,
    )
    logger.info(
        "Using backend={} workers={} pending={} total={}",
        backend_name,
        MAX_WORKERS,
        len(pending),
        len(chunks),
    )
    limiter = RequestLimiter(args.delay)
    completed_since_save = 0
    executor = ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="translator",
    )
    futures: dict[Future[tuple[int, str]], int] = {}
    try:
        for index, chunk in pending:
            future = executor.submit(
                _translate_with_retry,
                index,
                chunk,
                translator,
                limiter,
                args.source,
                args.target,
            )
            futures[future] = index
        for future in as_completed(futures):
            index = futures[future]
            try:
                completed_index, translated = future.result()
                translations[str(completed_index)] = translated
                completed_since_save += 1
                if completed_since_save >= args.save_every:
                    _atomic_save(args.output, translations)
                    completed_since_save = 0
                    logger.info(
                        "Saved progress after completing chunk {}",
                        completed_index,
                    )
            except Exception as exc:
                logger.error("Chunk {} permanently failed: {}", index, exc)
                _append_failed(args.failed, index)
                completed_since_save += 1
                if completed_since_save >= args.save_every:
                    _atomic_save(args.output, translations)
                    completed_since_save = 0
    except KeyboardInterrupt:
        logger.error("Interrupted; saving completed translations")
        for future in futures:
            future.cancel()
        _atomic_save(args.output, translations)
        executor.shutdown(wait=False, cancel_futures=True)
        return 130
    finally:
        if completed_since_save:
            _atomic_save(args.output, translations)
        executor.shutdown(wait=True)
    logger.info(
        "Finished: {} translated, {} pending failures",
        len(translations),
        len(chunks) - len(translations),
    )
    return 0


if __name__ == "__main__":
    logger.remove()
    logger.add(
        sys.stderr,
        level="ERROR",
        enqueue=True,
    )
    logger.add(
        "translate_chunks.log",
        level="DEBUG",
        rotation="5 MB",
        retention=3,
        enqueue=True,
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.error("Interrupted before translation started")
        raise SystemExit(130)
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        raise SystemExit(1)
