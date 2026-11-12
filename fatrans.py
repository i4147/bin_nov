#!/data/data/com.termux/files/usr/bin/env python
"""Translate files to and from Farsi.
Subcommands: ``to-fa`` translates a file into Persian, ``lines-to-en`` translates the Farsi lines of one file, ``inplace-to-en`` walks a tree and rewrites the Farsi files in English and ``batch-json`` writes one JSON map per ``.txt`` file.
The chunks are retried and slept between the requests.
Usage: fatrans {to-fa,lines-to-en,inplace-to-en,batch-json} [options] Positional arguments: input Input file source Source language code (default: 'auto').
path Root directory to walk (default: current dir).
directory Directory containing the .txt files (default: current dir).
Subcommands: to-fa Translate a single file to Farsi (auto-detect source by default).
lines-to-en Translate Farsi lines of one file into English (line-by-line).
inplace-to-en Recursively translate Farsi files into English, in place.
batch-json Translate every *.txt in a directory into per-file JSON maps.
Options: --target Target language (default: fa) --output-suffix Suffix appended to the stem of the output file (default: fa).
--chunk-size Max chars per request (default: 5000, as in tofa.py).
--retries Translation retries per chunk (default: 3).
--overwrite Overwrite output if it already exists (default: skip).
--encoding Input encoding (default: utf-8).
--sleep Sleep seconds after each translated chunk (default: 1.5).
--final-sleep Sleep seconds after finishing each file (default: 2.0).
--suffixes File suffixes to process.
--exclude Directory names to skip.
...
plus 2 more; run ``fatrans --help`` for the full list.
Examples: $ fatrans to-fa README.md $ fatrans inplace-to-en notes/ Command-line script: run ``python fatrans.py`` or the ``fatrans`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from multiprocessing import Pool
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable, Optional, Sequence

from deep_translator import GoogleTranslator
from dh import MAX_WORKERS
from loguru import logger
from tqdm import tqdm

FARSI_RE: re.Pattern[str] = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
TOFA_MARKERS: tuple[str, ...] = (
    "\n",
    "\r\n",
    ".  ",
    "!  ",
    "?  ",
    "; ",
    ", ",
    " ",
)
INPLACE_BREAK_RE: re.Pattern[str] = re.compile(r"[\s\n\.\!\?\;]+")
DEFAULT_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".py", ".json", ".csv")
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "lazy",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
)


def read_text_any_encoding(path: Path) -> str:
    for enc in ("utf-8", "latin-1", "cp1252", "iso-8859-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except (OSError, UnicodeDecodeError):
            continue
    msg = f"Could not read file {path} with any encoding"
    raise OSError(msg)


def contains_farsi(text: str) -> bool:
    return bool(FARSI_RE.search(text))


def derive_output_path(src: Path, suffix: str) -> Path:
    return src.parent / f"{src.stem}_{suffix}{src.suffix}"


def translate_with_retry(
    text: str,
    source: str = "auto",
    target: str = "fa",
    retries: int = 3,
    base_delay: float = 1.0,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result is None:
                msg = "Translator returned None"
                raise RuntimeError(msg)
            return result
        except Exception as exc:
            last_error = exc
            print(
                f"[WARN] Translation failed (attempt {attempt + 1}/{retries}): {exc}",
                file=sys.stderr,
            )
            time.sleep(base_delay + attempt)
    msg = f"Translation failed after {retries} attempts: {last_error}"
    raise RuntimeError(msg)


def chunk_by_markers(
    text: str,
    max_chars: int,
    markers: Sequence[str] = TOFA_MARKERS,
) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        remaining = text[pos:]
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        head = remaining[:max_chars]
        cut = max_chars
        for marker in markers:
            idx = head.rfind(marker)
            if idx > 0:
                cut = idx + len(marker)
                break
        else:
            idx = head.rfind(" ")
            if idx > 0:
                cut = idx + 1
        chunks.append(remaining[:cut])
        pos += cut
    return chunks


def chunk_by_regex(
    text: str,
    max_chars: int,
    pattern: re.Pattern[str] = INPLACE_BREAK_RE,
) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        if end == len(text):
            chunks.append(text[pos:])
            break
        last = None
        for match in pattern.finditer(text, pos, end):
            last = match
        cut = last.end() if (last is not None and last.end() > pos) else end
        chunks.append(text[pos:cut])
        pos = cut
    return chunks


def collect_files(
    root: Path,
    suffixes: Iterable[str],
    exclude: Iterable[str],
) -> list[Path]:
    suffix_set = {s.lower() for s in suffixes}
    exclude_set = set(exclude)
    found: list[Path] = []
    for p in root.rglob("*"):
        if any(part.startswith(".") or part in exclude_set for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in suffix_set:
            found.append(p)
    return sorted(found)


def _worker_line_to_en(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or not contains_farsi(stripped):
        return None
    try:
        result = GoogleTranslator(source="fa", target="en").translate(stripped)
        return (stripped, result) if result else None
    except Exception as exc:
        logger.debug(f"Translation error for '{stripped[:20]}': {exc}")
        return None


def _worker_chunk_to_en(payload: tuple[str, float]) -> str:
    chunk, sleep_sec = payload
    if not contains_farsi(chunk):
        return chunk
    try:
        result = GoogleTranslator(source="fa", target="en").translate(chunk)
        if result:
            preview = result[:30].replace("\n", " ")
            print(f"Chunk translated: {preview}...")
            time.sleep(sleep_sec)
            return result
    except Exception as exc:
        logger.error(f"Chunk translation error: {exc}")
    return chunk


def _worker_file_to_dict(path: Path) -> tuple[Path, dict[str, str]]:
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            logger.warning(f"⚠️  Empty file: {path.name}")
            return (path, {})
        translated = GoogleTranslator(source="fa", target="en").translate(content)
        if translated is None:
            translated = ""
        src_lines = [l.strip() for l in content.split("\n") if l.strip()]
        out_lines = [l.strip() for l in translated.split("\n") if l.strip()]
        mapping: dict[str, str] = {}
        if len(src_lines) != len(out_lines):
            for i, line in enumerate(src_lines):
                if not line:
                    continue
                try:
                    res = GoogleTranslator(source="fa", target="en").translate(line)
                    mapping[line] = res or ""
                except Exception as exc:
                    mapping[line] = f"TRANSLATION_ERROR: {exc}"
                    logger.warning(f"  ⚠️  Error translating line {i + 1} in {path.name}: {exc}")
        else:
            mapping = dict(zip(src_lines, out_lines))
        print(f"✅ Translated: {path.name} ({len(mapping)} words)")
        return (path, mapping)
    except Exception as exc:
        logger.error(f"❌ Error processing {path.name}: {exc}")
        return (path, {})


def cmd_to_fa(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        logger.error(f"File not found: {src}")
        return 1
    dst = derive_output_path(src, args.output_suffix)
    if dst.exists() and not args.overwrite:
        print(f"[INFO] Output file already exists: {dst}")
        print(f"[INFO] Skipping translation (use --overwrite to re-run)")
        return 0
    print(f"[INFO] Reading file: {src}")
    try:
        content = read_text_any_encoding(src)
    except OSError as exc:
        logger.error(str(exc))
        return 1
    print(f"[INFO] File size: {len(content)} characters")
    print(f"[INFO] Input:   {src}")
    print(f"[INFO] Output:  {dst}")
    print(f"[INFO] Source language: {args.source}")
    try:
        translated = _translate_file_to_target(
            content,
            source=args.source,
            target=args.target,
            chunk_size=args.chunk_size,
            retries=args.retries,
        )
    except Exception as exc:
        logger.error(f"Translation failed: {exc}")
        return 1
    print(f"\n[INFO] Saving result to: {dst}")
    dst.write_text(translated, encoding="utf-8")
    print("\n[SUCCESS] Translation complete!")
    print(f"[INFO] Output file: {dst}")
    print(f"[INFO] Output size: {len(translated)} characters")
    return 0


def _translate_file_to_target(
    content: str,
    source: str,
    target: str,
    chunk_size: int,
    retries: int,
) -> str:
    if len(content) <= chunk_size:
        print(f"[INFO] Content fits in single request ({len(content)} chars)")
        print("[INFO] Translating...")
        return translate_with_retry(content, source=source, target=target, retries=retries)
    chunks = chunk_by_markers(content, chunk_size)
    total = len(chunks)
    print(f"[INFO] Content split into {total} chunks")
    print(f"[INFO] Chunk sizes: {[len(c) for c in chunks]}")
    results: list[str] = []
    progress = tqdm(total=total, desc="Translating", unit="chunk")
    try:
        for i, chunk in enumerate(chunks):
            print(f"\n[INFO] Translating chunk {i + 1}/{total} ({len(chunk)} chars)...")
            try:
                results.append(translate_with_retry(chunk, source=source, target=target, retries=retries))
            except Exception as exc:
                print(f"[ERROR] Failed to translate chunk {i + 1}: {exc}")
                results.append(chunk)
            finally:
                progress.update(1)
    finally:
        progress.close()
    return "".join(results)


def cmd_lines_to_en(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        logger.error(f"File not found: {src}")
        return 1
    try:
        lines = src.read_text(encoding=args.encoding).splitlines()
    except Exception as exc:
        logger.error(f"Error reading file: {exc}")
        return 1
    dst = derive_output_path(src, args.output_suffix)
    print(f"Translating {len(lines)} lines from {src.name}...")
    results: list[tuple[str, str]] = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_worker_line_to_en, line): line for line in lines}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                orig, trans = res
                print(f"{orig} -> {trans}")
                results.append(res)
    try:
        with dst.open("w", encoding="utf-8") as fh:
            for orig, trans in results:
                fh.write(f"{orig} = {trans}\n")
    except Exception as exc:
        logger.error(f"Error writing output file: {exc}")
        return 1
    print(f"✓ Translated output saved to {dst}")
    return 0


def cmd_inplace_to_en(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.exists():
        logger.error(f"Path does not exist: {root}")
        return 1
    files = collect_files(root, args.suffixes, args.exclude)
    if not files:
        print("No files found to process.")
        return 0
    print(f"Processing {len(files)} files...")
    for path in files:
        _inplace_translate_one(
            path,
            workers=MAX_WORKERS,
            chunk_size=args.chunk_size,
            sleep=args.sleep,
            final_sleep=args.final_sleep,
        )
    return 0


def _inplace_translate_one(
    path: Path,
    workers: int,
    chunk_size: int,
    sleep: float,
    final_sleep: float,
) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Skipping unreadable file {path}: {exc}")
        return
    if not contains_farsi(content):
        return
    print(f"Translating: {path.name}")
    chunks = chunk_by_regex(content, chunk_size)
    translated_chunks: list[str] = []
    with Pool(processes=workers) as pool:
        async_results = [pool.apply_async(_worker_chunk_to_en, ((c, sleep),)) for c in chunks]
        for ar in async_results:
            translated_chunks.append(ar.get())
    new_content = "".join(translated_chunks)
    try:
        path.write_text(new_content, encoding="utf-8")
        print(f"✓ Updated: {path.name}")
    except Exception as exc:
        logger.error(f"Error writing to {path}: {exc}")
    time.sleep(final_sleep)


def cmd_batch_json(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    files = sorted(directory.glob(args.glob))
    if not files:
        logger.error(f"❌ No files matching {args.glob!r} found in {directory}")
        return 1
    print(f"📚 Found {len(files)} file(s) to translate")
    print(f"🚀 Starting translation with {MAX_WORKERS} parallel workers")
    print("-" * 40)
    start = time.time()
    ok = 0
    fail = 0
    with Pool(processes=MAX_WORKERS) as pool:
        async_results = [pool.apply_async(_worker_file_to_dict, (f,)) for f in files]
        for path, ar in zip(files, async_results):
            try:
                src, mapping = ar.get()
                if mapping:
                    _save_translations(src, mapping, args.out_dir)
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                logger.error(f"❌ Failed to process {path.name}: {exc}")
                fail += 1
    elapsed = time.time() - start
    print("=" * 40)
    print("✨ Translation complete!")
    print(f"   ✅ Successful: {ok} files")
    if fail:
        logger.warning(f"   ❌ Failed: {fail} files")
    print(f"   ⏱️  Time elapsed: {elapsed:.2f} seconds")
    print(f"   📁 Output directory: {Path(args.out_dir).absolute()}")
    return 0


def _save_translations(
    src: Path,
    mapping: dict[str, str],
    out_dir: str,
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{src.stem}_translations.json"
    target.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"💾 Saved: {target.name}")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translator.py",
        description="Unified translation toolkit (merged tofa/transfa/transfa2/transfamp).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Subcommand → original script:\n"
            "  to-fa         -> tofa.py\n"
            "  lines-to-en   -> transfa.py\n"
            "  inplace-to-en -> transfa2.py\n"
            "  batch-json    -> transfamp.py\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "to-fa",
        help="Translate a single file to Farsi (auto-detect source by default).",
    )
    p.add_argument("input", help="Input file")
    p.add_argument(
        "source",
        nargs="?",
        default="auto",
        help="Source language code (default: 'auto').",
    )
    p.add_argument("--target", default="fa", help="Target language (default: fa)")
    p.add_argument(
        "--output-suffix",
        default="fa",
        help="Suffix appended to the stem of the output file (default: fa).",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=5000,
        help="Max chars per request (default: 5000, as in tofa.py).",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Translation retries per chunk (default: 3).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output if it already exists (default: skip).",
    )
    p.set_defaults(func=cmd_to_fa)
    p = sub.add_parser(
        "lines-to-en",
        help="Translate Farsi lines of one file into English (line-by-line).",
    )
    p.add_argument("input", help="Input file (utf-8)")
    p.add_argument(
        "--output-suffix",
        default="en",
        help="Suffix for the output file (default: en).",
    )
    p.add_argument("--encoding", default="utf-8", help="Input encoding (default: utf-8).")
    p.set_defaults(func=cmd_lines_to_en)
    p = sub.add_parser(
        "inplace-to-en",
        help="Recursively translate Farsi files into English, in place.",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root directory to walk (default: current dir).",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=4900,
        help="Max chunk chars (default: 4900, as in transfa2.py).",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=1.5,
        help="Sleep seconds after each translated chunk (default: 1.5).",
    )
    p.add_argument(
        "--final-sleep",
        type=float,
        default=2.0,
        help="Sleep seconds after finishing each file (default: 2.0).",
    )
    p.add_argument(
        "--suffixes",
        nargs="+",
        default=list(DEFAULT_SUFFIXES),
        help="File suffixes to process.",
    )
    p.add_argument(
        "--exclude",
        nargs="+",
        default=list(DEFAULT_EXCLUDES),
        help="Directory names to skip.",
    )
    p.set_defaults(func=cmd_inplace_to_en)
    p = sub.add_parser(
        "batch-json",
        help="Translate every *.txt in a directory into per-file JSON maps.",
    )
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing the .txt files (default: current dir).",
    )
    p.add_argument("--glob", default="*.txt", help="Glob pattern for input files (default: *.txt).")
    p.add_argument(
        "--out-dir",
        default="./translations",
        help="Directory where the *_translations.json files go.",
    )
    p.set_defaults(func=cmd_batch_json)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
