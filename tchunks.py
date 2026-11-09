#!/data/data/com.termux/files/usr/bin/env python
"""Split a text file into chunks, translate each chunk, and save results to JSON."""

import argparse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import FrameType
from typing import TYPE_CHECKING

from dh import MAX_WORKERS
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class TranslationFailedError(Exception):
    pass


class UnknownBackendError(Exception):
    pass


class NoBackendAvailableError(Exception):
    pass


_DEEPL_TARGET_OVERRIDES: dict[str, str] = {
    "en": "EN-US",
    "pt": "PT-PT",
    "zh": "ZH",
}


def _lang_for_deepl(code: str, *, is_target: bool) -> str:
    code = code.strip()
    if is_target and code.lower() in _DEEPL_TARGET_OVERRIDES:
        return _DEEPL_TARGET_OVERRIDES[code.lower()]
    return code.upper()


def _lang_for_deep_translator(code: str, *, is_target: bool) -> str:
    return code.strip().lower()


def _lang_for_translate_pkg(code: str, *, is_target: bool) -> str:
    return code.strip().lower()


def _lang_for_translators_bing(code: str, *, is_target: bool) -> str:
    return code.strip().lower()


def _lang_for_googletrans(code: str, *, is_target: bool) -> str:
    return code.strip().lower()


def _lang_for_pygoogletranslation(code: str, *, is_target: bool) -> str:
    return code.strip().lower()


def _make_deepl(source: str, target: str) -> Callable[[str], str]:
    import deepl

    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        msg = "DEEPL_API_KEY is not set"
        raise NoBackendAvailableError(msg)
    translator = deepl.Translator(api_key)
    src = _lang_for_deepl(source, is_target=False)
    tgt = _lang_for_deepl(target, is_target=True)

    def translate(text: str) -> str:
        result = translator.translate_text(text, source_lang=src, target_lang=tgt)
        return str(result.text)

    return translate


def _make_deep_translator(source: str, target: str) -> Callable[[str], str]:
    from deep_translator import GoogleTranslator

    src = _lang_for_deep_translator(source, is_target=False)
    tgt = _lang_for_deep_translator(target, is_target=True)

    def translate(text: str) -> str:
        client = GoogleTranslator(source=src, target=tgt)
        result = client.translate(text)
        if result is None:
            msg = "deep_translator returned None"
            raise TranslationFailedError(msg)
        return str(result)

    return translate


def _make_translate(source: str, target: str) -> Callable[[str], str]:
    from translate import Translator as TranslatePkgTranslator

    src = _lang_for_translate_pkg(source, is_target=False)
    tgt = _lang_for_translate_pkg(target, is_target=True)

    def translate(text: str) -> str:
        client = TranslatePkgTranslator(from_lang=src, to_lang=tgt)
        result = client.translate(text)
        if not result:
            msg = "translate package returned empty result"
            raise TranslationFailedError(msg)
        return str(result)

    return translate


def _make_translators_bing(source: str, target: str) -> Callable[[str], str]:
    import translators as ts

    src = _lang_for_translators_bing(source, is_target=False)
    tgt = _lang_for_translators_bing(target, is_target=True)

    def translate(text: str) -> str:
        result = ts.translate_text(text, translator="bing", from_language=src, to_language=tgt)
        if not result:
            msg = "translators (bing) returned empty result"
            raise TranslationFailedError(msg)
        return str(result)

    return translate


_googletrans_lock = threading.Lock()


def _make_googletrans(source: str, target: str) -> Callable[[str], str]:
    from googletrans import Translator as GoogleTransTranslator

    src = _lang_for_googletrans(source, is_target=False)
    tgt = _lang_for_googletrans(target, is_target=True)
    client = GoogleTransTranslator()

    def translate(text: str) -> str:
        with _googletrans_lock:
            result = client.translate(text, src=src, dest=tgt)
        if not result or not result.text:
            msg = "googletrans returned empty result"
            raise TranslationFailedError(msg)
        return str(result.text)

    return translate


_pygoogletranslation_lock = threading.Lock()


def _make_pygoogletranslation(source: str, target: str) -> Callable[[str], str]:
    from pygoogletranslation import Translator as PyGoogleTranslator

    src = _lang_for_pygoogletranslation(source, is_target=False)
    tgt = _lang_for_pygoogletranslation(target, is_target=True)
    client = PyGoogleTranslator()

    def translate(text: str) -> str:
        with _pygoogletranslation_lock:
            result = client.translate(text, src=src, dest=tgt)
        if not result or not result.text:
            msg = "pygoogletranslation returned empty result"
            raise TranslationFailedError(msg)
        return str(result.text)

    return translate


BACKEND_FACTORIES: dict[str, Callable[[str, str], Callable[[str], str]]] = {
    "deepl": _make_deepl,
    "deep_translator": _make_deep_translator,
    "translate": _make_translate,
    "translators_bing": _make_translators_bing,
    "googletrans": _make_googletrans,
    "pygoogletranslation": _make_pygoogletranslation,
}
FALLBACK_ORDER: tuple[str, ...] = (
    "deepl",
    "deep_translator",
    "translate",
    "translators_bing",
    "googletrans",
    "pygoogletranslation",
)


def select_backend(requested: str | None, source: str, target: str) -> tuple[str, Callable[[str], str]]:
    if requested:
        if requested not in BACKEND_FACTORIES:
            valid = ", ".join(sorted(BACKEND_FACTORIES))
            msg = f"unknown backend '{requested}'. Valid options: {valid}"
            raise UnknownBackendError(msg)
        factory = BACKEND_FACTORIES[requested]
        translate_fn = factory(source, target)
        return requested, translate_fn
    last_error: Exception | None = None
    for name in FALLBACK_ORDER:
        if name == "deepl" and not os.environ.get("DEEPL_API_KEY"):
            logger.debug("Skipping deepl in fallback: DEEPL_API_KEY not set")
            continue
        try:
            factory = BACKEND_FACTORIES[name]
            translate_fn = factory(source, target)
            logger.debug(f"Selected backend via fallback order: {name}")
            return name, translate_fn
        except Exception as e:
            logger.debug(f"Backend '{name}' unavailable during fallback: {e}")
            last_error = e
            continue
    msg = f"no backend could be constructed from fallback order {FALLBACK_ORDER}. Last error: {last_error}"
    raise NoBackendAvailableError(msg)


def chunk_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        msg = "chunk_size must be positive"
        raise ValueError(msg)
    chunks: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        end = min(pos + chunk_size, length)
        if end < length:
            split_at = text.rfind(" ", pos, end)
            for ws_char in ("\n", "\t"):
                candidate = text.rfind(ws_char, pos, end)
                split_at = max(split_at, candidate)
            if split_at > pos:
                end = split_at
        piece = text[pos:end].strip()
        if piece:
            chunks.append(piece)
        pos = end
        while pos < length and text[pos] in " \t\n\r":
            pos += 1
    return chunks


def _normalize_for_comparison(s: str) -> str:
    return " ".join(s.lower().split())


def looks_untranslated(source_text: str, translated_text: str, source_lang: str, target_lang: str) -> bool:
    if source_lang.strip().lower() == target_lang.strip().lower():
        return False
    return _normalize_for_comparison(source_text) == _normalize_for_comparison(translated_text)


def translate_with_retry(
    translate_fn: Callable[[str], str],
    text: str,
    source_lang: str,
    target_lang: str,
    delay: float,
) -> str:
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(TranslationFailedError),
    )
    def _attempt() -> str:
        time.sleep(delay)
        try:
            result = translate_fn(text)
        except TranslationFailedError:
            raise
        except Exception as e:
            msg = f"backend call raised: {e}"
            raise TranslationFailedError(msg) from e
        if not result or not result.strip():
            msg = "backend returned empty translation"
            raise TranslationFailedError(msg)
        if looks_untranslated(text, result, source_lang, target_lang):
            msg = "translation looks identical to source (likely untranslated)"
            raise TranslationFailedError(msg)
        return result

    return _attempt()


def load_existing_results(output_path: Path) -> dict[str, str]:
    if not output_path.exists():
        return {}
    try:
        with output_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(f"{output_path} did not contain a JSON object; ignoring")
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load existing output {output_path}: {e}")
        return {}


def save_results_atomic(output_path: Path, results: dict[str, str]) -> None:
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    ordered = {str(k): results[str(k)] for k in sorted(results, key=int)}
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def append_failed_index(failed_path: Path, index: int) -> None:
    with failed_path.open("a", encoding="utf-8") as f:
        f.write(f"{index}\n")


_shutdown_requested = threading.Event()


def _handle_sigint(signum: int, frame: FrameType | None) -> None:
    logger.info("Ctrl+C received, finishing in-flight chunks and saving...")
    _shutdown_requested.set()


def run_translation(
    chunks: list[str],
    existing: dict[str, str],
    translate_fn: Callable[[str], str],
    source_lang: str,
    target_lang: str,
    output_path: Path,
    failed_path: Path,
    workers: int,
    delay: float,
    save_every: int,
) -> dict[str, str]:
    results = dict(existing)
    pending_indices = [i for i in range(len(chunks)) if str(i) not in results]
    if not pending_indices:
        logger.info("All chunks already translated; nothing to do.")
        return results
    logger.info(f"Translating {len(pending_indices)} of {len(chunks)} chunk(s) with {workers} worker(s)")
    completed_since_save = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index: dict[Future[str], int] = {}
        for idx in pending_indices:
            future = executor.submit(
                translate_with_retry,
                translate_fn,
                chunks[idx],
                source_lang,
                target_lang,
                delay,
            )
            future_to_index[future] = idx
        remaining = set(future_to_index)
        while remaining:
            if _shutdown_requested.is_set():
                logger.info(f"Shutdown requested: cancelling {len(remaining)} pending chunk(s)")
                for fut in remaining:
                    fut.cancel()
                break
            done_now: set[Future[str]] = set()
            for fut in list(remaining):
                if fut.done():
                    done_now.add(fut)
            if not done_now:
                time.sleep(0.1)
                continue
            for fut in done_now:
                idx = future_to_index[fut]
                try:
                    translated = fut.result()
                    results[str(idx)] = translated
                    logger.debug(f"Chunk {idx} translated ({len(translated)} chars)")
                except Exception as e:
                    logger.error(f"Chunk {idx} failed after retries: {e}")
                    append_failed_index(failed_path, idx)
                completed_since_save += 1
                remaining.discard(fut)
                if completed_since_save >= save_every:
                    save_results_atomic(output_path, results)
                    completed_since_save = 0
                    logger.debug(f"Progress saved ({len(results)}/{len(chunks)} chunks)")
    save_results_atomic(output_path, results)
    logger.info(f"Saved {len(results)}/{len(chunks)} chunk(s) to {output_path}")
    return results


def configure_logging(log_file: Path) -> None:
    logger.remove()
    logger.add(sys.stderr, level="ERROR", backtrace=False, diagnose=False)
    logger.add(
        log_file,
        level="DEBUG",
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Split a text file into chunks, translate each chunk, and save results to JSON.",
    )
    ap.add_argument("-i", "--input", type=Path, default=Path("input.txt"), help="Input text file.")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("chunks.json"),
        help="Output JSON file.",
    )
    ap.add_argument(
        "--failed",
        type=Path,
        default=Path("failed.txt"),
        help="Failed chunk indices log.",
    )
    ap.add_argument("-s", "--source", default="en", help="Source language code (ISO 639-1).")
    ap.add_argument("-t", "--target", default="fr", help="Target language code (ISO 639-1).")
    ap.add_argument(
        "-b",
        "--backend",
        default=None,
        choices=sorted(BACKEND_FACTORIES),
        help="Translator backend. If omitted, tries the fallback order automatically.",
    )
    ap.add_argument("-d", "--delay", type=float, default=0.5, help="Seconds between requests.")
    ap.add_argument("--chunk-size", type=int, default=2500, help="Characters per chunk.")
    ap.add_argument("--save-every", type=int, default=10, help="Save JSON every N completed chunks.")
    ap.add_argument(
        "--no-continue",
        action="store_true",
        help="Start fresh: ignore any existing output file instead of resuming.",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workers = max(1, min(MAX_WORKERS, 4))
    log_file = args.output.with_suffix(".log")
    configure_logging(log_file)
    signal.signal(signal.SIGINT, _handle_sigint)
    if not args.input.exists():
        logger.error(f"Input file not found: {args.input}")
        return 1
    try:
        text = args.input.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Could not read input file {args.input}: {e}")
        return 1
    chunks = chunk_text(text, args.chunk_size)
    if not chunks:
        logger.error("Input file produced zero chunks after splitting (empty file?)")
        return 1
    logger.info(f"Split input into {len(chunks)} chunk(s) of up to {args.chunk_size} chars")
    existing: dict[str, str] = {} if args.no_continue else load_existing_results(args.output)
    if existing:
        logger.info(f"Resuming: {len(existing)} chunk(s) already translated in {args.output}")
    try:
        backend_name, translate_fn = select_backend(args.backend, args.source, args.target)
    except (UnknownBackendError, NoBackendAvailableError) as e:
        logger.error(str(e))
        return 1
    logger.info(f"Using backend: {backend_name} ({args.source} -> {args.target})")
    results = run_translation(
        chunks=chunks,
        existing=existing,
        translate_fn=translate_fn,
        source_lang=args.source,
        target_lang=args.target,
        output_path=args.output,
        failed_path=args.failed,
        workers=workers,
        delay=args.delay,
        save_every=args.save_every,
    )
    failed_count = len(chunks) - len(results)
    if failed_count > 0:
        logger.warning(f"{failed_count} chunk(s) failed; see {args.failed}")
        return 2
    logger.info("All chunks translated successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
