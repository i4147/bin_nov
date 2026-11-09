#!/data/data/com.termux/files/usr/bin/env python
"""Clean and filter wordlist files."""

import argparse
from collections import defaultdict
import contextlib
import mmap
import os
from pathlib import Path
import re
import sys
import tempfile


def read_lines_dynamically(file_path: Path, mmap_threshold_mb: float) -> list[str]:
    size_bytes = file_path.stat().st_size
    threshold_bytes = mmap_threshold_mb * 1024 * 1024
    if size_bytes > threshold_bytes:
        print(f"[Info] Large file detected ({size_bytes / (1024 * 1024):.2f} MB). Using mmap...")
        with (
            file_path.open("r+b") as f,
            mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm,
        ):
            content = mm.read().decode("utf-8", errors="ignore")
            return [line.strip() for line in content.splitlines() if line.strip()]
    else:
        print("[Info] Small file detected. Using standard read...")
        with file_path.open("r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]


def process_similar(file_path: Path, mmap_threshold_mb: float, similar_out_file: Path) -> None:
    if not file_path.exists():
        print(f"Error: File '{file_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    words = read_lines_dynamically(file_path, mmap_threshold_mb)
    pattern_groups = defaultdict(list)
    for word in words:
        for idx in range(len(word)):
            pattern = word[:idx] + "*" + word[idx + 1 :]
            pattern_groups[pattern].append(word)
    similar_words: set[str] = set()
    for group in pattern_groups.values():
        if len(group) > 1:
            for word in group:
                similar_words.add(word)
    if not similar_words:
        print("No similar items found.")
        return
    clean_words = [word for word in words if word not in similar_words]
    with similar_out_file.open("a", encoding="utf-8") as sf:
        for word in sorted(similar_words):
            sf.write(word + "\n")
    with file_path.open("w", encoding="utf-8") as f:
        for word in clean_words:
            f.write(word + "\n")
    print(f"[Success] Moved {len(similar_words)} lines to {similar_out_file}")
    print(f"[Success] Updated {file_path} in-place ({len(clean_words)} lines remaining).")


def process_repeats(file_path: Path, pattern: str) -> None:
    if not file_path.exists():
        print(f"Error: File '{file_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    regex = re.compile(pattern, re.IGNORECASE)
    fd, temp_path = tempfile.mkstemp(prefix="wordlist_", suffix=".tmp")
    temp_file = Path(temp_path)
    removed_count = 0
    total_count = 0
    try:
        with (
            os.fdopen(fd, "w", encoding="utf-8", errors="ignore") as out_f,
            file_path.open("r", encoding="utf-8", errors="ignore") as in_f,
        ):
            for line in in_f:
                total_count += 1
                clean_line = line.rstrip("\n")
                if not regex.fullmatch(clean_line):
                    out_f.write(line)
                else:
                    removed_count += 1
        temp_file.replace(file_path)
        print(f"[Success] Filtered {removed_count} out of {total_count} lines.")
        print(f"[Success] Updated {file_path} in-place.")
    except Exception:
        if temp_file.exists():
            with contextlib.suppress(OSError):
                temp_file.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean and filter wordlist files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser_similar = subparsers.add_parser(
        "similar",
        help="Remove and isolate words that differ by a single character.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_similar.add_argument("input", type=Path, help="Input wordlist file")
    parser_similar.add_argument(
        "--mmap-threshold",
        type=float,
        default=5.0,
        help="File size threshold (in MB) to trigger memory-mapped reading.",
    )
    parser_similar.add_argument(
        "--out-similar",
        type=Path,
        default=Path("similar.txt"),
        help="File to append removed similar words to.",
    )
    parser_repeats = subparsers.add_parser(
        "repeats",
        help="Rapidly stream and remove words containing repeated single characters.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser_repeats.add_argument("input", type=Path, help="Input wordlist file")
    parser_repeats.add_argument(
        "--pattern",
        type=str,
        default=r"^(.)\1+$",
        help="Regex pattern to identify words to remove.",
    )
    args = parser.parse_args()
    if args.command == "similar":
        process_similar(args.input, args.mmap_threshold, args.out_similar)
    elif args.command == "repeats":
        process_repeats(args.input, args.pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
