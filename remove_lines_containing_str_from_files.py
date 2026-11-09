#!/data/data/com.termux/files/usr/bin/env python
"""Delete the lines containing a string from many text files.
Removes every line that contains the given substring (the last argument; the preceding arguments are the files, defaulting to all non-binary files below the current directory) and reports the lines removed per file plus the space freed.
Work is spread over a process pool.
Usage: remove_lines_containing_str_from_files [files ...] STRING Examples: $ remove_lines_containing_str_from_files src/ 'DEBUG' Runs as a script (``python remove_lines_containing_str_from_files.py``) in the directory it should act on; the module can also be imported."""

from __future__ import annotations

from multiprocessing import Pool
from pathlib import Path
import sys
from typing import Any, Iterable

from dh import fsz, get_nobinary, gsz


def clean_text(text: str, strtofind: Iterable[Any]) -> tuple[str, int]:
    kept = []
    removed = 0
    for line in text.splitlines():
        if any(s in line for s in strtofind):
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept), removed


def clean_file(path: Path, strtofind: Any):
    try:
        original = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return path, 0, 0
    cleaned, removed = clean_text(original, strtofind)
    if cleaned != original:
        Path(path).write_text(cleaned, encoding="utf-8")
    return path, removed, len(original) - len(cleaned)


def main() -> int | None:
    args = sys.argv[1:]
    if not args:
        print(f"usage: {sys.argv[0]} [file ...] <search_string>")
        return 1
    strtofind = [args[-1]]
    file_args = args[:-1]
    files = [Path(a) for a in file_args] if file_args else get_nobinary(Path.cwd())
    root = Path.cwd()
    isz = gsz(root)
    total_removed = 0
    if len(files) == 1:
        path, removed, _ = clean_file(files[0], strtofind)
        total_removed += removed
        print(f"{path}: {removed} line(s) removed")
    else:
        pool = Pool(8)
        results = [pool.apply_async(clean_file, (f, strtofind)) for f in files]
        pool.close()
        pool.join()
        for r in results:
            path, removed, _ = r.get()
            if removed:
                print(f"{path}: {removed} line(s) removed")
            total_removed += removed
    esz = gsz(root)
    print(f"total lines removed : {total_removed}")
    print(f"space freed : {fsz(isz - esz)}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
