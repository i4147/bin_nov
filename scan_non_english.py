#!/data/data/com.termux/files/usr/bin/env python
"""
Recursively scan files under the current directory for non-English text (Persian/Arabic, Chinese, Japanese, Korean) using Unicode ranges.

- Skips symlinks and .git directories - Skips binary files (binaryornot) - Looks inside archives too (zip/whl/jar, tar, tar.gz/bz2/xz, .gz/.bz2/.xz), including archives nested inside archives, via fswalk - Live console output as matches are found - Writes a JSON report at the end

"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

from binaryornot.helpers import is_binary_string
from fswalk import Entry, WalkOptions, walk

# ---------------------------------------------------------------------------
# Unicode ranges for the target scripts
# ---------------------------------------------------------------------------
ARABIC_PERSIAN_RANGES: list[tuple[int, int]] = [
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
]

CJK_RANGES: list[tuple[int, int]] = [
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Extension A
]

HIRAGANA_RANGE: tuple[int, int] = (0x3040, 0x309F)
KATAKANA_RANGE: tuple[int, int] = (0x30A0, 0x30FF)

HANGUL_RANGES: list[tuple[int, int]] = [
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
]

REPORT_NAME: str = "non_english_report.json"
MAX_MEMBER_SCAN: int = 64 * 1024 * 1024  # skip absurdly large archive members


def _in_ranges(code: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= code <= end for start, end in ranges)


def detect_scripts(text: str) -> set[str]:
    """Return the set of target scripts present in *text*."""
    found: set[str] = set()
    for char in text:
        code = ord(char)
        if _in_ranges(code, ARABIC_PERSIAN_RANGES):
            found.add("Arabic/Persian")
        if (
            HIRAGANA_RANGE[0] <= code <= HIRAGANA_RANGE[1]
            or KATAKANA_RANGE[0] <= code <= KATAKANA_RANGE[1]
        ):
            found.add("Japanese")
        if _in_ranges(code, HANGUL_RANGES):
            found.add("Korean")
        if _in_ranges(code, CJK_RANGES):
            found.add("Chinese")
        if len(found) == 4:
            break
    return found


def extract_sample(text: str, max_chars: int = 80) -> str:
    """Return a short sample of characters that belong to the target scripts."""
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


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------
def process_entry(entry: Entry) -> dict | None:
    """Return a report dict if the entry contains target-script text, else None."""
    if entry.is_member and entry.size and entry.size > MAX_MEMBER_SCAN:
        print(f"[skip] {entry.path}: too large ({entry.size} bytes)", file=sys.stderr)
        return None
    try:
        with entry.open() as fh:
            head = fh.read(1024)
            if is_binary_string(head):
                return None
            data = head + fh.read()
    except Exception as exc:  # OSError, bad zip member, CRC error, ...
        print(f"[error] cannot read {entry.path}: {exc}", file=sys.stderr)
        return None

    # Decode leniently – we only care about the presence of certain code points
    text = data.decode("utf-8", errors="replace")

    scripts = detect_scripts(text)
    if not scripts:
        return None

    return {
        "path": entry.path,  # e.g. 'a.zip!/inner.tar.gz!/x.txt'
        "in_archive": entry.is_member,
        "scripts": sorted(scripts),
        "sample": extract_sample(text),
    }


def main() -> None:
    root = Path.cwd()
    report: list[dict] = []

    print(
        f"Scanning {root} … (skipping symlinks, .git and binary files; looking inside archives)\n"
    )

    # read-only: we only report, never rewrite archives
    opts = WalkOptions(writable=False)

    for entry in walk(root, opts):
        if entry.path == REPORT_NAME:  # don't re-scan our own previous report
            continue
        result = process_entry(entry)
        if result is None:
            continue

        print(f"→ {result['path']}")
        print(f"  scripts : {', '.join(result['scripts'])}")
        print(f"  sample  : {result['sample']!r}")
        print()

        report.append(result)

    out_path = root / REPORT_NAME
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(f"Done. {len(report)} file(s) contained target-script text.")
    print(f"JSON report written to: {out_path}")


if __name__ == "__main__":
    main()
