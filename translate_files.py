#!/data/data/com.termux/files/usr/bin/env python
"""Translate text files into Vietnamese, Korean, Chinese or Russian (unified CLI)."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from multiprocessing.pool import Pool
from pathlib import Path
import random
import re
import signal
import sys
import time
from typing import Final, Iterable, Sequence

from deep_translator import GoogleTranslator
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


try:
    from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False
ENCODINGS: Final = ("utf-8", "utf-8-sig", "utf-16", "cp1258", "gb18030")
CYRILLIC_RE: Final = re.compile(r"[\u0400-\u04FF\u0500-\u052F\u2DE0-\u2DFF\uA640-\uA69F\u1C80-\u1C8F]")
NON_ASCII_RE: Final = re.compile(r"[^\x00-\x7F]")


class RateLimitError(Exception):
    pass


class TransientError(Exception):
    pass


class InterruptFlag:
    def __init__(self) -> None:
        self.set: bool = False

    def __bool__(self) -> bool:
        return self.set

    def trigger(self, *_args) -> None:
        print("\n⚠️  Ctrl+C — finishing current chunk then stopping.")
        self.set = True


def read_text_auto(path: Path) -> str:
    for enc in ENCODINGS:
        try:
            return path.read_text(encoding=enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(2048)
        return b"\x00" not in head
    except OSError:
        return False


def chunk_smart(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind("\n\n")
        if cut == -1 or cut < max_chars // 4:
            cut = window.rfind("\n")
        if cut == -1 or cut < max_chars // 4:
            cut = window.rfind(" ")
        if cut == -1:
            cut = max_chars
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def chunk_fixed(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def chunk_lines(lines: Sequence[str], max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if line_len > max_chars:
            if current:
                chunks.append(current)
                current, current_len = [], 0
            chunks.append([line])
            continue
        if current_len + line_len > max_chars and current:
            chunks.append(current)
            current, current_len = [], 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _classify_error(exc: Exception) -> type[Exception]:
    msg = str(exc).lower()
    if any(k in msg for k in ("429", "rate limit", "too many", "quota")):
        return RateLimitError
    return TransientError


def translate_with_retry(
    text: str,
    *,
    source: str,
    target: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    raise_on_empty: bool = True,
) -> str:
    translator = GoogleTranslator(source=source, target=target)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = translator.translate(text)
            if result is None or (raise_on_empty and not result.strip()):
                msg = "Empty result returned from translator"
                raise TransientError(msg)
            return result
        except Exception as exc:
            last_exc = exc
            kind = _classify_error(exc)
            if kind is RateLimitError:
                print("   ⏳ Rate limited — backing off…")
            if attempt < attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay += random.uniform(0, delay * 0.25)
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def translate_single_shot(text: str, *, source: str, target: str) -> str:
    try:
        result = GoogleTranslator(source=source, target=target).translate(text)
        return result if result is not None else text
    except Exception as exc:
        print(f"Chunk translation error: {exc}")
        return text


def make_progress_path(src: Path) -> Path:
    return src.with_suffix(src.suffix + ".viprogress")


def make_output_path(src: Path, style: str) -> Path:
    if style == "suffix":
        return src.with_suffix(src.suffix + ".en")
    return src.with_name(f"{src.stem}_eng{src.suffix}")


def _save_vi_progress(src: Path, chunk_map: dict[int, str], total: int) -> None:
    payload = {
        "source": str(src),
        "saved_at": datetime.now().isoformat(),
        "total_chunks": total,
        "chunks": {str(k): v for k, v in chunk_map.items()},
    }
    try:
        make_progress_path(src).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"   ⚠️  Could not save progress: {exc}")


def _load_vi_progress(src: Path) -> dict[int, str]:
    p = make_progress_path(src)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if Path(data.get("source", "")) != src:
            return {}
        chunks = {int(k): v for k, v in data.get("chunks", {}).items()}
        print(f"   🔄 Resuming: {len(chunks)} chunk(s) already done")
        return chunks
    except Exception:
        return {}


def _clear_vi_progress(src: Path) -> None:
    make_progress_path(src).unlink(missing_ok=True)


def _translate_one_vi_file(src: Path, args: argparse.Namespace, flag: InterruptFlag) -> bool:
    out = make_output_path(src, "suffix")
    print(f"\n📄 {src.name}  →  {out.name}")
    try:
        text = read_text_auto(src)
    except Exception as exc:
        print(f"   ❌ Cannot read: {exc}")
        return False
    if not text.strip():
        print("   ⚠️  File is empty — skipping")
        return True
    chunks = chunk_smart(text, args.chunk_size)
    total = len(chunks)
    print(f"   📦 {total} chunk(s)  |  file size: {len(text):,} chars")
    done = _load_vi_progress(src)
    failures = 0
    for idx, chunk in enumerate(chunks):
        if flag:
            _save_vi_progress(src, done, total)
            print("   💾 Progress saved.")
            return False
        if idx in done:
            print(f"   [{idx + 1:>3}/{total}] ⏭  skipped (cached)")
            continue
        try:
            translated = translate_with_retry(
                chunk,
                source=args.source,
                target=args.target,
                attempts=args.attempts,
                base_delay=args.retry_base_delay,
                max_delay=args.retry_max_delay,
            )
            ok = True
        except Exception as exc:
            print(f"   ❌ Chunk {idx} failed after all retries: {exc}")
            translated, ok = chunk, False
        done[idx] = translated
        if not ok:
            failures += 1
        preview = chunk[:40].replace("\n", "↵").strip()
        mark = "✓" if ok else "✗"
        print(f"   [{idx + 1:>3}/{total}] {mark}  {preview!r}…")
        if (idx + 1) % args.save_every == 0:
            _save_vi_progress(src, done, total)
        if idx < total - 1 and not flag:
            time.sleep(args.delay)
    final_text = "\n".join(done[i] for i in range(total))
    try:
        out.write_text(final_text, encoding="utf-8")
        _clear_vi_progress(src)
        if failures:
            print(f"   ⚠️  Done with {failures} chunk(s) untranslated — check {out.name}")
        else:
            print(f"   ✅ Saved → {out}")
        return True
    except Exception as exc:
        print(f"   ❌ Failed to write output: {exc}")
        return False


def run_vi(args: argparse.Namespace) -> int:
    files = [p for p in Path.cwd().glob("*.txt") if p.is_file() and p.name not in SKIP_DIRS]
    if not files:
        print("No .txt files found to translate.")
        return 0
    flag = InterruptFlag()
    previous = signal.signal(signal.SIGINT, flag.trigger)
    try:
        for src in sorted(files):
            if not _translate_one_vi_file(src, args, flag):
                break
            time.sleep(args.file_delay)
    finally:
        signal.signal(signal.SIGINT, previous)
    return 0


def run_ko(args: argparse.Namespace) -> int:
    src = Path(args.input_path)
    if not src.exists():
        print(f"Error: File not found: {src}", file=sys.stderr)
        return 1
    allowed = {".txt", ".md", ".csv", ".json", ".py"}
    if src.suffix.lower() not in allowed:
        print(f"Error: unsupported extension {src.suffix!r}", file=sys.stderr)
        return 1
    try:
        text = src.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"Read error: {exc}", file=sys.stderr)
        return 1
    chunks = chunk_fixed(text, args.chunk_size)
    translator = GoogleTranslator(source=args.source, target=args.target)
    try:
        translated = "".join(translator.translate(c) for c in chunks)
    except Exception as exc:
        print(f"Translation error: {exc}", file=sys.stderr)
        return 1
    out = make_output_path(src, "stem")
    try:
        out.write_text(translated, encoding="utf-8")
    except Exception as exc:
        print(f"Write error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved translated file → {out}")
    return 0


def _translate_zh_file(path: Path, args: argparse.Namespace) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        print(f"Skipping unreadable file: {path}")
        return
    if not NON_ASCII_RE.search(text):
        return
    chunks = chunk_fixed(text, args.chunk_size)

    def _worker(chunk: str) -> str:
        return translate_single_shot(chunk, source=args.source, target=args.target)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        pieces = list(ex.map(_worker, chunks))
    translated = "".join(pieces)
    out = path.with_name(f"{path.stem}_eng{path.suffix}")
    try:
        out.write_text(translated, encoding="utf-8")
        print(f"Translated → {out.name}")
    except Exception as exc:
        print(f"Error writing {out}: {exc}")


def run_zh(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if not root.exists():
        print(f"Error: root path does not exist: {root}", file=sys.stderr)
        return 1
    candidates = [p for p in root.rglob("*") if p.is_file() and is_text_file(p)]
    print(f"Found {len(candidates)} text files to process")
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        futures = {ex.submit(_translate_zh_file, p, args): p for p in candidates}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                print(f"Error processing {path}: {exc}")
    return 0


def _ru_translate_chunk(line_group: list[str], args: argparse.Namespace):
    joined = "\n".join(line_group)
    translator = GoogleTranslator(source=args.source, target=args.target)
    for attempt in range(1, args.attempts + 1):
        try:
            result = translator.translate(joined)
            if result is not None:
                return line_group, result
        except Exception as exc:
            delay = args.retry_base_delay * 2 ** (attempt - 1)
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "Translate attempt {}/{} failed for chunk starting {}: {}. Retrying in {}s",
                attempt,
                args.attempts,
                (line_group[0][:60] + "...") if line_group else "",
                exc,
                delay,
            )
            if attempt < args.attempts:
                time.sleep(delay)
    return line_group, None


def _ru_per_line(lines: Iterable[str], args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        try:
            translated = GoogleTranslator(source=args.source, target=args.target).translate(line)
            out[line] = translated if translated is not None else line
        except Exception as exc:
            logger.error("Per-line fallback failed for {}: {}", line[:50], exc)
            out[line] = line
    return out


def _ru_store_chunk(
    line_group: list[str],
    translated: str | None,
    mapping: dict[str, str],
    idx: int,
    total: int,
    args: argparse.Namespace,
) -> None:
    if translated is None:
        logger.error(
            "Failed to translate chunk starting with: {}",
            (line_group[0][:60] + "...") if line_group else "",
        )
        return
    split = translated.splitlines()
    if len(split) == len(line_group):
        for orig, new in zip(line_group, split):
            mapping[orig] = new
    else:
        logger.warning(
            "Line-count mismatch ({} vs {}). Falling back to per-line translation.",
            len(line_group),
            len(split),
        )
        mapping.update(_ru_per_line(line_group, args))
    sample = line_group[0] if line_group else ""
    print(
        "Translated chunk {}/{} (sample: '{}' → '{}')".format(
            idx,
            total,
            sample[:40] + ("..." if len(sample) > 40 else ""),
            mapping.get(sample, "")[:60],
        )
    )


def run_ru(args: argparse.Namespace) -> int:
    src = Path(args.input_path)
    if not src.exists():
        print(f"Input file not found: {src}", file=sys.stderr)
        return 1
    try:
        with src.open(encoding="utf-8") as fh:
            lines = [raw.rstrip("\n") for raw in fh if raw.strip() != ""]
    except Exception as exc:
        print(f"Error reading input file: {exc}", file=sys.stderr)
        return 1
    if not lines:
        print(f"No non-empty lines found in {src.name}")
        return 0
    cyrillic = [ln for ln in lines if CYRILLIC_RE.search(ln)]
    skipped = len(lines) - len(cyrillic)
    print(f"Loaded {len(lines)} lines: {len(cyrillic)} with Cyrillic, {skipped} already non-Cyrillic/skipped")
    if not cyrillic:
        print(f"No Russian/Cyrillic lines to translate in {src.name}")
        return 0
    unique = dedupe_preserve_order(cyrillic)
    print(f"Deduplicated Russian lines: {len(unique)} unique from {len(cyrillic)} total")
    groups = chunk_lines(unique, args.chunk_size)
    if not groups:
        print("Nothing to translate after chunking.")
        return 0
    print(
        f"Created {len(groups)} chunk(s) from {len(unique)} unique lines "
        f"(max {args.chunk_size} chars/chunk), using {MAX_WORKERS} worker(s)"
    )
    mapping: dict[str, str] = {}
    total = len(groups)
    pool = Pool(processes=MAX_WORKERS)
    try:
        async_results = [pool.apply_async(_ru_translate_chunk, (g, args)) for g in groups]
        for idx, async_res in enumerate(async_results, start=1):
            group = groups[idx - 1]
            try:
                line_group, translated = async_res.get()
                _ru_store_chunk(line_group, translated, mapping, idx, total, args)
            except Exception as exc:
                logger.error(
                    "Unexpected error processing chunk starting with {}: {}",
                    (group[0][:60] + "...") if group else "",
                    exc,
                )
    finally:
        pool.close()
        pool.join()
    json_out = src.with_suffix(".json")
    try:
        with json_out.open("w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=2)
        print(f"Saved {len(mapping)} translations to {json_out.name}")
    except Exception as exc:
        logger.error("Error saving JSON file: {}", exc)
    try:
        with src.open("w", encoding="utf-8") as fh:
            replaced = 0
            for line in lines:
                if line in mapping:
                    fh.write(f"{mapping[line]}\n")
                    replaced += 1
                else:
                    fh.write(f"{line}\n")
        print(f"Updated {src.name}: translated {replaced} lines, kept {len(lines) - replaced} lines unchanged")
    except Exception as exc:
        logger.error("Error updating input file: {}", exc)
    return 0


def _add_common_translation_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=None, help="Source language code (mode-specific default).")
    p.add_argument("--target", default="en", help="Target language code (default: en).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translate_files.py",
        description=(
            "Unified CLI that merges vitrans.py / tkor.py / tchn.py / trans_ru.py. "
            "Each original script corresponds to a subcommand."
        ),
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    p_vi = sub.add_parser("vi", help="Vietnamese → English, batch over cwd *.txt (vitrans.py).")
    p_vi.add_argument(
        "--chunk-size",
        type=int,
        default=4800,
        help="Max characters per chunk (default: 4800).",
    )
    p_vi.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="Seconds between chunks (default: 1.2).",
    )
    p_vi.add_argument(
        "--file-delay",
        type=float,
        default=3.0,
        help="Seconds between files (default: 3.0).",
    )
    p_vi.add_argument(
        "--save-every",
        type=int,
        default=5,
        help="Save progress every N chunks (default: 5).",
    )
    p_vi.add_argument(
        "--attempts",
        type=int,
        default=5,
        help="Max translation attempts per chunk (default: 5).",
    )
    p_vi.add_argument(
        "--retry-base-delay",
        type=float,
        default=3.0,
        help="Initial retry backoff in seconds (default: 3.0).",
    )
    p_vi.add_argument(
        "--retry-max-delay",
        type=float,
        default=90.0,
        help="Max retry backoff in seconds (default: 90).",
    )
    _add_common_translation_args(p_vi)
    p_vi.set_defaults(source="vi", _handler=run_vi)
    p_ko = sub.add_parser("ko", help="Korean → English, single file (tkor.py).")
    p_ko.add_argument("input_path", help="Path to the input file.")
    p_ko.add_argument(
        "--chunk-size",
        type=int,
        default=32768,
        help="Characters per chunk (default: 32768).",
    )
    p_ko.add_argument(
        "-g",
        "--game",
        default=None,
        help="Optional game argument (accepted for compatibility; unused).",
    )
    _add_common_translation_args(p_ko)
    p_ko.set_defaults(source="ko", _handler=run_ko)
    p_zh = sub.add_parser("zh", help="Auto → English, recursive directory (tchn.py).")
    p_zh.add_argument("--root", default=".", help="Directory to walk (default: cwd).")
    p_zh.add_argument(
        "--chunk-size",
        type=int,
        default=32768,
        help="Characters per chunk (default: 32768).",
    )
    _add_common_translation_args(p_zh)
    p_zh.set_defaults(source="auto", _handler=run_zh)
    p_ru = sub.add_parser("ru", help="Russian → English, line-based single file (trans_ru.py).")
    p_ru.add_argument("input_path", help="Path to the input file.")
    p_ru.add_argument(
        "--chunk-size",
        type=int,
        default=2000,
        help="Max characters per line-group (default: 2000).",
    )
    p_ru.add_argument("--attempts", type=int, default=4, help="Max attempts per chunk (default: 4).")
    p_ru.add_argument(
        "--retry-base-delay",
        type=float,
        default=0.6,
        help="Initial backoff in seconds (default: 0.6).",
    )
    _add_common_translation_args(p_ru)
    p_ru.set_defaults(source="ru", _handler=run_ru)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args._handler(args) or 0)
    except KeyboardInterrupt:
        print("\n👋 Process stopped by user.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
