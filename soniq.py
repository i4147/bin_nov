#!/data/data/com.termux/files/usr/bin/env python
"""Sort a text file and remove the duplicate lines.
Reads the file (optionally only the ``START_LINE``..``END_LINE`` range), sorts and deduplicates the lines, using an mmap and chunked processing for large files, and writes the result back in place.
``--quiet`` suppresses the progress output.
Usage: soniq FILE [START_LINE END_LINE] [--quiet] Examples: $ soniq words.txt $ soniq dump.txt 100 2000 -q Runs as a script (``python soniq.py``) in the directory it should act on; the module can also be imported."""

import mmap
from multiprocessing import Pool, cpu_count
from pathlib import Path
import sys

from dh import is_binary


THRESHOLD = 1024 * 1024


def _process_chunk(chunk: list[str]) -> list[str]:
    return [line.strip() for line in chunk if line.strip()]


def read_lines(path: Path) -> list[str]:
    sz = path.stat().st_size
    try:
        if sz > THRESHOLD:
            with open(path, encoding="utf-8", errors="ignore") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    data = mm.read().decode("utf-8", "ignore")
                    return data.splitlines()
        else:
            return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except (UnicodeDecodeError, ValueError) as e:
        print(f"Warning: Could not read file as text: {e}")
        return []


def sort_uniq(path: Path, start: int | None = None, end: int | None = None) -> tuple[int, list[str]]:
    lines = read_lines(path)
    original_count = len(lines)
    if not original_count:
        return (0, [])
    if start is not None and end is not None:
        start_idx = max(0, start - 1)
        end_idx = min(original_count, end)
        if start_idx >= original_count or start_idx >= end_idx:
            return (0, [])
        head = lines[:start_idx]
        target_lines = lines[start_idx:end_idx]
        tail = lines[end_idx:]
    else:
        head = []
        target_lines = lines
        tail = []
    if len(target_lines) > 1000:
        chunk_size = max(1, len(target_lines) // cpu_count())
        chunks = [target_lines[i : i + chunk_size] for i in range(0, len(target_lines), chunk_size)]
        with Pool(processes=min(cpu_count(), len(chunks))) as pool:
            processed_chunks = pool.map(_process_chunk, chunks)
        all_lines = [line for chunk in processed_chunks for line in chunk]
    else:
        all_lines = [line.strip() for line in target_lines if line.strip()]
    seen = set()
    duplicates = set()
    for line in all_lines:
        if line in seen:
            duplicates.add(line)
        else:
            seen.add(line)
    unique_sorted = sorted(seen)
    lines_removed = len(target_lines) - len(unique_sorted)
    final_lines = head + unique_sorted + tail
    if lines_removed > 0 or all_lines != unique_sorted:
        path.write_text("\n".join(final_lines) + ("\n" if final_lines else ""), encoding="utf-8")
    return (lines_removed, list(duplicates))


if __name__ == "__main__":
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    pos_args = [a for a in args if not a.startswith("-")]
    if not pos_args:
        print("Usage: python sort_uniq_mp.py <filename> [start_line] [end_line] [--quiet|-q]")
        print("  [start_line] [end_line] : Optional line numbers range (1-based index) to sort & uniq")
        print("  --quiet, -q             : Only show count, not the actual duplicate lines")
        sys.exit(1)
    filename_arg = pos_args[0]
    start_line = None
    end_line = None
    if len(pos_args) >= 3:
        try:
            start_line = int(pos_args[1])
            end_line = int(pos_args[2])
            if start_line < 1 or end_line < start_line:
                print("Error: Invalid line range. start_line must be >= 1 and end_line >= start_line.")
                sys.exit(1)
        except ValueError:
            print("Error: Start and end line parameters must be integers.")
            sys.exit(1)
    elif len(pos_args) == 2:
        print("Error: Both start_line and end_line must be provided for line range mode.")
        sys.exit(1)
    path = Path(filename_arg)
    if not path.exists():
        print(f"Error: File not found: {path}")
        sys.exit(1)
    if path.is_dir():
        print(f"Error: {path} is a directory, not a file")
        sys.exit(1)
    if is_binary(path):
        print(f"Skipping binary file: {path.name}")
        sys.exit(0)
    try:
        removed, duplicates = sort_uniq(path, start_line, end_line)
        range_str = f" in lines {start_line}-{end_line}" if start_line and end_line else ""
        if removed > 0:
            print(f"\n✓ {removed} duplicate{('s' if removed != 1 else '')} removed{range_str} and file sorted")
            if not quiet and duplicates:
                print("\nDuplicate lines removed:")
                sorted_dupes = sorted(duplicates)
                for line in sorted_dupes[:50]:
                    print(f"  {line}")
                if len(sorted_dupes) > 50:
                    print(f"... ({len(sorted_dupes) - 50} more duplicate lines not shown)")
            elif quiet:
                print("  (Use without --quiet to see the actual duplicate lines)")
        else:
            print(f"✓ File region sorted (all lines were unique){range_str}")
    except Exception as e:
        print(f"Error processing file: {e}")
        sys.exit(1)
