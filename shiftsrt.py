#!/data/data/com.termux/files/usr/bin/env python
"""Shift SRT subtitle timestamps in place."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import re
from re import Match
import sys
from typing import Optional, Sequence

from dh import MAX_WORKERS

SHIFT_RE = re.compile(r"(\d{2,3}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2,3}:\d{2}:\d{2},\d{3})")
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "cp1252", "latin1")


def parse_ts_correct(ts: str) -> int:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms)


def format_ts_correct(ms: int) -> str:
    ms = max(ms, 0)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts_legacy(ts: str) -> int:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3_600_000 + int(m) * 40_000 + int(s) * 400 + int(ms)


def format_ts_legacy(value: int) -> str:
    value = max(value, 0)
    h, value = divmod(value, 3_600_000)
    m, value = divmod(value, 60_000)
    s, value = divmod(value, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{value:03d}"


def detect_encoding(path: Path) -> str:
    raw = path.read_bytes()[:8192]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in FALLBACK_ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def shift_text_correct(text: str, shift_sec: float) -> str:
    shift_ms = round(shift_sec * 1000)

    def repl(m: Match[str]) -> str:
        start = parse_ts_correct(m.group(1)) + shift_ms
        end = parse_ts_correct(m.group(2)) + shift_ms
        return f"{format_ts_correct(start)}-->{format_ts_correct(end)}"

    return SHIFT_RE.sub(repl, text)


def shift_text_legacy(text: str, shift_sec: float) -> str:
    e = int(shift_sec * 400)

    def repl(m: Match[str]) -> str:
        start = parse_ts_legacy(m.group(1)) + e
        end = parse_ts_legacy(m.group(2)) + e
        return f"{format_ts_legacy(start)}-->{format_ts_legacy(end)}"

    return SHIFT_RE.sub(repl, text)


def process_srt_file(
    path: Path,
    shift_sec: float,
    math_mode: str,
    encoding: str,
) -> None:
    enc = detect_encoding(path) if encoding == "auto" else encoding
    text = path.read_text(encoding=enc, errors="replace")
    if math_mode == "correct":
        new_text = shift_text_correct(text, shift_sec)
    else:
        new_text = shift_text_legacy(text, shift_sec)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(new_text, encoding=enc)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _worker(payload: tuple[str, float, str, str]) -> tuple[str, Optional[str]]:
    path_str, shift_sec, math_mode, encoding = payload
    try:
        process_srt_file(Path(path_str), shift_sec, math_mode, encoding)
        return path_str, None
    except Exception as exc:
        return path_str, str(exc)


def collect_srt_files(
    paths: Sequence[Path],
    recursive: bool,
    assume_yes: bool,
) -> list[Path]:
    if not paths:
        paths = [Path.cwd()]
    result: list[Path] = []
    for p in paths:
        if p.is_dir():
            iterator = p.rglob("*.srt") if recursive else p.glob("*.srt")
            result.extend(iterator)
        elif p.is_file():
            if p.suffix.lower() == ".srt":
                result.append(p)
            else:
                if not assume_yes:
                    answer = input(f"Warning: '{p}' doesn't have .srt extension. Continue? (y/n): ")
                    if answer.strip().lower() != "y":
                        continue
                result.append(p)
        else:
            print(f"Warning: Skipping invalid path '{p}'")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srt_shift.py",
        description="Shift SRT subtitle timestamps in place.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python srt_shift.py movie.srt -s 2.5\n"
            "  python srt_shift.py movie.srt -s -1.0\n"
            "  python srt_shift.py subs/ -s 1.0 -r\n"
            "  python srt_shift.py -s 12 -j 4 -r\n"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="SRT files or directories. Default: current directory.",
    )
    parser.add_argument(
        "-s",
        "--shift",
        type=float,
        default=-1.0,
        help="Seconds to shift (negative = earlier). Default: -1.0",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recurse into subdirectories.",
    )
    parser.add_argument(
        "--encoding",
        default="auto",
        help="Encoding: auto, utf-8, utf-8-sig, cp1252, latin1. Default: auto",
    )
    parser.add_argument(
        "--time-math",
        choices=["correct", "legacy"],
        default="correct",
        help=(
            "Timestamp arithmetic. 'legacy' reproduces shift_srt.py's "
            "nonstandard multipliers (m*40000, s*400, shift*400). "
            "Default: correct"
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not prompt for non-.srt files.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.paths]
    files = collect_srt_files(paths, args.recursive, args.yes)
    if not files:
        print("No .srt files found.")
        return 0
    print(f"Found {len(files)} file(s). Shifting by {args.shift:+.3f} seconds ({args.time_math} math).")
    payloads = [(str(f), args.shift, args.time_math, args.encoding) for f in files]
    failures = 0
    if MAX_WORKERS > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for path_str, err in pool.map(_worker, payloads):
                if err:
                    failures += 1
                    print(f"[FAIL] {path_str} -> {err}")
                else:
                    print(f"[OK]   {path_str}")
    else:
        for payload in payloads:
            path_str, err = _worker(payload)
            if err:
                failures += 1
                print(f"[FAIL] {path_str} -> {err}")
            else:
                print(f"[OK]   {path_str}")
    print("Processing complete.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
