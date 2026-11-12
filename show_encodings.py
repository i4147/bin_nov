#!/data/data/com.termux/files/usr/bin/env python
"""Print the detected encoding of a file.
Reads the raw bytes, asks chardet for a guess and falls back to charset_normalizer when the confidence is low; empty files report ``utf-8``.
Usage: show_encodings FILE Examples: $ show_encodings legacy.txt Runs as a script (``python show_encodings.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

import chardet
import charset_normalizer


def get_file_encoding() -> None:
    if len(sys.argv) < 2:
        print("Error: Please provide an input file path.", file=sys.stderr)
        print("Usage: python script.py <path_to_file>", file=sys.stderr)
        sys.exit(1)
    file_path = Path(sys.argv[1])
    if not file_path.is_file():
        print(f"Error: File '{file_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    try:
        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            print("utf-8")
            return
        chardet_result = chardet.detect(raw_bytes)
        encoding = chardet_result.get("encoding")
        confidence = chardet_result.get("confidence") or 0.0
        if not encoding or confidence < 0.5:
            normalizer_result = charset_normalizer.from_bytes(raw_bytes).best()
            if normalizer_result:
                encoding = normalizer_result.encoding
        print(encoding or "utf-8")
    except Exception as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    get_file_encoding()
