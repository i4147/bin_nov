#!/data/data/com.termux/files/usr/bin/env python
"""Remove license header blocks from source files.
Loads the known license patterns from ``/sdcard/lic``, deletes the matching blocks (with the surrounding blank lines) from the files below the current directory and reports what changed.
Usage: rmlic [paths ...] Examples: $ rmlic src/ Runs as a script (``python rmlic.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import re
from typing import Any, Iterable

from dh import cprint, fsz, get_nobinary, gsz, mpf

LIC_FILE: Path = Path("/sdcard/lic")
MIN_BLANK_LINES: int = 3


def load_patterns(lic_path: Path) -> list[str]:
    try:
        content = Path(lic_path).read_text(encoding="utf-8", errors="ignore")
        pattern_separator = "\\n(?:\\s*\\n){" + str(MIN_BLANK_LINES) + ",}"
        patterns = re.split(pattern_separator, content)
        patterns = [p.strip() for p in patterns if p.strip()]
        for pattern in patterns:
            pattern[:50].replace("\n", "\\n")
        return patterns
    except Exception as e:
        print(f"Error loading patterns from {lic_path}: {e}")
        return []


def escape_for_regex(text: str) -> str:
    escaped = re.escape(text)
    return escaped.replace("\\n", "\\s*\\n\\s*")


def remove_patterns_from_content(content: str, patterns: list[str]) -> str:
    cleaned = content
    for pattern in patterns:
        regex_pattern = escape_for_regex(pattern)
        cleaned = re.sub(regex_pattern, "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    return cleaned


def process_file(path: Path, patterns: list[str]) -> tuple:
    path = Path(path)
    path = Path(path)
    before = gsz(path)
    original_content = path.read_text(encoding="utf-8")
    cleaned_content = remove_patterns_from_content(original_content, patterns)
    if len(cleaned_content) != len(original_content):
        path.write_text(cleaned_content, encoding="utf-8")
        cprint(f"{path.name} updated", "green", end=" | ")
        ds = before - gsz(path)
        cprint(f"{fsz(ds)}")
        del before, ds, cleaned_content, original_content, path


def main() -> None:
    if not LIC_FILE.exists():
        print(f"Error: License file not found: {LIC_FILE}")
        return
    patterns = load_patterns(LIC_FILE)
    if not patterns:
        print("No patterns found. Exiting.")
        return
    print()
    cwd = Path.cwd()
    files = get_nobinary(cwd)
    if not files:
        print("No files to process.")
        return
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
