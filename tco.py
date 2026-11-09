#!/data/data/com.termux/files/usr/bin/env python
"""Translate a text file between languages."""

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

from translate import Translator


SAVE_EVERY: int = 50
SLEEP_BETWEEN: float = 0.5
MAX_RETRIES: int = 5
BACKOFF_BASE: float = 1.5
DEFAULT_SOURCE: str = "zh"
DEFAULT_TARGET: str = "en"
MAX_CHARS: int = 450
CHUNK_SIZE: int = MAX_CHARS


def progress_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".progress")


def make_translator(source: str, target: str) -> Translator:
    try:
        return Translator(from_lang=source, to_lang=target)
    except TypeError:
        return Translator(to_lang=target)


def translate_text(translator: Translator, text: str) -> str:
    if not text.strip():
        return text
    delay = BACKOFF_BASE
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return translator.translate(text)
        except Exception as err:
            last_err = err
            print(
                f"  ! attempt {attempt}/{MAX_RETRIES} failed: {err}",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay *= BACKOFF_BASE
    msg = f"Translation failed after {MAX_RETRIES} retries: {last_err}"
    raise RuntimeError(msg)


def split_long_line(line: str, max_chars: int = MAX_CHARS) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    pieces: list[str] = []
    remaining = line
    soft_seps = ["。", "！", "？", "；", ".", "!", "?", ";", "\n"]
    hard_seps = ["，", "、", ",", ":", "：", " "]
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = -1
        for sep in soft_seps:
            pos = window.rfind(sep)
            if pos > 0:
                cut = pos + len(sep)
                break
        if cut <= 0:
            for sep in hard_seps:
                pos = window.rfind(sep)
                if pos > 0:
                    cut = pos + len(sep)
                    break
        if cut <= 0:
            cut = max_chars
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_lines(lines: list[str], max_chars: int = CHUNK_SIZE) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line)
        if current and current_len + line_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate a text file between languages.")
    parser.add_argument("input", help="Path to the input file (UTF-8).")
    parser.add_argument(
        "output",
        nargs="?",
        help="Optional output path. Defaults depend on mode (<input>.<target> for chunked, <input>.json for -l).",
    )
    parser.add_argument(
        "-s",
        "--source",
        default=DEFAULT_SOURCE,
        help=f"Source language code (default: {DEFAULT_SOURCE}).",
    )
    parser.add_argument(
        "-t",
        "--target",
        default=DEFAULT_TARGET,
        help=f"Target language code (default: {DEFAULT_TARGET}).",
    )
    parser.add_argument(
        "-l",
        "--line",
        action="store_true",
        help="Line-by-line mode: print each translation immediately and save source/target pairs to a JSON file.",
    )
    parser.add_argument(
        "-c",
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help=f"Target characters per chunk in default mode (default: {CHUNK_SIZE}, hard max: {MAX_CHARS}).",
    )
    return parser.parse_args()


def run_chunked(
    in_path: Path,
    out_path: Path,
    chunk_size: int,
    source: str,
    target: str,
) -> int:
    if chunk_size > MAX_CHARS:
        print(f"  ! --chunk-size capped at {MAX_CHARS} (backend limit)", file=sys.stderr)
        chunk_size = MAX_CHARS
    prog_path = progress_path(out_path)
    start_chunk = 0
    if prog_path.is_file():
        try:
            start_chunk = int(prog_path.read_text(encoding="utf-8").strip() or "0")
            print(f"↻ Resuming from chunk {start_chunk} (progress file found)")
        except ValueError:
            print("  ! Progress file unreadable, starting from scratch", file=sys.stderr)
            start_chunk = 0
    with in_path.open("r", encoding="utf-8") as f:
        src_lines = [ln.rstrip("\n") for ln in f]
    chunks = chunk_lines(src_lines, chunk_size)
    total = len(chunks)
    if start_chunk >= total:
        print("✔ Nothing to do — file already fully translated.")
        return 0
    translator = make_translator(source, target)
    print(f"→ {source} → {target}, {total} chunk(s), target {chunk_size} chars each")
    mode = "a" if start_chunk > 0 else "w"
    with out_path.open(mode, encoding="utf-8") as fout:
        for idx in range(start_chunk, total):
            chunk = chunks[idx]
            sub_pieces: list[str] = []
            for line in chunk:
                sub_pieces.extend(split_long_line(line, chunk_size))
            translated_parts: list[str] = []
            for piece in sub_pieces:
                preview = piece[:60].replace("\n", " ")
                print(f"[chunk {idx + 1}/{total}, {len(piece)} chars] {preview}…")
                try:
                    translated_parts.append(translate_text(translator, piece))
                except RuntimeError as err:
                    prog_path.write_text(str(idx), encoding="utf-8")
                    print(
                        f"\n✖ Aborting at chunk {idx + 1}: {err}",
                        file=sys.stderr,
                    )
                    return 2
                time.sleep(SLEEP_BETWEEN)
            translated = "\n".join(translated_parts)
            fout.write(translated)
            if not translated.endswith("\n"):
                fout.write("\n")
            fout.flush()
            if (idx + 1) % SAVE_EVERY == 0 or (idx + 1) == total:
                prog_path.write_text(str(idx + 1), encoding="utf-8")
                print(f"  ✓ saved progress at chunk {idx + 1}/{total}")
    prog_path.write_text(str(total), encoding="utf-8")
    print(f"\n✔ Done. Output written to {out_path}")
    return 0


def load_pairs(json_path: Path, src_key: str, tgt_key: str) -> list[dict[str, str]]:
    if not json_path.is_file():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            if data and not (src_key in data[0] and tgt_key in data[0]):
                print(
                    f"  ! Existing JSON uses different keys (expected '{src_key}'/'{tgt_key}'), starting fresh",
                    file=sys.stderr,
                )
                return []
            return data
    except (json.JSONDecodeError, OSError) as err:
        print(f"  ! Could not read existing JSON ({err}), starting fresh", file=sys.stderr)
    return []


def run_line_mode(
    in_path: Path,
    out_path: Path,
    source: str,
    target: str,
) -> int:
    prog_path = progress_path(out_path)
    pairs = load_pairs(out_path, source, target)
    start_line = len(pairs)
    if prog_path.is_file():
        try:
            stored = int(prog_path.read_text(encoding="utf-8").strip() or "0")
            start_line = max(start_line, stored)
        except ValueError:
            pass
    if start_line:
        print(f"↻ Resuming from line {start_line} ({len(pairs)} pairs loaded)")
    with in_path.open("r", encoding="utf-8") as f:
        src_lines = f.readlines()
    total = len(src_lines)
    if start_line >= total:
        print("✔ Nothing to do — file already fully translated.")
        return 0
    translator = make_translator(source, target)
    print(f"→ {source} → {target}, {total} line(s)")
    pairs = pairs[:start_line]
    for idx in range(start_line, total):
        raw = src_lines[idx].rstrip("\n")
        pieces = split_long_line(raw, MAX_CHARS)
        translated_pieces: list[str] = []
        for p_idx, piece in enumerate(pieces):
            try:
                translated_pieces.append(translate_text(translator, piece))
            except RuntimeError as err:
                out_path.write_text(
                    json.dumps(pairs, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                prog_path.write_text(str(idx), encoding="utf-8")
                print(
                    f"\n✖ Aborting at line {idx + 1}, piece {p_idx + 1}/{len(pieces)}: {err}",
                    file=sys.stderr,
                )
                return 2
            if len(pieces) > 1:
                print(f"  · piece {p_idx + 1}/{len(pieces)} ({len(piece)} chars) translated")
            time.sleep(SLEEP_BETWEEN)
        translated_full = " ".join(p.strip() for p in translated_pieces).strip()
        pairs.append({source: raw, target: translated_full})
        preview_src = raw if len(raw) <= 120 else raw[:117] + "…"
        preview_tgt = translated_full if len(translated_full) <= 120 else translated_full[:117] + "…"
        print(f"[{idx + 1}/{total}]")
        print(f"  {source}: {preview_src}")
        print(f"  {target}: {preview_tgt}")
        if len(pieces) > 1:
            print(f"  (line split into {len(pieces)} pieces)")
        print()
        if (idx + 1) % SAVE_EVERY == 0 or (idx + 1) == total:
            out_path.write_text(
                json.dumps(pairs, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            prog_path.write_text(str(idx + 1), encoding="utf-8")
            print(f"  ✓ saved {len(pairs)} pairs to {out_path.name}")
    out_path.write_text(
        json.dumps(pairs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prog_path.write_text(str(total), encoding="utf-8")
    print(f"✔ Done. {len(pairs)} pairs saved to {out_path}")
    return 0


def main() -> int:
    args = parse_args()
    in_path = Path(args.input).expanduser().resolve()
    if not in_path.is_file():
        print(f"Input file not found: {in_path}", file=sys.stderr)
        return 1
    source = args.source
    target = args.target
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
    elif args.line:
        out_path = in_path.with_suffix(".json")
    else:
        out_path = in_path.with_name(f"{in_path.stem}.{target}{in_path.suffix}")
    if args.line:
        return run_line_mode(in_path, out_path, source, target)
    return run_chunked(in_path, out_path, args.chunk_size, source, target)


if __name__ == "__main__":
    sys.exit(main())
