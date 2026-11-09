#!/data/data/com.termux/files/usr/bin/env python
"""Translate a word list into a JSON mapping.
Reads one word per line, translates each of them with ``translate``, ``deep_translator`` or ``googletrans`` (falling back automatically), retries the failures, throttles the requests, saves the output periodically and collects the words that kept failing in ``failed.txt``.
Usage: atrans [-i words.txt] [-o words.json] [-s fr] [-t en] Options: -i, --input Input file with one word per line (default: words.txt) -o, --output Output JSON file (default: words.json) --failed File collecting words that failed all retries (default: failed.txt) -s, --source Source language code (default: fr) -t, --target Target language code (default: en) -b, --backend Preferred translation backend (default: translate); falls back automatically if unusable -d, --delay Delay in seconds between requests per worker (default: 0.3) --save-every Save the output file every N words (default: 100) --no-continue Do not resume from a previous job; start from scratch Examples: $ atrans -i words.txt -o words.json Command-line script: run ``python atrans.py`` or the ``atrans`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, TextIO

from dh import MAX_WORKERS
from loguru import logger


logger.remove()
logger.add(
    "translate_words.log",
    level="DEBUG",
    rotation="10 MB",
    retention=5,
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {thread.name} | {message}",
)
logger.add(
    sys.stderr,
    level="ERROR",
    format="<red>{level}</red>: {message}",
)
FALLBACK_ORDER: tuple[str, str, str] = ("translate", "deep_translator", "googletrans")
MAX_RETRIES: int = 3


class BackendError(Exception):
    pass


def _make_translate(source: str, target: Path, script_path: Path) -> Callable[..., Any]:
    try:
        import translate
    except ImportError as e:
        msg = f"cannot import 'translate': {e}"
        raise BackendError(msg)
    mod_file = Path(getattr(translate, "__file__", "") or "").absolute()
    if mod_file and mod_file == Path(script_path).absolute():
        msg = "'translate' resolves to this script itself (rename the script to avoid shadowing the package)"
        raise BackendError(msg)
    cls = getattr(translate, "Translator", None) or getattr(translate, "GoogleTranslator", None)
    if cls is None:
        msg = "'translate' module exposes neither 'Translator' nor 'GoogleTranslator'"
        raise BackendError(msg)
    if cls.__name__ == "Translator":

        def call(text: str, cls=cls, source=source, target=target):
            return cls(from_lang=source, to_lang=target).translate(text)

    else:

        def call(text: str, cls=cls, source=source, target=target):
            return cls(source=source, target=target).translate(text)

    return call


def _make_deep_translator(source: str, target: Path, _script_path: Path) -> Callable[..., Any]:
    try:
        from deep_translator import GoogleTranslator
    except ImportError as e:
        msg = f"cannot import 'deep_translator': {e}"
        raise BackendError(msg)

    def call(text: str, cls=GoogleTranslator, source=source, target=target):
        return cls(source=source, target=target).translate(text)

    return call


def _make_googletrans(source: str, target: Path, _script_path: Path) -> Callable[..., Any]:
    try:
        from googletrans import Translator
    except ImportError as e:
        msg = f"cannot import 'googletrans': {e}"
        raise BackendError(msg)
    lock = threading.Lock()
    inst = Translator()

    def call(text: str, inst=inst, lock=lock, source=source, target=target):
        with lock:
            return inst.translate(text, src=source, dest=target).text

    return call


_BACKEND_FACTORIES: dict[str, Any] = {
    "translate": _make_translate,
    "deep_translator": _make_deep_translator,
    "googletrans": _make_googletrans,
}


class TranslatorWrapper:
    def __init__(self, preferred: str, source: str, target: Path, script_path: Path) -> None:
        self.source = source
        self.target = target
        self.backend_name = None
        self._call = None
        candidates = [preferred] + [b for b in FALLBACK_ORDER if b != preferred]
        errors = []
        for name in candidates:
            factory = _BACKEND_FACTORIES.get(name)
            if factory is None:
                errors.append((name, "unknown backend"))
                continue
            try:
                self._call = factory(source, target, script_path)
            except BackendError as e:
                errors.append((name, str(e)))
                logger.warning("Backend '{}' unavailable: {}", name, e)
                continue
            self.backend_name = name
            if name != preferred:
                print(f"⚠️  Backend '{preferred}' unavailable — falling back to '{name}'.")
            logger.info("Using backend '{}'", name)
            return
        print("Error: no usable translation backend found.", file=sys.stderr)
        for name, err in errors:
            print(f"  - {name}: {err}", file=sys.stderr)
            logger.error("Backend '{}' failed: {}", name, err)
        print("\nInstall at least one of:", file=sys.stderr)
        print("  pip install translate", file=sys.stderr)
        print("  pip install deep_translator", file=sys.stderr)
        print("  pip install googletrans==4.0.0rc1", file=sys.stderr)
        sys.exit(1)

    def translate(self, text: str):
        return self._call(text)


def load_progress(out_path: Path):
    if not Path(out_path).exists():
        return {}
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Could not read existing output file {}: {}", out_path, e)
    return {}


def save_output(out_path: Path, data: TextIO, lock: Any) -> None:
    with lock:
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, out_path)


def append_failed(failed_path: Path, word: str, lock: Any) -> None:
    with lock, open(failed_path, "a", encoding="utf-8") as f:
        f.write(word + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Translate a word list file line by line to JSON.")
    parser.add_argument(
        "-i",
        "--input",
        default="words.txt",
        help="Input file with one word per line (default: words.txt)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="words.json",
        help="Output JSON file (default: words.json)",
    )
    parser.add_argument(
        "--failed",
        default="failed.txt",
        help="File collecting words that failed all retries (default: failed.txt)",
    )
    parser.add_argument("-s", "--source", default="fr", help="Source language code (default: fr)")
    parser.add_argument("-t", "--target", default="en", help="Target language code (default: en)")
    parser.add_argument(
        "-b",
        "--backend",
        default="translate",
        choices=FALLBACK_ORDER,
        help="Preferred translation backend (default: translate); falls back automatically if unusable",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.3,
        help="Delay in seconds between requests per worker (default: 0.3)",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=100,
        help="Save the output file every N words (default: 100)",
    )
    parser.add_argument(
        "--no-continue",
        action="store_true",
        help="Do not resume from a previous job; start from scratch",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input
    out_path = args.output
    failed_path = args.failed
    if not Path(input_path).exists():
        print(f"Error: Input file '{input_path}' not found.")
        sys.exit(1)
    with open(input_path, "r", encoding="utf-8") as f:
        words = [line.strip() for line in f if line.strip()]
    print(f"Loaded {len(words)} words from {input_path}")
    print(f"Source -> target: {args.source} -> {args.target}")
    results = {} if args.no_continue else load_progress(out_path)
    if results:
        print(f"Resuming: {len(results)} words already translated.")
    pending = [w for w in words if w not in results]
    if not pending:
        print("Nothing to translate. Done.")
        return
    translator = TranslatorWrapper(
        preferred=args.backend,
        source=args.source,
        target=args.target,
        script_path=__file__,
    )
    print(
        f"Backend: {translator.backend_name} | "
        f"Workers: {MAX_WORKERS} | Delay: {args.delay}s | "
        f"Save every: {args.save_every} | Retries: {MAX_RETRIES}"
    )
    print(f"Words to translate: {len(pending)}")
    save_lock = threading.Lock()
    counter_lock = threading.Lock()
    print_lock = threading.Lock()
    failed_lock = threading.Lock()
    completed_count = [0]
    total_pending = len(pending)

    def translate_word(word: str):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                time.sleep(args.delay)
                raw = translator.translate(word)
                text = "" if raw is None else str(raw).strip()
                if not text:
                    msg = "empty translation"
                    raise ValueError(msg)
                if text.casefold() == word.casefold():
                    msg = f"identity translation returned: {text!r}"
                    raise ValueError(msg)
                with print_lock:
                    print(f"  → {word!r} returned {text!r}")
                return word, text
            except Exception as e:
                logger.warning(
                    "Attempt {}/{} failed for {!r}: {}",
                    attempt,
                    MAX_RETRIES,
                    word,
                    e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(args.delay * (attempt + 1))
        logger.error("All {} attempts failed for {!r}", MAX_RETRIES, word)
        return word, None

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(translate_word, w): w for w in pending}
            for future in as_completed(futures):
                word, translation = future.result()
                with counter_lock:
                    completed_count[0] += 1
                    count = completed_count[0]
                if translation is None:
                    append_failed(failed_path, word, failed_lock)
                    with print_lock:
                        print(f"[{count}/{total_pending}] ✗ {word} (failed -> {failed_path})")
                else:
                    results[word] = translation
                    with print_lock:
                        print(f"[{count}/{total_pending}] ✓ {word} -> {translation}")
                if count % args.save_every == 0:
                    save_output(out_path, results, save_lock)
                    with print_lock:
                        print(f"  💾 Saved progress ({count}/{total_pending})")
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. Saving progress...")
        save_output(out_path, results, save_lock)
        print(f"Progress saved to {out_path}. Run again to resume.")
        logger.info("Interrupted by user; saved {} entries", len(results))
        sys.exit(0)
    save_output(out_path, results, save_lock)
    print(f"\n✅ Done! Translated {len(results)} words total.")
    print(f"Output: {out_path}")
    if Path(failed_path).exists():
        with open(failed_path, "r", encoding="utf-8") as f:
            failed_count = sum(1 for _ in f if _.strip())
        print(f"Failed words ({failed_count}): {failed_path}")


if __name__ == "__main__":
    main()
