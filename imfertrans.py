#!/data/data/com.termux/files/usr/bin/env python
"""Apply line-by-line translations from a sidecar JSON file to a Python file in place.

Usage:
    python apply_translations.py path/to/file.py

Expects a JSON file beside the .py file (same stem, .json extension), of the form:

    {
      "translations": {
        "original line (with indentation)": "translated line",
        ...
      }
    }

Leading indentation of the original line is preserved even if the translation
value has had its leading whitespace stripped (as in your example).
"""

import json
import sys
from pathlib import Path


def split_into_lines(text: str):
    """Split text into lines, each keeping its trailing '\\n' (if any)."""
    lines = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            lines.append(text[start : i + 1])
            start = i + 1
    if start < len(text):
        lines.append(text[start:])
    return lines


def split_line_ending(line: str):
    """Return (content, ending) where ending is '\\r\\n', '\\n', or ''."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def apply_translations(py_path: Path, json_path: Path) -> None:
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    translations = data.get("translations")
    if not isinstance(translations, dict):
        raise ValueError(f"'translations' must be an object in {json_path}")

    with py_path.open("r", encoding="utf-8", newline="") as f:
        original_text = f.read()

    lines = split_into_lines(original_text)

    new_lines = []
    translated_count = 0
    unmatched = []

    for lineno, line in enumerate(lines, start=1):
        content, ending = split_line_ending(line)

        if content not in translations:
            new_lines.append(line)
            if content.strip():  # don't report blank lines
                unmatched.append((lineno, content))
            continue

        translated = translations[content]
        if not isinstance(translated, str):
            new_lines.append(line)
            continue

        # Keep the original indentation, replace the rest.
        orig_indent = content[: len(content) - len(content.lstrip())]
        trans_indent = translated[: len(translated) - len(translated.lstrip())]

        if trans_indent == orig_indent:
            new_content = translated
        else:
            new_content = orig_indent + translated.lstrip()

        new_lines.append(new_content + ending)
        translated_count += 1

    with py_path.open("w", encoding="utf-8", newline="") as f:
        f.writelines(new_lines)

    print(f"Updated {py_path}")
    print(f"  Translated: {translated_count}/{len(lines)} lines")
    if unmatched:
        print(f"  Unmatched non-blank lines: {len(unmatched)}")
        for ln, c in unmatched[:10]:
            print(f"    line {ln}: {c!r}")
        if len(unmatched) > 10:
            print(f"    ... and {len(unmatched) - 10} more")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <python_file>", file=sys.stderr)
        return 1

    py_path = Path(sys.argv[1])
    if not py_path.is_file():
        print(f"Not a file: {py_path}", file=sys.stderr)
        return 1

    json_path = py_path.with_suffix(".json")  # e.g. foo.py -> foo.json
    if not json_path.is_file():
        print(f"Sidecar JSON not found: {json_path}", file=sys.stderr)
        return 1

    try:
        apply_translations(py_path, json_path)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
