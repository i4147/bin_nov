#!/data/data/com.termux/files/usr/bin/env python
"""Split a file into fixed-size chunks.

Parses a size like 512KB/10MB/1GB and writes sequential .partNNN files next to the input, in binary multiples.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dh import atomic_write, is_binary

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kKmMgG]?[bB]?)$")

_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
}


@dataclass(frozen=True)
class SplitOptions:
    input_path: Path
    output_dir: Path
    parts: int | None
    part_size: int | None
    prefix: str
    suffix: str
    encoding: str
    dry_run: bool
    overwrite: bool


class SplitError(Exception):
    pass


def parse_size(value: str) -> int:
    match = _SIZE_RE.fullmatch(value.strip())
    if not match:
        msg = f"invalid size: {value!r}; use forms such as 8k, 2m, 512b, or 1048576"
        raise argparse.ArgumentTypeError(msg)
    number, unit = match.groups()
    size = int(float(number) * _UNITS[unit.lower()])
    if size <= 0:
        msg = "size must be greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return size


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        msg = "must be an integer"
        raise argparse.ArgumentTypeError(msg) from exc
    if number <= 0:
        msg = "must be greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="split-file",
        description="Split a text file into parts at line boundaries.",
    )
    parser.add_argument("input", type=Path, help="input text file")
    parser.add_argument(
        "-n",
        "--parts",
        type=positive_int,
        help="number of parts to create",
    )
    parser.add_argument(
        "-s",
        "--size",
        type=parse_size,
        help="target maximum size per part; examples: 8k, 2m, 512b",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="output directory; defaults to the input file directory",
    )
    parser.add_argument(
        "-p",
        "--prefix",
        default=None,
        help="output filename prefix; defaults to the input filename stem",
    )
    parser.add_argument(
        "-e",
        "--encoding",
        default="utf-8",
        help="text encoding used to read and write files; default: utf-8",
    )
    parser.add_argument(
        "--suffix",
        default=None,
        help="output filename extension; defaults to the input file extension",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned output files without writing them",
    )
    return parser


def validate_options(options: SplitOptions) -> None:
    if options.parts is None and options.part_size is None:
        msg = "specify either --parts/-n or --size/-s"
        raise SplitError(msg)
    if options.parts is not None and options.part_size is not None:
        msg = "use either --parts/-n or --size/-s, not both"
        raise SplitError(msg)
    if not options.input_path.is_file():
        msg = f"input file not found: {options.input_path}"
        raise SplitError(msg)
    if is_binary(options.input_path):
        msg = f"binary file detected: {options.input_path}"
        raise SplitError(msg)
    try:
        options.input_path.read_text(encoding=options.encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        msg = f"cannot decode input as {options.encoding}: {exc}"
        raise SplitError(msg) from exc


def resolve_output_dir(options: SplitOptions) -> Path:
    output_dir = options.output_dir or options.input_path.parent
    if not options.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def split_by_parts(lines: list[str], parts: int) -> list[list[str]]:
    if parts > len(lines):
        msg = f"cannot split {len(lines)} line(s) into {parts} non-empty part(s)"
        raise SplitError(msg)
    part_size, remainder = divmod(len(lines), parts)
    chunks: list[list[str]] = []
    start = 0
    for index in range(parts):
        end = start + part_size + (1 if index < remainder else 0)
        chunks.append(lines[start:end])
        start = end
    return chunks


def split_by_size(lines: list[str], max_bytes: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        line_size = len(line.encode("utf-8"))
        if line_size > max_bytes:
            msg = f"a single line is {line_size} bytes, exceeding the requested maximum part size of {max_bytes} bytes"
            raise SplitError(msg)
        if current and current_size + line_size > max_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(line)
        current_size += line_size
    if current:
        chunks.append(current)
    return chunks


def build_output_path(
    output_dir: Path,
    prefix: str,
    index: int,
    total: int,
    suffix: str,
) -> Path:
    padding = max(2, len(str(total)))
    return output_dir / f"{prefix}_{index:0{padding}d}{suffix}"


def split_file(options: SplitOptions) -> list[Path]:
    validate_options(options)
    lines = options.input_path.read_text(encoding=options.encoding).splitlines(
        keepends=True
    )
    if not lines:
        msg = "input file is empty"
        raise SplitError(msg)

    if options.parts is not None:
        chunks = split_by_parts(lines, options.parts)
    else:
        chunks = split_by_size(lines, options.part_size or 0)

    output_dir = resolve_output_dir(options)
    prefix = options.prefix or options.input_path.stem
    suffix = options.suffix if options.suffix is not None else options.input_path.suffix
    total = len(chunks)
    output_paths: list[Path] = []

    for index, chunk in enumerate(chunks, start=1):
        output_path = build_output_path(output_dir, prefix, index, total, suffix)
        if output_path.exists() and not options.overwrite and not options.dry_run:
            msg = f"output file already exists: {output_path}"
            raise SplitError(msg)
        output_paths.append(output_path)
        if options.dry_run:
            print(f"Would create: {output_path} ({len(chunk)} lines)")
            continue
        atomic_write(output_path, "".join(chunk), encoding=options.encoding)
        print(f"Created: {output_path} ({len(chunk)} lines)")

    return output_paths


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    options = SplitOptions(
        input_path=args.input,
        output_dir=args.output_dir,
        parts=args.parts,
        part_size=args.size,
        prefix=args.prefix,
        suffix=args.suffix,
        encoding=args.encoding,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    try:
        split_file(options)
    except SplitError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
