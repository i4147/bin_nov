#!/data/data/com.termux/files/usr/bin/env python
"""Flag files that contain Arabic/Persian, CJK, kana or Hangul characters.
Checks every text file (binaries are skipped with ``binaryornot``) against the Unicode ranges of the major non-Latin scripts and reports the files with the detected scripts plus a short sample, optionally as JSON.
Usage: noneng_unicode_range Examples: $ noneng_unicode_range Runs as a script (``python noneng_unicode_range.py``) in the directory it should act on; the module can also be imported.
"""

import json
import os
from pathlib import Path
import sys
from typing import Iterator

from binaryornot.check import is_binary

ARABIC_PERSIAN_RANGES: list[tuple[int, int]] = [
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
]

CJK_RANGES: list[tuple[int, int]] = [
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
]

HIRAGANA_RANGE: tuple[int, int] = (0x3040, 0x309F)
KATAKANA_RANGE: tuple[int, int] = (0x30A0, 0x30FF)

HANGUL_RANGES: list[tuple[int, int]] = [
    (0xAC00, 0xD7AF),
    (0x1100, 0x11FF),
    (0x3130, 0x318F),
]


def _in_ranges(code: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= code <= end for start, end in ranges)


def detect_scripts(text: str) -> set[str]:
    found: set[str] = set()
    for char in text:
        code = ord(char)
        if _in_ranges(code, ARABIC_PERSIAN_RANGES):
            found.add("Arabic/Persian")
        if HIRAGANA_RANGE[0] <= code <= HIRAGANA_RANGE[1] or KATAKANA_RANGE[0] <= code <= KATAKANA_RANGE[1]:
            found.add("Japanese")
        if _in_ranges(code, HANGUL_RANGES):
            found.add("Korean")
        if _in_ranges(code, CJK_RANGES):
            found.add("Chinese")
        if len(found) == 4:
            break
    return found


def extract_sample(text: str, max_chars: int = 80) -> str:
    sample: list[str] = []
    for char in text:
        code = ord(char)
        if (
            _in_ranges(code, ARABIC_PERSIAN_RANGES)
            or HIRAGANA_RANGE[0] <= code <= HIRAGANA_RANGE[1]
            or KATAKANA_RANGE[0] <= code <= KATAKANA_RANGE[1]
            or _in_ranges(code, HANGUL_RANGES)
            or _in_ranges(code, CJK_RANGES)
        ):
            sample.append(char)
            if len(sample) >= max_chars:
                break
    return "".join(sample)


def iter_files(root: Path) -> Iterator[Path]:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git" and not (Path(dirpath) / d).is_symlink()]

        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def process_file(path: Path, root: Path) -> dict | None:
    try:
        if is_binary(str(path)):
            return None
        data = path.read_bytes()
    except OSError as exc:
        print(f"[error] cannot read {path}: {exc}", file=sys.stderr)
        return None

    text = data.decode("utf-8", errors="replace")

    scripts = detect_scripts(text)
    if not scripts:
        return None

    rel = path.relative_to(root).as_posix()
    sample = extract_sample(text)

    result = {
        "path": rel,
        "scripts": sorted(scripts),
        "sample": sample,
    }
    return result


def main() -> None:
    root = Path.cwd()
    report: list[dict] = []

    print(f"Scanning {root} … (skipping symlinks, .git and binary files)\n")

    for path in iter_files(root):
        result = process_file(path, root)
        if result is None:
            continue

        scripts_str = ", ".join(result["scripts"])
        print(f"→ {result['path']}")
        print(f"  scripts : {scripts_str}")
        print(f"  sample  : {result['sample']!r}")
        print()

        report.append(result)

    out_path = root / "non_english_report.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(f"Done. {len(report)} file(s) contained target-script text.")
    print(f"JSON report written to: {out_path}")


if __name__ == "__main__":
    main()


"""
from archive_walker import AdvancedWalker
# … reuse the Unicode-range detection functions from the previous script …

with AdvancedWalker(".") as walker:
    for entry in walker:
        data = entry.read_bytes()
        # … detect scripts, print live, collect for JSON …
# no writes → flush is a no-op
"""
