#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file in chunks with resume support and detailed logging.
Smart-chunks the text, runs the workers against the first usable backend in the fallback order, retries with backoff, saves the state atomically, handles an interruption gracefully and logs to ``<output>.debug.log``; ``--list-backends`` prints what is available.
Usage: dt5 INPUT [-o OUT.json] [-s LANG] [-t LANG] [--backends LIST] Positional arguments: input Input text file Options: -o, --output Output JSON file (default: <input>.translations.json) -s, --source Source language code (ISO 639-1) -t, --target Target language code (ISO 639-1) --backends --chunk-size Chunk size in characters --save-every Save state every N chunks --debug-log Path to debug log file (default: <output>.debug.log) --quiet Suppress error output to stderr --list-backends List known backends and exit Examples: $ dt5 article.txt -t de --list-backends Command-line script: run ``python dt5.py`` or the ``dt5`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from atomicio import atomic_write_json
from dh import MAX_WORKERS
from loguru import logger


def setup_logging(debug_log: Path, error_stderr: bool = True) -> None:
    logger.remove()
    logger.add(
        str(debug_log),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="50 MB",
        retention=3,
    )
    if error_stderr:
        logger.add(
            sys.stderr,
            level="ERROR",
            format="{time:HH:mm:ss} | {level: <8} | {message}",
        )


LANGUAGE_CODES: dict[str, dict[str, str]] = {
    "deep_translator": {
        "en": "en",
        "es": "es",
        "fr": "fr",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh-CN",
        "ja": "ja",
        "ko": "ko",
        "ar": "ar",
    },
    "deepl": {
        "en": "en",
        "es": "es",
        "fr": "fr",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh",
        "ja": "ja",
        "ko": "ko",
        "ar": "ar",
    },
    "translate": {
        "en": "en",
        "es": "es",
        "fr": "fr",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh",
        "ja": "ja",
        "ko": "ko",
        "ar": "ar",
    },
    "googletrans": {
        "en": "en",
        "es": "es",
        "fr": "fr",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh-CN",
        "ja": "ja",
        "ko": "ko",
        "ar": "ar",
    },
    "translators_bing": {
        "en": "en",
        "es": "es",
        "fr": "fr",
        "de": "de",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "zh": "zh-Hans",
        "ja": "ja",
        "ko": "ko",
        "ar": "ar",
    },
}


def map_language_code(backend: str, code: str) -> str:
    if backend not in LANGUAGE_CODES:
        msg = f"Unknown backend: {backend}"
        raise ValueError(msg)
    return LANGUAGE_CODES[backend].get(code, code)


def _make_deep_translator(source: str, target: str) -> Callable:
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        msg = "deep_translator not installed. Run: pip install deep_translator"
        raise ImportError(msg)
    source = map_language_code("deep_translator", source)
    target = map_language_code("deep_translator", target)

    def translate_chunk(text: str) -> str:
        translator = GoogleTranslator(source_language=source, target_language=target)
        return translator.translate(text)

    return translate_chunk


def _make_deepl(source: str, target: str) -> Callable:
    try:
        import deepl
    except ImportError:
        msg = "deepl not installed. Run: pip install deepl"
        raise ImportError(msg)
    import os

    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key:
        msg = "DEEPL_API_KEY environment variable not set"
        raise ValueError(msg)
    source = map_language_code("deepl", source).upper()
    target = map_language_code("deepl", target).upper()
    translator = deepl.Translator(api_key)

    def translate_chunk(text: str) -> str:
        result = translator.translate_text(text, source_lang=source, target_lang=target)
        return result.text

    return translate_chunk


def _make_translate(source: str, target: str) -> Callable:
    try:
        from translate import Translator
    except ImportError:
        msg = "translate not installed. Run: pip install translate"
        raise ImportError(msg)
    source = map_language_code("translate", source)
    target = map_language_code("translate", target)
    translator = Translator(from_lang=source, to_lang=target)

    def translate_chunk(text: str) -> str:
        return translator.translate(text)

    return translate_chunk


def _make_translators_bing(source: str, target: str) -> Callable:
    try:
        import translators
    except ImportError:
        msg = "translators not installed. Run: pip install translators"
        raise ImportError(msg)
    source = map_language_code("translators_bing", source)
    target = map_language_code("translators_bing", target)

    def translate_chunk(text: str) -> str:
        return translators.translate_text(text, from_language=source, to_language=target, service="bing")

    return translate_chunk


def _make_googletrans(source: str, target: str) -> Callable:
    try:
        from googletrans import Translator
    except ImportError:
        msg = 'googletrans not installed. Run: pip install "googletrans==4.0.0rc1"'
        raise ImportError(msg)
    source = map_language_code("googletrans", source)
    target = map_language_code("googletrans", target)
    lock = threading.Lock()
    translator = Translator()

    def translate_chunk(text: str) -> str:
        with lock:
            result = translator.translate(text, src_language=source, dest_language=target)
            return result["text"]

    return translate_chunk


def _make_pygoogletranslation(source: str, target: str) -> Callable:
    try:
        from pygoogletranslation import Translator
    except ImportError:
        msg = "pygoogletranslation not installed. Run: pip install pygoogletranslation"
        raise ImportError(msg)
    source = map_language_code("googletrans", source)
    target = map_language_code("googletrans", target)
    lock = threading.Lock()
    translator = Translator()

    def translate_chunk(text: str) -> str:
        with lock:
            result = translator.translate(text, src_language=source, dest_language=target)
            return result["text"]

    return translate_chunk


def _make_boto3(source: str, target: str) -> Callable:
    try:
        import boto3
    except ImportError:
        msg = "boto3 not installed. Run: pip install boto3"
        raise ImportError(msg)
    source = map_language_code("deep_translator", source)
    target = map_language_code("deep_translator", target)
    client = boto3.client("translate", region_name="us-east-1")

    def translate_chunk(text: str) -> str:
        response = client.translate_text(
            Text=text,
            SourceLanguageCode=source,
            TargetLanguageCode=target,
        )
        return response["TranslatedText"]

    return translate_chunk


BACKEND_FACTORIES: dict[str, Any] = {
    "deep_translator": _make_deep_translator,
    "deepl": _make_deepl,
    "translate": _make_translate,
    "translators_bing": _make_translators_bing,
    "googletrans": _make_googletrans,
    "pygoogletranslation": _make_pygoogletranslation,
    "boto3": _make_boto3,
}
DEFAULT_BACKEND_ORDER: list[str] = [
    "deepl",
    "deep_translator",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
]


def smart_chunk_text(text: str, chunk_size: int = 2500) -> list[str]:
    if not text or not text.strip():
        return []
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        chunk = text[pos:end]
        if end < len(text) and chunk and chunk[-1] not in (" ", "\n", "\t"):
            last_space = chunk.rfind(" ")
            if last_space > 0:
                chunk = chunk[:last_space]
                end = pos + len(chunk)
            else:
                end = pos + len(chunk)
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        pos = end
    return chunks


def is_identity_translation(original: str, translated: str, threshold: float = 0.98) -> bool:
    if translated is None:
        return True
    a = original.strip()
    b = translated.strip()
    if not b:
        return True
    if a == b:
        return True
    if abs(len(a) - len(b)) > max(4, int(0.05 * len(a))):
        return False
    import difflib

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= threshold


@dataclass
class TranslationState:
    source_file: str
    source_lang: str
    target_lang: str
    chunks: list[str] = field(default_factory=list)
    translations: dict[int, str] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    backend_used: dict[int, str] = field(default_factory=dict)
    completed_at: Optional[float] = None

    def to_json(self) -> dict:
        return {
            "source_file": self.source_file,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "chunks": self.chunks,
            "translations": {str(k): v for k, v in self.translations.items()},
            "failed": {str(k): v for k, v in self.failed.items()},
            "backend_used": {str(k): v for k, v in self.backend_used.items()},
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "TranslationState":
        return cls(
            source_file=data.get("source_file", ""),
            source_lang=data.get("source_lang", ""),
            target_lang=data.get("target_lang", ""),
            chunks=list(data.get("chunks", [])),
            translations={int(k): v for k, v in data.get("translations", {}).items()},
            failed={int(k): v for k, v in data.get("failed", {}).items()},
            backend_used={int(k): v for k, v in data.get("backend_used", {}).items()},
            completed_at=data.get("completed_at"),
        )


def load_state(path: Path) -> Optional[TranslationState]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        state = TranslationState.from_json(data)
        logger.info(f"Resumed state from {path} ({len(state.translations)} chunks done)")
        return state
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning(f"Could not load state from {path}: {e}")
        return None


def save_state(path: Path, state: TranslationState) -> None:
    try:
        atomic_write_json(path, state.to_json())
    except OSError as e:
        logger.error(f"Failed to save state: {e}")


def append_failed_chunk(path: Path, index: int, text: str, error: str) -> None:
    record = {"index": index, "text": text, "error": error, "ts": time.time()}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"Could not append failed chunk: {e}")


def retry_with_backoff(
    fn: Callable[[], str],
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
) -> str:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            logger.debug(f"Attempt {attempt}/{attempts} failed: {e!r}; retrying in {delay:.1f}s")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


class TranslatorEngine:
    def __init__(
        self,
        source_lang: str,
        target_lang: str,
        backend_order: Optional[list[str]] = None,
        attempts_per_backend: int = 3,
    ) -> None:
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.attempts = attempts_per_backend
        self.backends: list[tuple[str, Callable[[str], str]]] = []
        self.lock = threading.Lock()
        order = backend_order or DEFAULT_BACKEND_ORDER
        for name in order:
            factory = BACKEND_FACTORIES.get(name)
            if factory is None:
                logger.warning(f"Unknown backend in order: {name}")
                continue
            try:
                fn = factory(source_lang, target_lang)
                self.backends.append((name, fn))
                logger.info(f"Backend available: {name}")
            except (ImportError, ValueError) as e:
                logger.info(f"Backend {name} unavailable: {e}")
        if not self.backends:
            msg = (
                "No translation backends available. Install one of: "
                "deep_translator, deepl, translate, translators, "
                "googletrans, pygoogletranslation, boto3"
            )
            raise RuntimeError(msg)

    def translate_chunk(self, text: str) -> tuple[str, str]:
        errors: list[str] = []
        for name, fn in self.backends:
            try:
                result = retry_with_backoff(
                    lambda f=fn, t=text: f(t),
                    attempts=self.attempts,
                )
            except Exception as e:
                errors.append(f"{name}: {e!r}")
                logger.debug(f"Backend {name} failed for chunk: {e!r}")
                continue
            if result is None:
                errors.append(f"{name}: returned None")
                continue
            if is_identity_translation(text, result):
                errors.append(f"{name}: identity translation")
                logger.debug(f"Backend {name} returned identity; trying next")
                continue
            return result, name
        raise RuntimeError("All backends failed. " + " | ".join(errors))


class GracefulShutdown:
    def __init__(self) -> None:
        self.event = threading.Event()
        self._prev_int = None
        self._prev_term = None

    def __enter__(self):
        def handler(signum: int, frame: Any) -> None:
            if not self.event.is_set():
                logger.warning(f"Signal {signum} received; finishing current chunks...")
                print(
                    "\n[!] Interrupt received — finishing in-flight work and saving state...",
                    file=sys.stderr,
                )
            self.event.set()

        self._prev_int = signal.signal(signal.SIGINT, handler)
        try:
            self._prev_term = signal.signal(signal.SIGTERM, handler)
        except (ValueError, AttributeError):
            self._prev_term = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._prev_int is not None:
            signal.signal(signal.SIGINT, self._prev_int)
        if self._prev_term is not None:
            signal.signal(signal.SIGTERM, self._prev_term)
        return False


def run_translation(
    input_path: Path,
    output_path: Path,
    source_lang: str,
    target_lang: str,
    backend_order: Optional[list[str]] = None,
    chunk_size: int = 2500,
    max_workers: int = 2,
    save_every: int = 10,
) -> TranslationState:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        msg = f"Input file not found: {input_path}"
        raise FileNotFoundError(msg)
    failed_path = output_path.with_suffix(output_path.suffix + ".failed.jsonl")
    state = load_state(output_path)
    if state is not None and (state.source_lang != source_lang or state.target_lang != target_lang):
        logger.warning(
            "Existing state has different language pair "
            f"({state.source_lang}->{state.target_lang}); "
            f"proceeding with current request ({source_lang}->{target_lang})"
        )
        state.source_lang = source_lang
        state.target_lang = target_lang
    if state is None or not state.chunks:
        logger.info(f"Reading {input_path}")
        text = input_path.read_text(encoding="utf-8", errors="replace")
        chunks = smart_chunk_text(text, chunk_size=chunk_size)
        logger.info(f"Split into {len(chunks)} chunks")
        state = TranslationState(
            source_file=str(input_path),
            source_lang=source_lang,
            target_lang=target_lang,
            chunks=chunks,
        )
        save_state(output_path, state)
    total = len(state.chunks)
    pending = [i for i in range(total) if i not in state.translations]
    logger.info(f"Total chunks: {total}, pending: {len(pending)}")
    if not pending:
        logger.info("Nothing to translate — all chunks already done.")
        state.completed_at = state.completed_at or time.time()
        save_state(output_path, state)
        return state
    engine = TranslatorEngine(
        source_lang=source_lang,
        target_lang=target_lang,
        backend_order=backend_order,
    )
    state_lock = threading.Lock()
    stop_event = threading.Event()
    done_since_save = 0

    def worker(idx: int) -> tuple[int, Optional[str], Optional[str], Optional[str]]:
        if stop_event.is_set():
            return idx, None, None, "cancelled"
        text = state.chunks[idx]
        try:
            translated, backend = engine.translate_chunk(text)
            return idx, translated, backend, None
        except Exception as e:
            return idx, None, None, repr(e)

    completed_count = 0
    try:
        with GracefulShutdown() as shutdown, ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(worker, i): i for i in pending}
            for fut in as_completed(futures):
                idx, translated, backend, error = fut.result()
                completed_count += 1
                with state_lock:
                    if error is None and translated is not None:
                        state.translations[idx] = translated
                        state.backend_used[idx] = backend or "unknown"
                        state.failed.pop(idx, None)
                        logger.debug(f"[{completed_count}/{len(pending)}] chunk {idx} ok via {backend}")
                    else:
                        state.failed[idx] = error or "unknown error"
                        logger.error(f"[{completed_count}/{len(pending)}] chunk {idx} FAILED: {error}")
                        append_failed_chunk(failed_path, idx, state.chunks[idx], error or "unknown")
                    done_since_save += 1
                    need_save = (done_since_save >= save_every) or shutdown.event.is_set()
                    if need_save:
                        save_state(output_path, state)
                        done_since_save = 0
                if shutdown.event.is_set():
                    stop_event.set()
    finally:
        with state_lock:
            save_state(output_path, state)
    ok = len(state.translations)
    bad = len(state.failed)
    logger.info(f"Done. Translated: {ok}/{total}, Failed: {bad}")
    if bad:
        logger.warning(f"Failed chunks logged to {failed_path}")
    else:
        state.completed_at = time.time()
        save_state(output_path, state)
    return state


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="translate_chunks",
        description="Translate a text file into chunks with resume support.",
    )
    parser.add_argument("input", type=Path, help="Input text file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output JSON file (default: <input>.translations.json)",
    )
    parser.add_argument("-s", "--source", default="en", help="Source language code (ISO 639-1)")
    parser.add_argument("-t", "--target", required=True, help="Target language code (ISO 639-1)")
    parser.add_argument(
        "--backends",
        default=None,
        help="Comma-separated backend order (default: " + ",".join(DEFAULT_BACKEND_ORDER) + ")",
    )
    parser.add_argument("--chunk-size", type=int, default=2500, help="Chunk size in characters")
    parser.add_argument("--save-every", type=int, default=10, help="Save state every N chunks")
    parser.add_argument(
        "--debug-log",
        type=Path,
        default=None,
        help="Path to debug log file (default: <output>.debug.log)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress error output to stderr")
    parser.add_argument(
        "--list-backends",
        action="store_true",
        help="List known backends and exit",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_backends:
        print("Known backends (in default fallback order):")
        for b in DEFAULT_BACKEND_ORDER:
            print(f"  - {b}")
        print("\nOptional (not in default order):")
        for b in BACKEND_FACTORIES:
            if b not in DEFAULT_BACKEND_ORDER:
                print(f"  - {b}")
        return 0
    output = args.output or args.input.with_suffix(args.input.suffix + ".translations.json")
    debug_log = args.debug_log or output.with_suffix(output.suffix + ".debug.log")
    setup_logging(debug_log, error_stderr=not args.quiet)
    logger.info(f"=== translate_chunks start ===")
    logger.info(f"input={args.input} output={output} {args.source}->{args.target}")
    backend_order = None
    if args.backends:
        backend_order = [b.strip() for b in args.backends.split(",") if b.strip()]
        logger.info(f"Backend order override: {backend_order}")
    try:
        state = run_translation(
            input_path=args.input,
            output_path=output,
            source_lang=args.source,
            target_lang=args.target,
            backend_order=backend_order,
            chunk_size=args.chunk_size,
            max_workers=max(1, min(2, MAX_WORKERS)),
            save_every=max(1, args.save_every),
        )
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    except RuntimeError as e:
        logger.error(str(e))
        return 3
    except Exception as e:
        logger.exception(f"Unhandled error: {e}")
        return 1
    total = len(state.chunks)
    ok = len(state.translations)
    bad = len(state.failed)
    print(f"\nTranslated {ok}/{total} chunks ({bad} failed).")
    print(f"Output: {output}")
    if bad:
        print(f"Failures: {output.with_suffix(output.suffix + '.failed.jsonl')}")
    return 0 if bad == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
