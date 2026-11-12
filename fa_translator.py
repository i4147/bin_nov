#!/data/data/com.termux/files/usr/bin/env python
"""Persian to English (and back) translation toolkit.
Subcommands: ``file`` translates a text file line by line or in chunks, ``words`` builds a JSON dictionary from a word list and ``lookup`` is an offline dictionary search with optional fzf selection.
The backends are deep_translator, the ``translate`` package and the ``translate`` CLI.
Usage: fa_translator {file,words,lookup} [options] Positional arguments: input Path to the input text file word ...
Word(s) to translate Subcommands: file Translate a text file (line-by-line or chunked).
words Translate a word list into a JSON dictionary.
lookup Offline dictionary lookup (with optional fzf).
Options: -v, --verbose Enable debug logging --mode Translate each line independently (default) or in chunks --chunk-size Max characters per chunk in --mode chunk (default: 4500) --backend Translation backend (default: deep-translator) --source Source language --target Target language --executor Pool kind (default: thread) --persian-only Only translate lines that contain Persian characters --in-place Rewrite the input file with translations --output JSON output path (default: <input>.json) ...
plus 11 more; run ``fa_translator --help`` for the full list.
Examples: $ fa_translator file notes.txt --target en $ fa_translator lookup Command-line script: run ``python fa_translator.py`` or the ``fa_translator`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from difflib import get_close_matches
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Optional, Self, Sequence, TextIO

from dh import MAX_WORKERS
from loguru import logger

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None
try:
    from translate import Translator as PyTranslator
except ImportError:
    PyTranslator = None
try:
    from tqdm import tqdm
except ImportError:

    class _DummyTqdm:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def update(self, _n: int = 1) -> None:
            pass

        def close(self) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def tqdm(iterable: Any = None, *_: Any, **__: Any) -> Any:
        if iterable is not None and hasattr(iterable, "__iter__"):
            return iterable
        return _DummyTqdm()


try:
    import readline

    _HAVE_READLINE = True
except ImportError:
    _HAVE_READLINE = False
log = logger


def setup_logging(verbose: bool = False) -> None:
    pass


PERSIAN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def is_persian(text: str) -> bool:
    return bool(PERSIAN_RE.search(text))


def load_json_dict(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        msg = f"{path} does not contain a JSON object"
        raise ValueError(msg)
    return {str(k).strip(): str(v).strip() for k, v in data.items()}


def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def translate_google(
    text: str,
    source: str = "auto",
    target: str = "en",
    retries: int = 3,
    delay: float = 0.5,
) -> Optional[str]:
    if GoogleTranslator is None:
        msg = "deep-translator is not installed (pip install deep-translator)"
        raise RuntimeError(msg)
    translator = GoogleTranslator(source=source, target=target)
    for attempt in range(retries):
        try:
            result = translator.translate(text)
            if result:
                return result
        except Exception as exc:
            log.warning(
                "google translate failed for {} (attempt {}/{}): {}",
                text[:40],
                attempt + 1,
                retries,
                exc,
            )
            if attempt < retries - 1:
                time.sleep(delay)
    return None


def translate_pypackage(
    text: str,
    source: str = "en",
    target: str = "fa",
    retries: int = 1,
    delay: float = 0.5,
) -> Optional[str]:
    if PyTranslator is None:
        msg = "translate package is not installed (pip install translate)"
        raise RuntimeError(msg)
    translator = PyTranslator(from_lang=source, to_lang=target)
    for attempt in range(retries):
        try:
            result = translator.translate(text)
            if result:
                return result
        except Exception as exc:
            log.warning(
                "translate-package failed for {} (attempt {}/{}): {}",
                text[:40],
                attempt + 1,
                retries,
                exc,
            )
            if attempt < retries - 1:
                time.sleep(delay)
    return None


def translate_cli(
    text: str,
    source: str = "en",
    target: str = "fa",
    retries: int = 1,
    delay: float = 0.5,
) -> Optional[str]:
    cmd = ["translate-cli", "-f", source, "-t", target, "-o", text]
    last_err = ""
    for attempt in range(retries):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return "__ERROR__: translate-cli not found in PATH"
        except Exception as exc:
            return f"__ERROR__: {exc}"
        if proc.returncode == 0:
            return proc.stdout.strip()
        last_err = proc.stderr.strip()
        if attempt < retries - 1:
            time.sleep(delay)
    return f"__ERROR__: {last_err}"


_BACKENDS: dict[str, Any] = {
    "deep-translator": translate_google,
    "translate-package": translate_pypackage,
    "translate-cli": translate_cli,
}


def get_backend(name: str):
    try:
        return _BACKENDS[name]
    except KeyError:
        msg = f"unknown backend: {name!r} (choose from {sorted(_BACKENDS)})"
        raise ValueError(msg) from None


def _translate_item(
    item: tuple[str, str, str, str, int, float],
) -> tuple[str, Optional[str]]:
    text, backend_name, source, target, retries, delay = item
    try:
        backend = get_backend(backend_name)
        result = backend(text, source=source, target=target, retries=retries, delay=delay)
    except Exception as exc:
        log.warning("worker error for {}: {}", text[:40], exc)
        result = None
    return text, result


def split_into_chunks(lines: list[str], max_chars: int) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    buf: list[str] = []
    buf_len = 0
    start = 0
    for i, line in enumerate(lines):
        n = len(line) + 1
        if buf_len + n > max_chars and buf:
            chunks.append((start, i - 1, "\n".join(buf)))
            buf, buf_len, start = [], 0, i
        if n > max_chars:
            if buf:
                chunks.append((start, i - 1, "\n".join(buf)))
                buf, buf_len, start = [], 0, i
            chunks.append((i, i, line))
            start = i + 1
        else:
            buf.append(line)
            buf_len += n
    if buf:
        chunks.append((start, len(lines) - 1, "\n".join(buf)))
    return chunks


def cmd_file(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.is_file():
        log.error("Input file not found: {}", input_path)
        return 1
    try:
        raw_lines = input_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        log.error("Error reading {}: {}", input_path, exc)
        return 1
    lines = [ln.strip() for ln in raw_lines if ln.strip()]
    if not lines:
        print(f"No lines found in {input_path.name}")
        return 0
    if args.persian_only:
        pending = [ln for ln in lines if is_persian(ln)]
        skipped = len(lines) - len(pending)
    else:
        pending = list(lines)
        skipped = 0
    print(f"Loaded {len(lines)} lines: {len(pending)} to translate, {skipped} skipped")
    if not pending:
        print("Nothing to translate.")
        return 0
    backend_name = args.backend
    source, target = args.source, args.target
    retries, delay = args.retries, args.delay
    if args.mode == "line":
        work_units = list(pending)
    else:
        work_units = [text for _s, _e, text in split_into_chunks(pending, args.chunk_size)]
    print(f"Translating {len(work_units)} unit(s) with {MAX_WORKERS} workers via {backend_name} ({args.executor})...")
    PoolClass = ThreadPoolExecutor if args.executor == "thread" else ProcessPoolExecutor
    packed = [(text, backend_name, source, target, retries, delay) for text in work_units]
    results: list[tuple[str, Optional[str]]] = []
    try:
        with PoolClass(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_translate_item, item) for item in packed]
            for i, fut in enumerate(futures, 1):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    log.error("Unexpected worker error: {}", exc)
                    results.append(("", None))
                if args.progress and (i % 5 == 0 or i == len(futures)):
                    print(f"  progress: {i}/{len(futures)}")
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130
    translations: dict[str, str] = {}
    if args.mode == "line":
        for orig, trans in results:
            if trans:
                translations[orig] = trans
    else:
        for chunk_text, trans in results:
            if not trans:
                continue
            src_lines = chunk_text.split("\n")
            dst_lines = trans.split("\n")
            for i, src_line in enumerate(src_lines):
                if i < len(dst_lines):
                    translations[src_line] = dst_lines[i]
                else:
                    log.error(
                        "Line-count mismatch in chunk, missing translation for: {}",
                        src_line[:50],
                    )
    if not translations:
        log.error("No translations were produced.")
        return 1
    output_path = Path(args.output) if args.output else input_path.with_suffix(".json")
    if args.output_format == "chunks" and args.mode == "chunk":
        payload: Any = {
            "translations": [
                {
                    "chunk_id": str(i),
                    "original": orig,
                    "translated": trans,
                }
                for i, (orig, trans) in enumerate(results)
                if trans
            ]
        }
    elif args.output_format == "numbered":
        payload = [
            {
                "line_number": i + 1,
                "source": src,
                "translation": translations.get(src),
            }
            for i, src in enumerate(pending)
        ]
    else:
        payload = translations
    try:
        save_json(payload, output_path)
        print(f"Saved {len(translations)} translations to {output_path.name}")
    except Exception as exc:
        log.error("Error saving JSON: {}", exc)
    if args.in_place:
        try:
            out_lines = [translations.get(ln, ln) for ln in lines]
            input_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            print(f"Updated {input_path.name}")
        except Exception as exc:
            log.error("Error updating input file: {}", exc)
            return 1
    return 0


def cmd_words(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.is_file():
        log.error("Input file not found: {}", input_path)
        return 1
    output_path = Path(args.output)
    seen: set[str] = set()
    words: list[str] = []
    for raw in input_path.read_text(encoding="utf-8").splitlines():
        w = raw.strip()
        if not w:
            continue
        if args.dedupe and w in seen:
            continue
        seen.add(w)
        words.append(w)
    if not words:
        print(f"No words found in {input_path}")
        return 0
    print(f"Loaded {len(words)} words from {input_path}")
    existing: dict[str, str] = {}
    if args.resume and output_path.exists():
        try:
            existing = load_json_dict(output_path)
            print(f"Loaded {len(existing)} existing translations from {output_path}")
        except Exception as exc:
            log.warning("Could not load existing {}: {}", output_path, exc)
    pending = [w for w in words if w not in existing]
    print(f"{len(pending)} to translate (skipping {len(words) - len(pending)} already translated)")
    if not pending:
        print("Nothing to do.")
        return 0
    results: dict[str, str] = dict(existing)
    progress = tqdm(total=len(pending), desc="Translating", unit="word")
    PoolClass = ThreadPoolExecutor if args.executor == "thread" else ProcessPoolExecutor
    packed = [(w, args.backend, args.source, args.target, args.retries, args.delay) for w in pending]
    saved_count = 0
    try:
        with PoolClass(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_translate_item, item) for item in packed]
            for fut in as_completed(futures):
                try:
                    word, trans = fut.result()
                except Exception as exc:
                    log.error("Worker error: {}", exc)
                    progress.update(1)
                    continue
                is_error = isinstance(trans, str) and trans.startswith("__ERROR__")
                if trans and not is_error:
                    results[word] = trans
                    print(f"{word} → {trans}")
                    saved_count += 1
                else:
                    log.error("Could not translate: {}", word)
                progress.update(1)
                if args.save_every and saved_count and (saved_count % args.save_every == 0):
                    try:
                        save_json(results, output_path)
                        log.info("Checkpoint: saved {} entries", len(results))
                    except Exception as exc:
                        log.error("Checkpoint save failed: {}", exc)
    except KeyboardInterrupt:
        print("Interrupted by user. Saving progress...")
    finally:
        progress.close()
        try:
            save_json(results, output_path)
            print(f"Saved {len(results)} entries to {output_path}")
        except Exception as exc:
            log.error("Error saving results: {}", exc)
            return 1
    failures = len(pending) - saved_count
    if failures:
        log.warning("{} entr(ies) failed.", failures)
    return 0


def _setup_readline(candidates: list[str]) -> None:
    if not _HAVE_READLINE:
        return
    words = sorted(candidates)

    def completer(text: str, state: int) -> Optional[str]:
        matches = [w for w in words if w.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n")


def _load_dictionary(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        log.error("Dictionary file not found: {}", path)
        sys.exit(1)
    try:
        fwd = load_json_dict(path)
    except Exception as exc:
        log.error("Error loading dictionary: {}", exc)
        sys.exit(1)
    rev = {v: k for k, v in fwd.items()}
    return fwd, rev


def _fuzzy(
    word: str,
    candidates: Iterable[str],
    n: int = 5,
    cutoff: float = 0.6,
) -> list[str]:
    return get_close_matches(word, list(candidates), n=n, cutoff=cutoff)


def _fzf_select(candidates: Iterable[str], prompt: str = "Select word: ") -> Optional[str]:
    if not shutil.which("fzf"):
        return None
    try:
        proc = subprocess.run(
            [
                "fzf",
                f"--prompt={prompt}",
                "--height=40%",
                "--layout=reverse",
                "--border",
            ],
            input="\n".join(sorted(candidates)),
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        log.error("fzf execution error: {}", exc)
        return None
    out = proc.stdout.strip()
    return out or None


def _interactive_loop(fwd: dict[str, str], rev: dict[str, str]) -> int:
    candidates = set(fwd) | set(rev)
    _setup_readline(list(candidates))
    print("\n🌐 Offline Persian ↔ English Translator")
    print("⌨  TAB for suggestions, Ctrl+C to exit\n")
    while True:
        try:
            query = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Bye.")
            return 0
        if not query:
            continue
        hit = fwd.get(query) or rev.get(query)
        if hit:
            print(f"✅ {hit}")
            continue
        matches = _fuzzy(query, candidates)
        if matches:
            print(f"❓ Not found. Did you mean: {', '.join(matches)}?")
        else:
            print("❌ Not found")


def cmd_lookup(args: argparse.Namespace) -> int:
    dict_path = Path(Path(args.dict).expanduser())
    fwd, rev = _load_dictionary(dict_path)
    candidates = set(fwd) | set(rev)
    if args.prefix:
        matches = sorted(w for w in candidates if w.startswith(args.prefix))
        if matches:
            print("\n".join(matches))
            return 0
        print(f"No matches found for prefix: {args.prefix}")
        return 1
    if args.fuzzy:
        matches = _fuzzy(args.fuzzy, candidates)
        if matches:
            print("\n".join(matches))
            return 0
        print(f"No close matches found for: {args.fuzzy}")
        return 1
    if args.word:
        word = " ".join(args.word).strip()
        hit = fwd.get(word) or rev.get(word)
        if hit:
            print(hit)
            return 0
        matches = _fuzzy(word, candidates)
        if matches:
            print(
                f"Not found. Did you mean: {', '.join(matches)}?",
                file=sys.stderr,
            )
        else:
            print("Not found", file=sys.stderr)
        return 1
    if not args.no_fzf:
        selected = _fzf_select(candidates)
        if selected:
            hit = fwd.get(selected) or rev.get(selected)
            print(f"{selected} → {hit}")
            return 0
        if shutil.which("fzf"):
            return 0
    return _interactive_loop(fwd, rev)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged_translator.py",
        description="Unified Persian ↔ English translation toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  merged_translator.py file notes.txt --persian-only --in-place\n"
            "  merged_translator.py file big.txt --mode chunk --output-format chunks\n"
            "  merged_translator.py words words.txt --output dic.json --workers 8\n"
            "  merged_translator.py lookup --prefix کتاب\n"
            "  merged_translator.py lookup\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    p_file = sub.add_parser(
        "file",
        help="Translate a text file (line-by-line or chunked).",
    )
    p_file.add_argument("input", help="Path to the input text file")
    p_file.add_argument(
        "--mode",
        choices=["line", "chunk"],
        default="line",
        help="Translate each line independently (default) or in chunks",
    )
    p_file.add_argument(
        "--chunk-size",
        type=int,
        default=4500,
        help="Max characters per chunk in --mode chunk (default: 4500)",
    )
    p_file.add_argument(
        "--backend",
        choices=sorted(_BACKENDS),
        default="deep-translator",
        help="Translation backend (default: deep-translator)",
    )
    p_file.add_argument("--source", default="fa", help="Source language")
    p_file.add_argument("--target", default="en", help="Target language")
    p_file.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="thread",
        help="Pool kind (default: thread)",
    )
    p_file.add_argument(
        "--persian-only",
        action="store_true",
        help="Only translate lines that contain Persian characters",
    )
    p_file.add_argument(
        "--in-place",
        action="store_true",
        help="Rewrite the input file with translations",
    )
    p_file.add_argument(
        "--output",
        default=None,
        help="JSON output path (default: <input>.json)",
    )
    p_file.add_argument(
        "--output-format",
        choices=["dict", "chunks", "numbered"],
        default="dict",
        help="JSON shape (default: dict)",
    )
    p_file.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries per translation unit (default: 3)",
    )
    p_file.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between retries (default: 0.5)",
    )
    p_file.add_argument("--progress", action="store_true", help="Print periodic progress lines")
    p_file.set_defaults(func=cmd_file)
    p_words = sub.add_parser("words", help="Translate a word list into a JSON dictionary.")
    p_words.add_argument("input", help="Path to the words file")
    p_words.add_argument(
        "--output",
        default="dic.json",
        help="Output JSON dict (default: dic.json)",
    )
    p_words.add_argument(
        "--backend",
        choices=sorted(_BACKENDS),
        default="deep-translator",
        help="Translation backend (default: deep-translator)",
    )
    p_words.add_argument("--source", default="auto", help="Source language (default: auto)")
    p_words.add_argument("--target", default="en", help="Target language (default: en)")
    p_words.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="thread",
        help="Pool kind (default: thread)",
    )
    p_words.add_argument(
        "--dedupe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove duplicate words (default: on)",
    )
    p_words.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip words already present in --output (default: on)",
    )
    p_words.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries per word (default: 3)",
    )
    p_words.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between retries (default: 0.5)",
    )
    p_words.add_argument(
        "--save-every",
        type=int,
        default=1000,
        help="Checkpoint the output after N new translations (0 disables)",
    )
    p_words.set_defaults(func=cmd_words)
    p_lookup = sub.add_parser("lookup", help="Offline dictionary lookup (with optional fzf).")
    p_lookup.add_argument("word", nargs="*", help="Word(s) to translate")
    p_lookup.add_argument(
        "--dict",
        default="~/dic.json",
        help="Path to the JSON dictionary (default: ~/dic.json)",
    )
    p_lookup.add_argument("--prefix", default=None, help="List words starting with this prefix")
    p_lookup.add_argument("--fuzzy", default=None, help="Fuzzy-search this string")
    p_lookup.add_argument(
        "--no-fzf",
        action="store_true",
        help="Disable the interactive fzf picker",
    )
    p_lookup.set_defaults(func=cmd_lookup)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
