#!/data/data/com.termux/files/usr/bin/env python
"""Extract the keys or the values of a JSON file into a text file.
Flattens the nested document, writes one key (or one value) per line to ``keys.txt``/``values.txt`` or to ``-o``, sorts them with ``--sort`` and deduplicates the result.
Usage: get_keys INPUT.json [-k|-v] [-o OUT.txt] [--sort] Positional arguments: input Path to input JSON file Options: -o, --output Output text file (default: keys.txt or values.txt) -k, --keys Extract keys to keys.txt (default) -v, --values Extract values to values.txt --sort Sort output lines Examples: $ get_keys data.json --values --sort Command-line script: run ``python get_keys.py`` or the ``get_keys`` command.
The module is importable as well."""

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterator


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def flatten(data: Any, parent: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(data, dict):
        if not data:
            yield parent, data
            return
        for k, v in data.items():
            child = f"{parent}.{k}" if parent else str(k)
            yield from flatten(v, child)
    elif isinstance(data, list):
        if not data:
            yield parent, data
            return
        for i, v in enumerate(data):
            yield from flatten(v, f"{parent}[{i}]")
    else:
        yield parent, data


def format_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def write_lines(lines: list[str], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract JSON keys or values to a text file.")
    parser.add_argument("input", type=Path, help="Path to input JSON file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output text file (default: keys.txt or values.txt)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-k",
        "--keys",
        action="store_true",
        help="Extract keys to keys.txt (default)",
    )
    mode.add_argument(
        "-v",
        "--values",
        action="store_true",
        help="Extract values to values.txt",
    )
    parser.add_argument("--sort", action="store_true", help="Sort output lines")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = parse_args(argv)
    try:
        data: Any = load_json(args.input)
        flat: list[tuple[str, Any]] = list(flatten(data))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.values:
        lines: list[str] = [format_value(v) for _, v in flat]
        default_out = Path("values.txt")
        mode_name = "values"
    else:
        lines = [k for k, _ in flat]
        default_out = Path("keys.txt")
        mode_name = "keys"
    if args.sort:
        lines = sorted(lines)
    out_path: Path = args.output or default_out
    try:
        write_lines(lines, out_path)
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Extracted {len(lines)} {mode_name} from {args.input} to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
