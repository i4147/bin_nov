#!/data/data/com.termux/files/usr/bin/env python
"""Drop the untranslated entries from a translation dictionary and log them to ``failed.txt``.
Reads a JSON mapping (or a list of ``{"orig", "translated"}`` objects), keeps only the records whose translation differs from the source, rewrites the file with the kept records and appends the untranslated keys to ``failed.txt`` for a retry.
Usage: cleandic DICTIONARY.json Examples: $ cleandic words.json Runs as a script (``python cleandic.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <dic.json>")
        sys.exit(1)
    json_path = Path(sys.argv[1])
    if not json_path.is_file():
        print(f"Error: file not found: {json_path}")
        sys.exit(1)
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = [(entry["orig"], entry["translated"]) for entry in data]
    else:
        print("Error: unsupported JSON structure (expected dict or list).")
        sys.exit(1)
    good = []
    failed = []
    for orig, translated in items:
        if orig == translated:
            failed.append(orig)
        else:
            good.append((orig, translated))
    if isinstance(data, dict):
        good_data = dict(good)
    else:
        good_data = [{"orig": o, "translated": t} for o, t in good]
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(good_data, f, ensure_ascii=False, indent=2)
    print(f"Kept {len(good)} records in {json_path}")
    if not failed:
        print("No failed records — skipped creating failed.txt")
        return
    failed_path = json_path.with_name("failed.txt")
    if failed_path.exists():
        existing = failed_path.read_text(encoding="utf-8")
        with failed_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(failed))
            f.write("\n")
        print(f"Appended {len(failed)} failed records to {failed_path}")
    else:
        with failed_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(failed))
            f.write("\n")
        print(f"Wrote {len(failed)} failed records to {failed_path}")


if __name__ == "__main__":
    main()
