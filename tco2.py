#!/data/data/com.termux/files/usr/bin/env python
"""Translate a one-word-per-line file into a JSON mapping."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import importlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
from typing import Callable

from dh import MAX_WORKERS
from loguru import logger


try:
    from loguru import logger
except ImportError as exc:
    msg = "Missing dependency: install it with 'pip install loguru'"
    raise SystemExit(msg) from exc


class BackendError(Exception):
    pass


TranslatorCallable = Callable[[str], str]
Factory = Callable[[str, str, str], TranslatorCallable]
_LANG_MAP: dict[str, dict[str, str]] = {
    "deepl": {
        "fr": "FR",
        "en": "EN-US",
        "de": "DE",
        "es": "ES",
        "it": "IT",
        "pt": "PT-PT",
        "nl": "NL",
        "pl": "PL",
        "ru": "RU",
        "ja": "JA",
        "zh": "ZH",
    },
    "deep_translator": {
        "fr": "fr",
        "en": "en",
        "de": "de",
        "es": "es",
        "it": "it",
        "pt": "pt",
        "nl": "nl",
        "pl": "pl",
        "ru": "ru",
        "ja": "ja",
        "zh": "zh-CN",
    },
    "libretranslate_remote": {
        "fr": "fr",
        "en": "en",
        "de": "de",
        "es": "es",
        "it": "it",
        "pt": "pt",
        "nl": "nl",
        "pl": "pl",
        "ru": "ru",
        "ja": "ja",
        "zh": "zh",
    },
    "boto3": {
        "fr": "fr",
        "en": "en",
        "de": "de",
        "es": "es",
        "it": "it",
        "pt": "pt",
        "zh": "zh",
    },
    "baidu": {
        "fr": "fra",
        "en": "en",
        "de": "de",
        "es": "spa",
        "it": "it",
        "pt": "pt",
        "zh": "zh",
    },
    "azure": {
        "fr": "fr",
        "en": "en",
        "de": "de",
        "es": "es",
        "it": "it",
        "pt": "pt",
        "zh": "zh-Hans",
    },
}
_SERIAL_LOCKS: dict[str, threading.Lock] = {
    "googletrans": threading.Lock(),
    "pygoogletranslation": threading.Lock(),
    "translators_bing": threading.Lock(),
}
_CONSOLE_LOCK = threading.Lock()
_RESULTS_LOCK = threading.Lock()
_FAILED_LOCK = threading.Lock()
_PROGRESS_LOCK = threading.Lock()


def _configure_logging() -> None:
    logger.remove()
    logger.add(
        "translate_words.log",
        level="DEBUG",
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {thread.name} | {message}",
    )
    logger.add(
        sys.stderr,
        level="ERROR",
        format="<red>{level}</red>: {message}",
    )


def _print(message: str) -> None:
    with _CONSOLE_LOCK:
        print(message, flush=True)


def _language(backend: str, code: str) -> str:
    mapped = _LANG_MAP.get(backend, {}).get(code.casefold())
    if mapped is None:
        logger.warning(
            "No language mapping for backend '{}' and code '{}'; using raw code",
            backend,
            code,
        )
        return code
    return mapped


def _require_python_and_platform() -> None:
    if sys.version_info[:2] != (3, 12):
        msg = f"Python 3.12 is required; detected {platform.python_version()}"
        raise SystemExit(msg)
    machine = platform.machine().casefold()
    if machine not in {"armv8l", "armv7l", "armv7"}:
        msg = f"This script targets 32-bit ARM Termux; detected architecture '{machine}'"
        raise SystemExit(msg)
    if sys.maxsize > 2**32:
        msg = "This script requires a 32-bit Python process"
        raise SystemExit(msg)


def _import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:
        msg = f"cannot import {name}: {exc}"
        raise BackendError(msg) from exc


def _make_deepl(source: str, target: str, script_path: str) -> TranslatorCallable:
    key = os.getenv("DEEPL_API_KEY")
    if not key:
        msg = "DEEPL_API_KEY is not set"
        raise BackendError(msg)
    _import("deepl")
    source_code = _language("deepl", source)
    target_code = _language("deepl", target)

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("deepl")
            client = module.DeepLClient(key)
            return str(
                client.translate_text(
                    text,
                    source_lang=source_code,
                    target_lang=target_code,
                )
            )
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_deep_translator(source: str, target: str, script_path: str) -> TranslatorCallable:
    _import("deep_translator")
    source_code = _language("deep_translator", source)
    target_code = _language("deep_translator", target)

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("deep_translator")
            client = module.GoogleTranslator(
                source=source_code,
                target=target_code,
            )
            return str(client.translate(text))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_libretranslate_remote(source: str, target: str, script_path: str) -> TranslatorCallable:
    url = os.getenv("LIBRETRANSLATE_URL")
    if not url:
        msg = "LIBRETRANSLATE_URL is not set"
        raise BackendError(msg)
    _import("deep_translator")
    source_code = _language("libretranslate_remote", source)
    target_code = _language("libretranslate_remote", target)

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("deep_translator")
            client = module.LibreTranslateTranslator(
                source=source_code,
                target=target_code,
                api_url=url.rstrip("/"),
            )
            return str(client.translate(text))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_translate(source: str, target: str, script_path: str) -> TranslatorCallable:
    if Path(script_path).stem.casefold() == "translate":
        msg = "the local script name resolves to the translate package; rename it"
        raise BackendError(msg)
    _import("translate")

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("translate")
            client = module.Translator(from_lang=source, to_lang=target)
            return str(client.translate(text))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_translators_bing(source: str, target: str, script_path: str) -> TranslatorCallable:
    _import("translators")
    lock = _SERIAL_LOCKS["translators_bing"]
    logger.warning("translators_bing is serialized because each request may start a JS runtime")

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("translators")
            with lock:
                return str(
                    module.translate_text(
                        query=text,
                        translator="bing",
                        from_language=source,
                        to_language=target,
                    )
                )
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_googletrans(source: str, target: str, script_path: str) -> TranslatorCallable:
    module = _import("googletrans")
    client = module.Translator()
    lock = _SERIAL_LOCKS["googletrans"]

    def translate(text: str) -> str:
        try:
            with lock:
                result = client.translate(text, src=source, dest=target)
                return str(result.text)
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_pygoogletranslation(source: str, target: str, script_path: str) -> TranslatorCallable:
    module = _import("pygoogletranslation")
    client_class = getattr(module, "Translator", None)
    if client_class is None:
        msg = "pygoogletranslation.Translator is unavailable"
        raise BackendError(msg)
    client = client_class()
    lock = _SERIAL_LOCKS["pygoogletranslation"]

    def translate(text: str) -> str:
        try:
            with lock:
                result = client.translate(text, src=source, dest=target)
                return str(getattr(result, "text", result))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_boto3(source: str, target: str, script_path: str) -> TranslatorCallable:
    _import("boto3")
    source_code = _language("boto3", source)
    target_code = _language("boto3", target)

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("boto3")
            client = module.client("translate")
            response = client.translate_text(
                Text=text,
                SourceLanguageCode=source_code,
                TargetLanguageCode=target_code,
            )
            return str(response["TranslatedText"])
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_baidu(source: str, target: str, script_path: str) -> TranslatorCallable:
    app_id = os.getenv("BAIDU_APP_ID")
    api_key = os.getenv("BAIDU_API_KEY")
    secret_key = os.getenv("BAIDU_SECRET_KEY")
    if not all((app_id, api_key, secret_key)):
        msg = "BAIDU_APP_ID, BAIDU_API_KEY, and BAIDU_SECRET_KEY are required"
        raise BackendError(msg)
    _import("aip")
    source_code = _language("baidu", source)
    target_code = _language("baidu", target)

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("aip")
            client = module.AipNlp(app_id, api_key, secret_key)
            response = client.translate(text, fromLang=source_code, toLang=target_code)
            if "trans_result" not in response:
                raise BackendError(str(response))
            return str(response["trans_result"]["dst"])
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_alibaba(source: str, target: str, script_path: str) -> TranslatorCallable:
    access_key = os.getenv("ALIBABA_ACCESS_KEY_ID")
    secret = os.getenv("ALIBABA_ACCESS_KEY_SECRET")
    region = os.getenv("ALIBABA_REGION", "cn-hangzhou")
    if not access_key or not secret:
        msg = "ALIBABA_ACCESS_KEY_ID and ALIBABA_ACCESS_KEY_SECRET are required"
        raise BackendError(msg)
    _import("aliyunsdkcore")
    _import("aliyunsdkalimt")
    source_code = _language("alibaba", source)
    target_code = _language("alibaba", target)

    def translate(text: str) -> str:
        try:
            core = importlib.import_module("aliyunsdkcore.client")
            request_module = importlib.import_module("aliyunsdkalimt.request.v20181012")
            client = core.AcsClient(access_key, secret, region)
            request = request_module.TranslateGeneralRequest()
            request.set_SourceLanguage(source_code)
            request.set_TargetLanguage(target_code)
            request.set_SourceText(text)
            response = client.do_action_with_exception(request)
            payload = json.loads(response)
            return str(payload["Data"]["Translated"])
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_watson(source: str, target: str, script_path: str) -> TranslatorCallable:
    api_key = os.getenv("WATSON_API_KEY")
    url = os.getenv("WATSON_URL")
    if not api_key or not url:
        msg = "WATSON_API_KEY and WATSON_URL are required"
        raise BackendError(msg)
    _import("ibm_watson")

    def translate(text: str) -> str:
        try:
            module = importlib.import_module("ibm_watson")
            authenticator_module = importlib.import_module("ibm_cloud_sdk_core.authenticators")
            authenticator = authenticator_module.IamAuthenticator(api_key)
            client = module.LanguageTranslatorV3(
                version="2018-05-01",
                authenticator=authenticator,
            )
            client.set_service_url(url)
            response = client.translate(
                text=[text],
                source=source,
                target=target,
            ).get_result()
            return str(response["translations"][0]["translation"])
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


def _make_azure(source: str, target: str, script_path: str) -> TranslatorCallable:
    key = os.getenv("AZURE_TRANSLATOR_KEY")
    region = os.getenv("AZURE_TRANSLATOR_REGION")
    endpoint = os.getenv(
        "AZURE_TRANSLATOR_ENDPOINT",
        "https://api.cognitive.microsofttranslator.com",
    )
    if not key or not region:
        msg = "AZURE_TRANSLATOR_KEY and AZURE_TRANSLATOR_REGION are required"
        raise BackendError(msg)
    _import("requests")
    source_code = _language("azure", source)
    target_code = _language("azure", target)

    def translate(text: str) -> str:
        try:
            requests = importlib.import_module("requests")
            response = requests.post(
                f"{endpoint.rstrip('/')}/translate",
                params={"api-version": "3.0", "from": source_code, "to": target_code},
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Ocp-Apim-Subscription-Region": region,
                    "Content-Type": "application/json",
                },
                json=[{"Text": text}],
                timeout=30,
            )
            response.raise_for_status()
            return str(response.json()[0]["translations"][0]["text"])
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    return translate


_BACKEND_FACTORIES: dict[str, Factory] = {
    "deepl": _make_deepl,
    "deep_translator": _make_deep_translator,
    "libretranslate_remote": _make_libretranslate_remote,
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


def _fallback_order() -> list[str]:
    order: list[str] = []
    if os.getenv("DEEPL_API_KEY"):
        order.append("deepl")
    order.append("deep_translator")
    if os.getenv("LIBRETRANSLATE_URL"):
        order.append("libretranslate_remote")
    order.extend([
        "translate",
        "translators_bing",
        "googletrans",
        "pygoogletranslation",
    ])
    return order


def _select_backend(
    preferred: str | None,
    source: str,
    target: str,
    script_path: str,
) -> tuple[str, TranslatorCallable]:
    candidates = [preferred] if preferred else _fallback_order()
    failures: list[str] = []
    for name in candidates:
        if name not in _BACKEND_FACTORIES:
            raise SystemExit(f"Unknown backend '{name}'. Choose from: " + ", ".join(sorted(_BACKEND_FACTORIES)))
        try:
            translator = _BACKEND_FACTORIES[name](source, target, script_path)
            if failures:
                _print(f"⚠️  Backend '{failures[-1]}' unavailable — falling back to '{name}'.")
            logger.debug("Selected backend '{}'", name)
            return name, translator
        except Exception as exc:
            failures.append(name)
            logger.warning("Backend '{}' unavailable: {}", name, exc)
            if preferred:
                msg = (
                    f"Backend '{name}' is unavailable: {exc}. "
                    "Install its package and configure its environment variables."
                )
                raise SystemExit(msg) from exc
    msg = "No usable backend was found. Install deep_translator or configure one of the supported backends."
    raise SystemExit(msg)


def _read_words(path: Path) -> list[str]:
    if not path.is_file():
        msg = f"Input file does not exist: {path}"
        raise SystemExit(msg)
    words: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                word = line.strip()
                if word:
                    words.append(word)
    except OSError as exc:
        msg = f"Cannot read input file '{path}': {exc}"
        raise SystemExit(msg) from exc
    return words


def _load_results(path: Path, continue_run: bool) -> dict[str, str]:
    if not continue_run or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            msg = "JSON root must be an object"
            raise ValueError(msg)
        return {str(key): str(value) for key, value in data.items()}
    except Exception as exc:
        msg = f"Cannot load existing output '{path}': {exc}"
        raise SystemExit(msg) from exc


def _atomic_save(path: Path, results: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _append_failed(path: Path, word: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FAILED_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(f"{word}\n")


def _translate_one(
    word: str,
    translator: TranslatorCallable,
    delay: float,
    failed_path: Path,
) -> tuple[str, str | None]:
    for attempt in range(1, 4):
        try:
            time.sleep(delay)
            translated = str(translator(word)).strip()
            if not translated or translated.casefold() == word.casefold():
                msg = "empty or identity translation"
                raise BackendError(msg)
            return word, translated
        except Exception as exc:
            logger.warning(
                "Attempt {}/3 failed for word {!r}: {}",
                attempt,
                word,
                exc,
            )
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    _append_failed(failed_path, word)
    logger.error("Giving up on word {!r}; appended to {}", word, failed_path)
    return word, None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate a one-word-per-line file into a JSON mapping.")
    parser.add_argument("-i", "--input", default="words.txt")
    parser.add_argument("-o", "--output", default="words.json")
    parser.add_argument("--failed", default="failed.txt")
    parser.add_argument("-s", "--source", default="fr")
    parser.add_argument("-t", "--target", default="en")
    parser.add_argument("-b", "--backend")
    parser.add_argument("-d", "--delay", type=float, default=0.5)
    parser.add_argument("--save-every", type=int, default=50)
    parser.add_argument("--no-continue", action="store_true")
    args = parser.parse_args()
    if MAX_WORKERS < 1:
        parser.error("--workers must be at least 1")
    if args.save_every < 1:
        parser.error("--save-every must be at least 1")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    return args


def _run(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    failed_path = Path(args.failed)
    words = _read_words(input_path)
    results = _load_results(output_path, not args.no_continue)
    if not args.no_continue:
        _print(f"Loaded {len(results)} existing translations from {output_path}")
    pending = [word for word in words if word not in results]
    total = len(words)
    completed = total - len(pending)
    _print(f"Processing {len(pending)} pending words out of {total}")
    if not pending:
        _atomic_save(output_path, results)
        return 0
    backend_name, translator = _select_backend(
        args.backend,
        args.source,
        args.target,
        str(Path(__file__).resolve()),
    )
    _print(f"Using backend: {backend_name}")
    executor = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 2))
    futures: dict[Future[tuple[str, str | None]], str] = {}
    try:
        for word in pending:
            future = executor.submit(
                _translate_one,
                word,
                translator,
                args.delay,
                failed_path,
            )
            futures[future] = word
        for future in as_completed(futures):
            word, translated = future.result()
            completed += 1
            if translated is not None:
                with _RESULTS_LOCK:
                    results[word] = translated
                _print(f"[{completed}/{total}] ✓ {word} -> {translated}")
            else:
                _print(f"[{completed}/{total}] ✗ {word} (failed → {failed_path})")
            with _PROGRESS_LOCK:
                if completed % args.save_every == 0:
                    with _RESULTS_LOCK:
                        snapshot = dict(results)
                    _atomic_save(output_path, snapshot)
                    _print(f"Saved {len(snapshot)} translations to {output_path}")
        with _RESULTS_LOCK:
            _atomic_save(output_path, dict(results))
        _print(f"Completed. Saved {len(results)} translations to {output_path}")
        return 0
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        with _RESULTS_LOCK:
            _atomic_save(output_path, dict(results))
        _print(f"Interrupted. Current state saved to {output_path}")
        return 130
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def main() -> int:
    _require_python_and_platform()
    _configure_logging()
    args = _parse_args()
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
