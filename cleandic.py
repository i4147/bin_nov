#!/data/data/com.termux/files/usr/bin/env python
"""Drop translation entries whose target is missing or identical to the source.

Reads a JSON dict (or a list of {orig, translated} objects), removes every entry whose candidates are empty or equal to the original text, and writes the cleaned data back in place.
"""

from __future__ import annotations
import json
from pathlib import Path
import sys
from typing import Any, TypedDict


class EntryDict(TypedDict):
    orig: str
    translated: str | list[str]


def _normalize_candidates(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _is_failed(orig: str, value: Any) -> bool:
    candidates = _normalize_candidates(value)
    if not candidates:
        return True
    target = orig.strip().lower()
    return any(candidate.lower() == target for candidate in candidates)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <dic.json>")
        sys.exit(1)

    json_path = Path(sys.argv[1])
    if not json_path.is_file():
        print(f"Error: file not found: {json_path}")
        sys.exit(1)

    with json_path.open("r", encoding="utf-8") as f:
        data: Any = json.load(f)

    items: list[tuple[str, Any]]
    if isinstance(data, dict):
        items = [(str(k), v) for k, v in data.items()]
    elif isinstance(data, list):
        parsed_list: list[tuple[str, Any]] = []
        for entry in data:
            if (
                not isinstance(entry, dict)
                or "orig" not in entry
                or "translated" not in entry
            ):
                print(
                    "Error: list entries must be objects with 'orig' and 'translated' keys."
                )
                sys.exit(1)
            parsed_list.append((str(entry["orig"]), entry["translated"]))
        items = parsed_list
    else:
        print("Error: unsupported JSON structure (expected dict or list).")
        sys.exit(1)

    good: list[tuple[str, Any]] = []
    failed: list[str] = []

    for orig, translated in items:
        if _is_failed(orig, translated):
            failed.append(orig)
        else:
            good.append((orig, translated))

    good_data: dict[str, Any] | list[EntryDict]
    if isinstance(data, dict):
        good_data = dict(good)
    else:
        good_data = [
            {"orig": orig, "translated": translated} for orig, translated in good
        ]

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(good_data, f, ensure_ascii=False, indent=2)
        f.write("\n")

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
