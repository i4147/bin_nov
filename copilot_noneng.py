#!/data/data/com.termux/files/usr/bin/env python
"""Detect the non-English lines of text files.
Runs gcld3, pycld2 and langdetect on each line, combines their votes, reports the lines that are not English in a JSON file and parallelises either by file (default) or by line.
Usage: copilot_noneng [paths ...] [--out report.json] [--parallel-mode file|line] Positional arguments: paths ...
Files or directories (default: .) Options: --out, -o Output JSON file.
--max-line-length Max chars per line.
--parallel-mode Parallelism mode: file (default) or line (inside each file).
Examples: $ copilot_noneng docs/ -o noneng.json Command-line script: run ``python copilot_noneng.py`` or the ``copilot_noneng`` command.
The module is importable as well."""

import argparse
from collections.abc import Iterable, Iterator
import json
import multiprocessing as mp
from pathlib import Path
import sys
import threading
from typing import Any, TYPE_CHECKING

from dh import MAX_WORKERS, is_binary
import gcld3
from langdetect import DetectorFactory, detect_langs
import pycld2 as cld2


DetectorFactory.seed = 0
_print_lock = threading.Lock()
_results_lock = threading.Lock()
_results: list[dict[str, Any]] = []
_gcld3_detector = gcld3.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=1000)


def iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    for p in paths:
        p = p.expanduser()
        if p.is_symlink():
            continue
        if p.is_file():
            if not is_binary(p):
                yield p
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_symlink():
                    continue
                if f.is_file() and not is_binary(f):
                    yield f


def read_text_lines(path: Path) -> Iterator[str]:
    for enc in ("utf-8", "latin-1"):
        try:
            with path.open("r", encoding=enc, errors="strict") as fh:
                for ln in fh:
                    yield ln.rstrip("\n")
            return
        except UnicodeDecodeError:
            continue
        except Exception:
            return


def detect_gcld3(text: str) -> str | None:
    try:
        res = _gcld3_detector.FindLanguage(text[:1000])
        return getattr(res, "language", None)
    except Exception:
        return None


def detect_pycld2(text: str) -> str | None:
    try:
        _, _, details = cld2.detect(text)
        return details[0][1]
    except Exception:
        return None


def detect_langdetect(text: str) -> str | None:
    try:
        langs = detect_langs(text)
        if langs:
            return langs[0].lang
    except Exception:
        return None


def normalize(code: str | None) -> str:
    return "unknown" if not code else code.lower()


def combine_votes(g3: str | None, p2: str | None, ld: str | None) -> dict[str, Any]:
    votes = {
        "gcld3": normalize(g3),
        "pycld2": normalize(p2),
        "langdetect": normalize(ld),
    }
    print(votes)

    def is_en(v: str) -> bool:
        return v.startswith("en")

    non_en_votes = sum(1 for v in votes.values() if v != "unknown" and not is_en(v))
    known_votes = sum(1 for v in votes.values() if v != "unknown")
    if non_en_votes >= 2:
        decision = True
    elif non_en_votes == 1 and known_votes == 1:
        if (votes["gcld3"] != "unknown" and not is_en(votes["gcld3"])) or (
            votes["pycld2"] != "unknown" and not is_en(votes["pycld2"])
        ):
            decision = True
        else:
            decision = False
    else:
        decision = False
    return {"votes": votes, "non_english": decision}


def detect_line(path: Path, lineno: int, line: str, max_len: int) -> dict[str, Any] | None:
    if not line.strip():
        return None
    text = line if len(line) <= max_len else line[:max_len] + "…"
    g3 = detect_gcld3(text)
    p2 = detect_pycld2(text)
    ld = detect_langdetect(text)
    combined = combine_votes(g3, p2, ld)
    if combined["non_english"]:
        rec = {
            "file": str(path),
            "line_no": lineno,
            "text": text,
            "detectors": combined["votes"],
            "non_english": True,
        }
        return rec
    return None


def process_file_sequential(path: Path, max_len: int) -> list[dict[str, Any]]:
    local: list[dict[str, Any]] = []
    for lineno, raw in enumerate(read_text_lines(path), start=1):
        rec = detect_line(path, lineno, raw, max_len)
        if rec:
            with _print_lock:
                print(f"{rec['file']}:{rec['line_no']}: {rec['text']}")
                print(f"  detectors: {rec['detectors']}")
            local.append(rec)
    return local


def process_file_per_line_parallel(path: Path, max_len: int, workers: int) -> list[dict[str, Any]]:
    local: list[dict[str, Any]] = []
    lines = list(read_text_lines(path))
    if not lines:
        return local
    with mp.Pool(processes=8) as ex:
        futures = {
            ex.apply_async(
                detect_line,
                args=(
                    path,
                    lineno,
                    line,
                    max_len,
                ),
            ): lineno
            for lineno, line in enumerate(lines, start=1)
            if line.strip()
        }
        for fut in as_completed(futures):
            try:
                rec = fut.get()
                if rec:
                    with _print_lock:
                        print(f"{rec['file']}:{rec['line_no']}: {rec['text']}")
                        print(f"  detectors: {rec['detectors']}")
                    local.append(rec)
            except Exception as e:
                with _print_lock:
                    print(f"Error in per-line task for {path}: {e}", file=sys.stderr)
    return local


def run_per_file(files: list[Path], workers: int, max_len: int) -> None:
    with mp.Pool(processes=8) as ex:
        futures = {
            ex.apply_async(
                process_file_sequential,
                args=(
                    f,
                    max_len,
                ),
            ): f
            for f in files
        }
        for fut in as_completed(futures):
            try:
                res = fut.get()
                if res:
                    with _results_lock:
                        _results.extend(res)
            except Exception as e:
                print(f"Error processing {futures[fut]}: {e}", file=sys.stderr)


def run_per_line(files: list[Path], workers: int, max_len: int) -> None:
    with mp.Pool(processes=8) as ex_files:
        futures = {
            ex_files.apply_async(
                process_file_per_line_parallel,
                args=(
                    f,
                    max_len,
                    workers,
                ),
            ): f
            for f in files
        }
        for fut in as_completed(futures):
            try:
                res = fut.get()
                if res:
                    with _results_lock:
                        _results.extend(res)
            except Exception as e:
                print(f"Error in per-line mode for {futures[fut]}: {e}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Detect non-English lines in text files.")
    parser.add_argument("paths", default=".", nargs="*", help="Files or directories (default: .)")
    parser.add_argument("--out", "-o", default="noneng.json", help="Output JSON file.")
    parser.add_argument("--max-line-length", type=int, default=2000, help="Max chars per line.")
    parser.add_argument(
        "--parallel-mode",
        choices=["file", "line"],
        default="file",
        help="Parallelism mode: file (default) or line (inside each file).",
    )
    args = parser.parse_args(argv)
    input_paths = [Path(p) for p in args.paths] if args.paths else [Path()]
    files = list(iter_files(input_paths))
    if not files:
        print("No suitable text files found.", file=sys.stderr)
        return 1
    if args.parallel_mode == "file":
        run_per_file(files, MAX_WORKERS, args.max_line_length)
    else:
        run_per_line(files, MAX_WORKERS, args.max_line_length)
    with Path(args.out).open("w", encoding="utf-8") as fh:
        json.dump(_results, fh, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(_results)} non-English lines to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
