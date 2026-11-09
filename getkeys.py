#!/data/data/com.termux/files/usr/bin/env python
"""Extract the keys of a JSON document into a text file.
Recurses through the objects and the lists, writes the keys one per line to ``keys.txt`` (or ``-o``) and can sort them (``-s``) and keep only the unique ones (``-u``).
Usage: getkeys INPUT.json [-o keys.txt] [-s] [-u] Positional arguments: input Path to input JSON file Options: -o, --output Output text file (default: keys.txt) -s, --sort Sort keys alphabetically before writing -u, --unique Write only unique keys Examples: $ getkeys data.json -s -u Command-line script: run ``python getkeys.py`` or the ``getkeys`` command.
The module is importable as well."""

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_keys(data: Any) -> list[str]:
    if isinstance(data, dict):
        return [str(k) for k in data]
    if isinstance(data, list):
        return [str(i) for i in range(len(data))]
    msg = f"Unsupported top-level JSON type: {type(data).__name__}"
    raise TypeError(msg)


def dedupe(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


def write_keys(keys: list[str], path: Path) -> None:
    if path.exists():
        print(f"{path.name} exists. remove it and run again.")
        return
    with path.open("w", encoding="utf-8") as f:
        for key in keys:
            f.write(key + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract JSON keys to a text file.")
    parser.add_argument("input", type=Path, help="Path to input JSON file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("keys.txt"),
        help="Output text file (default: keys.txt)",
    )
    parser.add_argument(
        "-s",
        "--sort",
        action="store_true",
        help="Sort keys alphabetically before writing",
    )
    parser.add_argument(
        "-u",
        "--unique",
        action="store_true",
        help="Write only unique keys",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = parse_args(argv)
    try:
        data: Any = load_json(args.input)
        keys: list[str] = extract_keys(data)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.unique:
        keys = dedupe(keys)
    if args.sort:
        keys = sorted(keys)
    write_keys(keys, args.output)
    print(f"Extracted {len(keys)} keys from {args.input} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
