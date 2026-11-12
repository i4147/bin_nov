#!/data/data/com.termux/files/usr/bin/env python
"""Translate a one-word-per-line file into a JSON map with many backends.
Supports deep_translator, DeepL, LibreTranslate, translate, Bing, googletrans, boto3, Baidu, Alibaba, Watson and Azure, keeps the progress across the runs, retries with backoff, throttles the requests per worker and logs the failures to ``failed.txt`` and a rotating log.
Usage: ctrans [-i words.txt] [-o words.json] [-b BACKEND] [-w N] Options: -i, --input Input file (default: words.txt) -o, --output Output JSON (default: words.json) --failed Failed words file (default: failed.txt) -s, --source Source language (default: fr) -t, --target Target language (default: en) -b, --backend -w, --workers Worker threads (default: 2) -d, --delay Seconds before each request, per worker (default: 0.5) --save-every Save every N words (default: 50) --no-continue Ignore existing output; start fresh Examples: $ ctrans -i words.txt -b deep_translator -w 4 Command-line script: run ``python ctrans.py`` or the ``ctrans`` command.
The module is importable as well."""

import argparse
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib
import inspect
import json
import os
import platform
import shutil
import signal
import sys
import tempfile
import threading
from typing import Any, Callable, Iterator, Optional, TextIO

try:
    from loguru import logger
except ImportError:
    sys.exit("loguru is required:  pip install loguru")
MAX_ATTEMPTS: int = 3
BACKOFF_BASE: float = 1.0
LOG_FILE: str = "translate_words.log"
_CONSOLE_LOCK = threading.Lock()
Translator = Callable[[str], str]


class BackendError(Exception):
    pass


class ConfigError(BackendError):
    pass


class UserError(Exception):
    pass


_LANG_MAP: dict[str, dict[str, str]] = {
    "deepl:source": {
        c: c.upper()
        for c in [
            "bg",
            "cs",
            "da",
            "de",
            "el",
            "en",
            "es",
            "et",
            "fi",
            "fr",
            "hu",
            "id",
            "it",
            "ja",
            "ko",
            "lt",
            "lv",
            "nb",
            "nl",
            "pl",
            "pt",
            "ro",
            "ru",
            "sk",
            "sl",
            "sv",
            "tr",
            "uk",
            "zh",
        ]
    },
    "deepl:target": dict(
        {
            c: c.upper()
            for c in [
                "bg",
                "cs",
                "da",
                "de",
                "el",
                "es",
                "et",
                "fi",
                "fr",
                "hu",
                "id",
                "it",
                "ja",
                "ko",
                "lt",
                "lv",
                "nb",
                "nl",
                "pl",
                "ro",
                "ru",
                "sk",
                "sl",
                "sv",
                "tr",
                "uk",
                "zh",
            ]
        },
        en="EN-US",
        pt="PT-PT",
    ),
    "deep_translator": {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-tw": "zh-TW"},
    "googletrans": {"zh": "zh-cn"},
    "pygoogletranslation": {"zh": "zh-cn"},
    "translators_bing": {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-tw": "zh-Hant"},
    "boto3": {"zh": "zh", "zh-cn": "zh", "zh-tw": "zh-TW"},
    "baidu": {
        "fr": "fra",
        "es": "spa",
        "ja": "jp",
        "ko": "kor",
        "ar": "ara",
        "vi": "vie",
        "sv": "swe",
        "da": "dan",
        "fi": "fin",
        "ro": "rom",
        "bg": "bul",
        "et": "est",
        "sl": "slo",
        "zh-tw": "cht",
    },
    "alibaba": {"zh-cn": "zh", "zh-tw": "zh-tw"},
    "watson": {"zh-cn": "zh", "zh-tw": "zh-TW"},
    "azure": {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-tw": "zh-Hant"},
}


def _map_lang(key: str, code: str) -> str:
    return _LANG_MAP.get(key, {}).get(code.lower(), code)


def _import(module: str, hint: str):
    try:
        return importlib.import_module(module)
    except Exception as exc:
        raise BackendError("cannot import '%s' (%s). Install with: %s" % (module, exc, hint))


def _require_env(name: str, backend: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError("backend '%s' needs the %s environment variable" % (backend, name))
    return value


def _as_text(value: str) -> str:
    return "" if value is None else str(value)


async def _await(awaitable: Any):
    return await awaitable


def _make_deepl(source: str, target: str, script_path: str) -> Translator:
    key = _require_env("DEEPL_API_KEY", "deepl")
    deepl = _import("deepl", "pip install deepl")
    src, tgt = _map_lang("deepl:source", source), _map_lang("deepl:target", target)

    def call(text: str) -> str:
        client = deepl.Translator(key)
        return _as_text(client.translate_text(text, source_lang=src, target_lang=tgt).text)

    return call


def _make_deep_translator(source: str, target: str, script_path: str) -> Translator:
    mod = _import("deep_translator", "pip install deep_translator")
    service = os.environ.get("DEEP_TRANSLATOR_SERVICE", "google").lower()
    cls_name = {"google": "GoogleTranslator", "mymemory": "MyMemoryTranslator"}.get(service)
    if cls_name is None or not hasattr(mod, cls_name):
        msg = "DEEP_TRANSLATOR_SERVICE must be 'google' or 'mymemory'"
        raise BackendError(msg)
    cls = getattr(mod, cls_name)
    src, tgt = _map_lang("deep_translator", source), _map_lang("deep_translator", target)

    def call(text: str) -> str:
        return _as_text(cls(source=src, target=tgt).translate(text))

    return call


def _make_libretranslate_remote(source: str, target: str, script_path: str) -> Translator:
    url = _require_env("LIBRETRANSLATE_URL", "libretranslate_remote")
    mod = _import("deep_translator", "pip install deep_translator")
    cls = getattr(mod, "LibreTranslator", None) or getattr(mod, "LibreTranslateTranslator", None)
    if cls is None:
        msg = "this deep_translator has no LibreTranslate class; upgrade it"
        raise BackendError(msg)
    base = url.rstrip("/") + "/"
    api_key = os.environ.get("LIBRETRANSLATE_API_KEY", "")

    def build():
        kwargs = {"source": source, "target": target}
        if api_key:
            kwargs["api_key"] = api_key
        try:
            return cls(base_url=base, **kwargs)
        except TypeError:
            return cls(api_url=base, **kwargs)

    build()

    def call(text: str) -> str:
        return _as_text(build().translate(text))

    return call


def _make_translate(source: str, target: str, script_path: str) -> Translator:
    mod = _import("translate", "pip install translate")
    mod_file = getattr(mod, "__file__", None)
    if mod_file and os.path.realpath(mod_file) == os.path.realpath(script_path):
        msg = "'import translate' resolved to this script itself; rename the script (e.g. translate_words.py) and retry"
        raise BackendError(msg)
    if hasattr(mod, "Translator"):

        def call(text: str) -> str:
            return _as_text(mod.Translator(from_lang=source, to_lang=target).translate(text))

    elif hasattr(mod, "GoogleTranslator"):

        def call(text: str) -> str:
            return _as_text(mod.GoogleTranslator(source=source, target=target).translate(text))

    else:
        msg = "module 'translate' has neither Translator nor GoogleTranslator"
        raise BackendError(msg)
    return call


def _make_translators_bing(source: str, target: str, script_path: str) -> Translator:
    if shutil.which("node") is None:
        msg = "Node.js not found. Install it with: pkg install nodejs"
        raise BackendError(msg)
    ts = _import("translators", "pip install translators")
    src, tgt = _map_lang("translators_bing", source), _map_lang("translators_bing", target)
    lock = threading.Lock()
    logger.warning("translators_bing spawns a JS subprocess per call: expect low throughput")

    def call(text: str) -> str:
        with lock:
            fn = getattr(ts, "translate_text", None)
            if fn is not None:
                return _as_text(fn(text, translator="bing", from_language=src, to_language=tgt))
            return _as_text(ts.bing(text, from_language=src, to_language=tgt))

    return call


def _make_googletrans(source: str, target: str, script_path: str) -> Translator:
    mod = _import("googletrans", 'pip install "googletrans==4.0.0rc1"')
    instance = mod.Translator()
    lock = threading.Lock()
    src, tgt = _map_lang("googletrans", source), _map_lang("googletrans", target)

    def call(text: str) -> str:
        with lock:
            res = instance.translate(text, src=src, dest=tgt)
            if inspect.isawaitable(res):
                res = asyncio.run(_await(res))
            return _as_text(res.text)

    return call


def _make_pygoogletranslation(source: str, target: str, script_path: str) -> Translator:
    mod = _import("pygoogletranslation", "pip install pygoogletranslation")
    instance = mod.Translator()
    lock = threading.Lock()
    src, tgt = _map_lang("pygoogletranslation", source), _map_lang("pygoogletranslation", target)

    def call(text: str) -> str:
        with lock:
            res = instance.translate(text, src=src, dest=tgt)
            return _as_text(getattr(res, "text", res))

    return call


def _make_boto3(source: str, target: str, script_path: str) -> Translator:
    boto3 = _import("boto3", "pip install boto3")
    if boto3.Session().get_credentials() is None:
        msg = "backend 'boto3' found no AWS credentials (set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)"
        raise ConfigError(msg)
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    src, tgt = _map_lang("boto3", source), _map_lang("boto3", target)

    def call(text: str) -> str:
        client = boto3.client("translate", region_name=region)
        return _as_text(
            client.translate_text(Text=text, SourceLanguageCode=src, TargetLanguageCode=tgt)["TranslatedText"]
        )

    return call


def _make_baidu(source: str, target: str, script_path: str) -> Translator:
    app_id = _require_env("BAIDU_APP_ID", "baidu")
    api_key = _require_env("BAIDU_API_KEY", "baidu")
    secret = _require_env("BAIDU_SECRET_KEY", "baidu")
    aip = _import("aip", "pip install baidu-aip")
    src, tgt = _map_lang("baidu", source), _map_lang("baidu", target)

    def call(text: str) -> str:
        client = aip.AipMachineTranslation(app_id, api_key, secret)
        res = client.translate(text, src, tgt)
        if "error_code" in res:
            raise RuntimeError("baidu error %s: %s" % (res.get("error_code"), res.get("error_msg")))
        return _as_text(res["result"]["trans_result"][0]["dst"])

    return call


def _make_alibaba(source: str, target: str, script_path: str) -> Translator:
    key_id = _require_env("ALIBABA_ACCESS_KEY_ID", "alibaba")
    key_secret = _require_env("ALIBABA_ACCESS_KEY_SECRET", "alibaba")
    core = _import("aliyunsdkcore.client", "pip install aliyun-python-sdk-alimt")
    req_mod = _import(
        "aliyunsdkalimt.request.v20181012.TranslateGeneralRequest",
        "pip install aliyun-python-sdk-alimt",
    )
    src, tgt = _map_lang("alibaba", source), _map_lang("alibaba", target)

    def call(text: str) -> str:
        client = core.AcsClient(key_id, key_secret, "cn-hangzhou")
        request = req_mod.TranslateGeneralRequest()
        request.set_SourceLanguage(src)
        request.set_TargetLanguage(tgt)
        request.set_SourceText(text)
        request.set_FormatType("text")
        request.set_Scene("general")
        body = json.loads(client.do_action_with_exception(request))
        return _as_text(body["Data"]["Translated"])

    return call


def _make_watson(source: str, target: str, script_path: str) -> Translator:
    api_key = _require_env("WATSON_API_KEY", "watson")
    url = _require_env("WATSON_URL", "watson")
    wat = _import("ibm_watson", "pip install ibm-watson")
    auth = _import("ibm_cloud_sdk_core.authenticators", "pip install ibm-watson")
    model = "%s-%s" % (_map_lang("watson", source), _map_lang("watson", target))

    def call(text: str) -> str:
        service = wat.LanguageTranslatorV3(version="2018-05-01", authenticator=auth.IAMAuthenticator(api_key))
        service.set_service_url(url)
        res = service.translate(text=text, model_id=model).get_result()
        return _as_text(res["translations"][0]["translation"])

    return call


def _make_azure(source: str, target: str, script_path: str) -> Translator:
    key = _require_env("AZURE_TRANSLATOR_KEY", "azure")
    region = _require_env("AZURE_TRANSLATOR_REGION", "azure")
    endpoint = os.environ.get("AZURE_TRANSLATOR_ENDPOINT", "")
    text_mod = _import("azure.ai.translation.text", "pip install azure-ai-translation-text")
    cred_mod = _import("azure.core.credentials", "pip install azure-ai-translation-text")
    src, tgt = _map_lang("azure", source), _map_lang("azure", target)

    def call(text: str) -> str:
        kwargs = {"credential": cred_mod.AzureKeyCredential(key), "region": region}
        if endpoint:
            kwargs["endpoint"] = endpoint
        client = text_mod.TextTranslationClient(**kwargs)
        try:
            res = client.translate(body=[text], to=[tgt], from_parameter=src)
        except TypeError:
            res = client.translate(body=[text], to_language=[tgt], from_language=src)
        return _as_text(res[0].translations[0].text)

    return call


_BACKEND_FACTORIES: dict[str, Callable[[str, str, str], Translator]] = {
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
_DEFAULT_CHAIN: list[str] = [
    "deepl",
    "deep_translator",
    "libretranslate_remote",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
]
_FORBIDDEN: dict[str, str] = {
    "argostranslate": "depends on ctranslate2 (no 32-bit ARM wheel)",
    "libretranslate": "self-hosting needs ctranslate2 + sentencepiece",
    "opus_mt": "depends on torch",
    "nllb": "depends on torch",
    "m2m100": "depends on torch",
    "transformers": "depends on torch",
    "torch": "no linux_armv8l wheel",
    "sentencepiece": "no 32-bit ARM wheel",
    "ctranslate2": "no 32-bit ARM wheel",
    "google_cloud_translate": "depends on grpcio/protobuf native extensions",
    "google_cloud": "depends on grpcio/protobuf native extensions",
    "yandex_cloud": "depends on grpcio/protobuf native extensions",
    "pydantic_core": "Rust extension, cannot be built on Termux Android 7",
    "openai": "pulls pydantic>=2 -> pydantic-core (Rust)",
    "anthropic": "pulls pydantic>=2 -> pydantic-core (Rust)",
    "mistralai": "pulls pydantic>=2 -> pydantic-core (Rust)",
}


def _validate_backend_name(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    if key in _FORBIDDEN:
        raise UserError(
            "Backend '%s' is not available on Termux Android 7 / armv8l: %s.\n"
            "Offline alternative: run a LibreTranslate server on another machine "
            "on your LAN, then:\n"
            "  export LIBRETRANSLATE_URL=http://192.168.1.50:5000\n"
            "  python translate_words.py -b libretranslate_remote" % (name, _FORBIDDEN[key])
        )
    if key not in _BACKEND_FACTORIES:
        raise UserError("Unknown backend '%s'. Choose one of: %s" % (name, ", ".join(sorted(_BACKEND_FACTORIES))))
    return key


def _build_chain(preferred: Optional[str]) -> list[str]:
    chain: list[str] = [preferred] if preferred else []
    for name in _DEFAULT_CHAIN:
        if name in chain:
            continue
        if name == "deepl" and not os.environ.get("DEEPL_API_KEY"):
            continue
        if name == "libretranslate_remote" and not os.environ.get("LIBRETRANSLATE_URL"):
            continue
        chain.append(name)
    return chain


def build_translator(preferred: Optional[str], source: str, target: str, script_path: str) -> tuple[str, Translator]:
    failed: list[tuple[str, str]] = []
    for name in _build_chain(preferred):
        try:
            fn = _BACKEND_FACTORIES[name](source, target, script_path)
        except ConfigError as exc:
            if name == preferred:
                raise UserError(str(exc))
            logger.warning("backend {!r} skipped: {}", name, exc)
            failed.append((name, str(exc)))
        except BackendError as exc:
            logger.warning("backend {!r} unavailable: {}", name, exc)
            failed.append((name, str(exc)))
        except Exception as exc:
            logger.warning("backend {!r} crashed during init: {!r}", name, exc)
            failed.append((name, repr(exc)))
        else:
            if failed:
                logger.warning("falling back from {!r} to {!r}", failed[0][0], name)
                with _CONSOLE_LOCK:
                    print("\u26a0\ufe0f  Backend '%s' unavailable \u2014 falling back to '%s'." % (failed[0][0], name))
                    print("    reason: %s" % failed[0][1])
            return name, fn
    detail = "\n".join("  - %s: %s" % item for item in failed)
    raise UserError(
        "No translation backend could be initialised.\n%s\nInstall one, e.g.: pip install deep_translator" % detail
    )


def _stderr_sink(message: str) -> None:
    with _CONSOLE_LOCK:
        sys.stderr.write(str(message))
        sys.stderr.flush()


def setup_logging() -> None:
    logger.remove()
    logger.add(
        LOG_FILE,
        level="DEBUG",
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {thread.name} | {message}",
    )
    logger.add(_stderr_sink, level="ERROR", format="{level}: {message}\n")


def iter_words(path: str) -> Iterator[str]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            word = line.strip()
            if word:
                yield word


def load_results(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise UserError(
            "Cannot read existing output '%s' (%s). Fix or delete it, or rerun with --no-continue." % (path, exc)
        )
    if not isinstance(data, dict):
        raise UserError("Output '%s' is not a JSON object; use --no-continue." % path)
    return data


def atomic_save(path: str, data: dict[str, str], lock: threading.Lock) -> bool:
    directory = os.path.dirname(os.path.abspath(path))
    with lock:
        fd, tmp = tempfile.mkstemp(prefix=".tw_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return True
        except OSError as exc:
            logger.error("could not save {!r}: {}", path, exc)
            return False
        finally:
            if os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


class Shared:
    def __init__(
        self,
        fn: Translator,
        results: dict[str, str],
        total: int,
        args: argparse.Namespace,
    ) -> None:
        self.fn = fn
        self.results = results
        self.total = total
        self.args = args
        self.stop = threading.Event()
        self.counter = 0
        self.counter_lock = threading.Lock()
        self.failed_lock = threading.Lock()
        self.file_lock = threading.Lock()


def translate_with_retries(sh: Shared, word: str) -> tuple[Optional[str], object, bool]:
    last_raw: object = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if sh.stop.wait(sh.args.delay):
            return None, last_raw, True
        try:
            raw = sh.fn(word)
        except Exception as exc:
            logger.warning("attempt {}/{} for {!r} raised {!r}", attempt, MAX_ATTEMPTS, word, exc)
        else:
            last_raw = raw
            text = raw.strip() if isinstance(raw, str) else ""
            if text and text.casefold() != word.casefold():
                return text, raw, False
            logger.warning(
                "attempt {}/{} for {!r}: empty or identity result {!r}",
                attempt,
                MAX_ATTEMPTS,
                word,
                raw,
            )
        if attempt < MAX_ATTEMPTS and sh.stop.wait(BACKOFF_BASE * 2 ** (attempt - 1)):
            return None, last_raw, True
    return None, last_raw, False


def worker(sh: Shared, word: str) -> tuple[str, Optional[str]]:
    try:
        if word in sh.results:
            translation, raw, aborted = sh.results[word], sh.results[word], False
        else:
            translation, raw, aborted = translate_with_retries(sh, word)
        if aborted:
            return word, None
        with sh.counter_lock:
            sh.counter += 1
            n = sh.counter
        shown = raw if isinstance(raw, str) else ("<no result>" if raw is None else repr(raw))
        if translation is not None:
            with _CONSOLE_LOCK:
                print("  %s => %s" % (word, shown))
                print("[%d/%d] \u2713 %s -> %s" % (n, sh.total, word, translation))
        else:
            logger.error("GIVE UP on {!r} after {} attempts", word, MAX_ATTEMPTS)
            with sh.failed_lock:
                try:
                    with open(sh.args.failed, "a", encoding="utf-8") as handle:
                        handle.write(word + "\n")
                except OSError as exc:
                    logger.error("could not append {!r} to {!r}: {}", word, sh.args.failed, exc)
            with _CONSOLE_LOCK:
                print("  %s => %s" % (word, shown))
                print("[%d/%d] \u2717 %s (failed -> %s)" % (n, sh.total, word, sh.args.failed))
        return word, translation
    except Exception as exc:
        logger.error("unexpected worker error for {!r}: {!r}", word, exc)
        return word, None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Translate a one-word-per-line file into a JSON map (Termux-friendly).")
    p.add_argument("-i", "--input", default="words.txt", help="input file (default: words.txt)")
    p.add_argument("-o", "--output", default="words.json", help="output JSON (default: words.json)")
    p.add_argument("--failed", default="failed.txt", help="failed words file (default: failed.txt)")
    p.add_argument("-s", "--source", default="fr", help="source language (default: fr)")
    p.add_argument("-t", "--target", default="en", help="target language (default: en)")
    p.add_argument(
        "-b",
        "--backend",
        default=None,
        help="preferred backend (default: deep_translator, after deepl if "
        "DEEPL_API_KEY is set). Choices: %s" % ", ".join(sorted(_BACKEND_FACTORIES)),
    )
    p.add_argument("-w", "--workers", type=int, default=2, help="worker threads (default: 2)")
    p.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.5,
        help="seconds before each request, per worker (default: 0.5)",
    )
    p.add_argument("--save-every", type=int, default=50, help="save every N words (default: 50)")
    p.add_argument("--no-continue", action="store_true", help="ignore existing output; start fresh")
    return p.parse_args(argv)


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def print_next_steps(failed_count: int, args: argparse.Namespace) -> None:
    with _CONSOLE_LOCK:
        print("\nNext steps:")
        if failed_count:
            print("  * %d word(s) are in %s. Retry them with another backend, e.g.:" % (failed_count, args.failed))
            print("      python translate_words.py -i %s -o %s -b translate" % (args.failed, args.output))
            print("    (the output file is resumed, so only missing words are redone)")
        print("  * Offline setup: run LibreTranslate on a LAN machine, then")
        print("      export LIBRETRANSLATE_URL=http://192.168.1.50:5000")
        print("      python translate_words.py -b libretranslate_remote")


def run(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        raise UserError("Input file '%s' not found. Create it with one word per line or pass -i." % args.input)
    if args.workers < 1 or args.save_every < 1 or args.delay < 0:
        msg = "--workers and --save-every must be >= 1 and --delay >= 0."
        raise UserError(msg)
    preferred = _validate_backend_name(args.backend) if args.backend else None
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging()
    logger.debug(
        "python {} on {} ({})",
        platform.python_version(),
        platform.machine(),
        platform.system(),
    )
    script_path = os.path.abspath(__file__)
    name, fn = build_translator(preferred, args.source, args.target, script_path)
    print(
        "Backend: %s  (%s -> %s, %d worker(s), delay %.2fs)"
        % (name, args.source, args.target, args.workers, args.delay)
    )
    results: dict[str, str] = {} if args.no_continue else load_results(args.output)
    if results:
        print("Loaded %d existing translation(s) from %s" % (len(results), args.output))
    try:
        open(args.failed, "w", encoding="utf-8").close()
    except OSError as exc:
        raise UserError("Cannot write failed-words file '%s': %s" % (args.failed, exc))
    pending = [w for w in iter_words(args.input) if w not in results]
    total = len(pending)
    if total == 0:
        print("Nothing to do: every word is already translated.")
        return 0
    print("%d word(s) to translate." % total)
    sh = Shared(fn, results, total, args)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _raise_interrupt)
    executor = ThreadPoolExecutor(max_workers=args.workers)
    window: deque = deque()
    max_inflight = args.workers * 4
    next_i = since_save = failed_count = 0
    interrupted = False
    try:
        while next_i < total or window:
            while next_i < total and len(window) < max_inflight:
                window.append(executor.submit(worker, sh, pending[next_i]))
                next_i += 1
            word, translation = window.popleft().result()
            since_save += 1
            if translation is None:
                failed_count += 1
            else:
                results[word] = translation
            if since_save >= args.save_every:
                atomic_save(args.output, results, sh.file_lock)
                since_save = 0
    except KeyboardInterrupt:
        interrupted = True
        sh.stop.set()
        for fut in window:
            fut.cancel()
    finally:
        executor.shutdown(wait=not interrupted)
        saved = atomic_save(args.output, results, sh.file_lock)
    done = len(results)
    with _CONSOLE_LOCK:
        if interrupted:
            print(
                "\nInterrupted. %d translation(s) %s %s (rerun to resume)."
                % (done, "saved to" if saved else "NOT saved to", args.output)
            )
        else:
            print("\nDone: %d translated, %d failed. Output: %s" % (done, failed_count, args.output))
    print_next_steps(failed_count, args)
    return 130 if interrupted else 0


def main() -> int:
    try:
        return run(parse_args())
    except UserError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
