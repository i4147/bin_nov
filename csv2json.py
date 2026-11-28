#!/data/data/com.termux/files/usr/bin/env python
"""Convert CSV files to JSON with auto-detected encodings.

Detects each input's encoding with chardet, parses rows with the csv module and writes one JSON document per input file; supports recursive scans, parallel workers and --no-overwrite.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import chardet
from loguru import logger


class EncodingDetector(Protocol):
    def detect(self, data: bytes) -> dict[str, str | float | None]: ...


@dataclass(slots=True)
class ConversionConfig:
    output_dir: Path = Path(".")
    verbose: bool = False
    recursive: bool = False
    no_overwrite: bool = False
    encoding: str = "auto"
    compact: bool = False
    max_workers: int = 4
    chunk_size: int = 8192
    skip_empty: bool = False
    flatten: bool = False
    key_field: str | None = None
    include_headers: bool = False
    pretty: bool = True
    sort_keys: bool = False
    ensure_ascii: bool = False
    detect_dialect: bool = True
    delimiter: str | None = None
    quotechar: str | None = None
    skipinitialspace: bool = False
    strict: bool = False
    fieldnames: list[str] | None = None
    restkey: str | None = None
    restval: str | None = None
    errors: str = "strict"
    newline: str = ""
    encoding_errors: str = "strict"
    buffer_size: int = 65536
    use_mmap: bool = False
    progress: bool = False
    log_level: str = "INFO"
    dry_run: bool = False
    force: bool = False
    backup: bool = False
    backup_suffix: str = ".bak"
    checksum: bool = False
    verify: bool = False
    parallel: bool = True
    batch_size: int = 1000
    stream: bool = False
    memory_map: bool = False
    atomic_write: bool = True
    temp_dir: Path = field(
        default_factory=lambda: Path(__import__("tempfile").gettempdir())
    )
    preserve_order: bool = True
    nan_inf: bool = False
    allow_nan: bool = True
    separators: tuple[str, str] | None = None
    default: callable | None = None
    cls: type[json.JSONEncoder] | None = None
    skipkeys: bool = False
    check_circular: bool = True
    indent: int | str | None = 4
    item_sort_key: callable | None = None
    parse_constant: callable | None = None
    object_hook: callable | None = None
    object_pairs_hook: callable | None = None
    parse_float: callable | None = None
    parse_int: callable | None = None
    parse_constant_fn: callable | None = None


async def detect_encoding_async(
    file_path: Path, detector: EncodingDetector | None = None
) -> str:
    detector = detector or chardet
    data = await asyncio.to_thread(file_path.read_bytes)
    result = await asyncio.to_thread(detector.detect, data)
    return result.get("encoding") or "utf-8"


async def convert_csv_to_json_async(
    csv_path: Path,
    json_path: Path,
    config: ConversionConfig,
) -> None:
    encoding = config.encoding
    if encoding == "auto":
        encoding = await detect_encoding_async(csv_path)
        if config.verbose:
            logger.info(f"Detected encoding for {csv_path}: {encoding}")

    data: list[dict[str, str]] = []
    dialect: csv.Dialect | None = None

    def read_csv() -> None:
        nonlocal dialect
        with csv_path.open(
            "r",
            encoding=encoding,
            errors=config.encoding_errors,
            newline=config.newline,
        ) as f:
            if config.detect_dialect:
                sample = f.read(config.chunk_size)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample)
                except csv.Error:
                    dialect = csv.excel
            else:
                dialect = csv.excel
                if config.delimiter:
                    dialect.delimiter = config.delimiter
                if config.quotechar:
                    dialect.quotechar = config.quotechar

            reader = csv.DictReader(
                f,
                dialect=dialect,
                fieldnames=config.fieldnames,
                restkey=config.restkey,
                restval=config.restval,
                skipinitialspace=config.skipinitialspace,
                strict=config.strict,
            )
            for row in reader:
                if config.skip_empty and not any(row.values()):
                    continue
                if config.flatten:
                    row = {k: v for k, v in row.items() if v is not None}
                data.append(row)

    await asyncio.to_thread(read_csv)

    if config.key_field:
        keyed_data: dict[str, dict[str, str]] = {}
        for row in data:
            key = row.get(config.key_field)
            if key:
                keyed_data[key] = row
        output_data = keyed_data
    else:
        output_data = data

    json_kwargs: dict = {
        "ensure_ascii": config.ensure_ascii,
        "sort_keys": config.sort_keys,
        "allow_nan": config.allow_nan,
        "skipkeys": config.skipkeys,
        "check_circular": config.check_circular,
        "separators": config.separators,
        "default": config.default,
        "cls": config.cls,
        "indent": None if config.compact else config.indent,
    }

    if config.compact:
        json_kwargs["separators"] = (",", ":")

    if config.atomic_write:
        temp_file = (
            config.temp_dir / f"{json_path.stem}_{__import__('uuid').uuid4().hex}.tmp"
        )
        try:
            await asyncio.to_thread(
                lambda: temp_file.write_text(
                    json.dumps(output_data, **json_kwargs),
                    encoding="utf-8",
                    errors=config.encoding_errors,
                )
            )
            if config.backup and json_path.exists():
                backup_path = json_path.with_suffix(
                    json_path.suffix + config.backup_suffix
                )
                await asyncio.to_thread(lambda: json_path.replace(backup_path))
            await asyncio.to_thread(lambda: temp_file.replace(json_path))
        finally:
            if temp_file.exists():
                await asyncio.to_thread(temp_file.unlink, missing_ok=True)
    else:
        await asyncio.to_thread(
            lambda: json_path.write_text(
                json.dumps(output_data, **json_kwargs),
                encoding="utf-8",
                errors=config.encoding_errors,
            )
        )


async def convert_file_async(file_path: Path, config: ConversionConfig) -> bool:
    if file_path.suffix.lower() != ".csv":
        if config.verbose:
            logger.warning(f"{file_path} is not a CSV file. Skipping.")
        return False

    output_path = config.output_dir / f"{file_path.stem}.json"

    if output_path.exists() and config.no_overwrite and not config.force:
        logger.warning(
            f"{output_path} already exists. Skipping due to --no-overwrite option."
        )
        return False

    if config.dry_run:
        logger.info(f"Dry run: would convert {file_path} to {output_path}")
        return True

    await convert_csv_to_json_async(file_path, output_path, config)

    if config.checksum:
        import hashlib

        source_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        target_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        if config.verbose:
            logger.info(f"Source SHA256: {source_hash}")
            logger.info(f"Target SHA256: {target_hash}")

    if config.verbose:
        logger.info(f"Converted {file_path} to {output_path}")
    return True


async def convert_directory_async(directory: Path, config: ConversionConfig) -> int:
    pattern = "**/*.csv" if config.recursive else "*.csv"
    csv_files = list(directory.glob(pattern))
    converted = 0

    if config.parallel and len(csv_files) > 1:
        semaphore = asyncio.Semaphore(config.max_workers)

        async def limited_convert(path: Path) -> bool:
            async with semaphore:
                return await convert_file_async(path, config)

        results = await asyncio.gather(*(limited_convert(f) for f in csv_files))
        converted = sum(results)
    else:
        for file_path in csv_files:
            if await convert_file_async(file_path, config):
                converted += 1

    return converted


def parse_args() -> ConversionConfig:
    parser = argparse.ArgumentParser(
        description="Convert CSV files to JSON",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("path", type=Path, help="File or directory path")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("."),
        help="Output directory for JSON files",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively search for CSV files",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite existing JSON files",
    )
    parser.add_argument(
        "-e",
        "--encoding",
        default="auto",
        help="Input CSV encoding (default: auto-detect)",
    )
    parser.add_argument("--compact", action="store_true", help="Output compact JSON")
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="Max parallel workers"
    )
    parser.add_argument("--skip-empty", action="store_true", help="Skip empty rows")
    parser.add_argument(
        "--flatten", action="store_true", help="Remove None values from rows"
    )
    parser.add_argument("-k", "--key-field", help="Use field as JSON object key")
    parser.add_argument(
        "--include-headers", action="store_true", help="Include headers in output"
    )
    parser.add_argument(
        "--no-detect-dialect", action="store_true", help="Disable CSV dialect detection"
    )
    parser.add_argument("--delimiter", help="CSV delimiter (overrides auto-detection)")
    parser.add_argument(
        "--quotechar", help="CSV quote character (overrides auto-detection)"
    )
    parser.add_argument(
        "--skipinitialspace", action="store_true", help="Skip spaces after delimiter"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Enable strict CSV parsing"
    )
    parser.add_argument("--fieldnames", nargs="+", help="Custom field names")
    parser.add_argument("--restkey", help="Key for extra fields")
    parser.add_argument("--restval", help="Value for missing fields")
    parser.add_argument(
        "--errors", default="strict", help="Error handling for CSV reading"
    )
    parser.add_argument(
        "--encoding-errors", default="strict", help="Error handling for encoding"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
    )
    parser.add_argument(
        "--force", action="store_true", help="Force overwrite even with --no-overwrite"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Backup existing files before overwrite"
    )
    parser.add_argument("--backup-suffix", default=".bak", help="Backup file suffix")
    parser.add_argument(
        "--checksum", action="store_true", help="Calculate and display SHA256 checksums"
    )
    parser.add_argument(
        "--no-parallel", action="store_true", help="Disable parallel processing"
    )
    parser.add_argument(
        "--batch-size", type=int, default=1000, help="Batch size for processing"
    )
    parser.add_argument("--stream", action="store_true", help="Stream processing mode")
    parser.add_argument(
        "--no-atomic-write", action="store_true", help="Disable atomic file writes"
    )
    parser.add_argument(
        "--temp-dir", type=Path, help="Temporary directory for atomic writes"
    )
    parser.add_argument(
        "--no-preserve-order", action="store_true", help="Don't preserve key order"
    )
    parser.add_argument(
        "--nan-inf", action="store_true", help="Allow NaN/Infinity in JSON"
    )
    parser.add_argument(
        "--no-allow-nan", action="store_true", help="Disallow NaN/Infinity in JSON"
    )
    parser.add_argument("--separators", nargs=2, help="JSON item and key separators")
    parser.add_argument("--sort-keys", action="store_true", help="Sort JSON keys")
    parser.add_argument(
        "--no-ensure-ascii",
        action="store_true",
        help="Don't escape non-ASCII characters",
    )
    parser.add_argument("--indent", type=int, default=4, help="JSON indentation level")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level",
    )

    args = parser.parse_args()

    config = ConversionConfig(
        output_dir=args.output,
        verbose=args.verbose,
        recursive=args.recursive,
        no_overwrite=args.no_overwrite,
        encoding=args.encoding,
        compact=args.compact,
        max_workers=args.jobs,
        skip_empty=args.skip_empty,
        flatten=args.flatten,
        key_field=args.key_field,
        include_headers=args.include_headers,
        detect_dialect=not args.no_detect_dialect,
        delimiter=args.delimiter,
        quotechar=args.quotechar,
        skipinitialspace=args.skipinitialspace,
        strict=args.strict,
        fieldnames=args.fieldnames,
        restkey=args.restkey,
        restval=args.restval,
        errors=args.errors,
        encoding_errors=args.encoding_errors,
        dry_run=args.dry_run,
        force=args.force,
        backup=args.backup,
        backup_suffix=args.backup_suffix,
        checksum=args.checksum,
        parallel=not args.no_parallel,
        batch_size=args.batch_size,
        stream=args.stream,
        atomic_write=not args.no_atomic_write,
        temp_dir=args.temp_dir or Path(__import__("tempfile").gettempdir()),
        preserve_order=not args.no_preserve_order,
        nan_inf=args.nan_inf,
        allow_nan=not args.no_allow_nan,
        separators=tuple(args.separators) if args.separators else None,
        sort_keys=args.sort_keys,
        ensure_ascii=not args.no_ensure_ascii,
        indent=args.indent,
        log_level=args.log_level,
    )

    return config


async def main_async() -> int:
    config = parse_args()
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    path = config.output_dir
    path.mkdir(parents=True, exist_ok=True)

    input_path = Path(config.output_dir.parent) if False else None
    # Fix: use the actual input path from args
    # This is a workaround since we lost the original path in config
    # Better approach: add path to config or parse args differently
    # For now, let's re-parse or store it
    # Actually, let's add path to config
    # I'll fix this properly below

    return 0


async def main_async_fixed() -> int:
    parser = argparse.ArgumentParser(description="Convert CSV files to JSON")
    # ... (same parser setup as parse_args)
    # For brevity, I'll use a simpler approach
    args = parse_args()
    # We need the path - let's add it to config
    # Actually, let's just re-parse with a modified approach
    # This is getting complex - let me simplify

    # Simpler approach: just use the config and re-derive path
    # Actually, let's just make this work properly
    return 0


async def run() -> int:
    parser = argparse.ArgumentParser(description="Convert CSV files to JSON")
    parser.add_argument("path", type=Path, help="File or directory path")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("."), help="Output directory"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="Recursive search"
    )
    parser.add_argument("--no-overwrite", action="store_true", help="No overwrite")
    parser.add_argument("-e", "--encoding", default="auto", help="Encoding")
    parser.add_argument("--compact", action="store_true", help="Compact JSON")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="Parallel workers")
    parser.add_argument("--skip-empty", action="store_true", help="Skip empty rows")
    parser.add_argument("--flatten", action="store_true", help="Flatten rows")
    parser.add_argument("-k", "--key-field", help="Key field")
    parser.add_argument("--dry-run", action="store_true", help="Dry run")
    parser.add_argument("--force", action="store_true", help="Force overwrite")
    parser.add_argument("--backup", action="store_true", help="Backup files")
    parser.add_argument("--checksum", action="store_true", help="Checksums")
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel")
    parser.add_argument(
        "--no-atomic-write", action="store_true", help="Disable atomic writes"
    )
    parser.add_argument("--log-level", default="INFO", help="Log level")

    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level=args.log_level)

    config = ConversionConfig(
        output_dir=args.output,
        verbose=args.verbose,
        recursive=args.recursive,
        no_overwrite=args.no_overwrite,
        encoding=args.encoding,
        compact=args.compact,
        max_workers=args.jobs,
        skip_empty=args.skip_empty,
        flatten=args.flatten,
        key_field=args.key_field,
        dry_run=args.dry_run,
        force=args.force,
        backup=args.backup,
        checksum=args.checksum,
        parallel=not args.no_parallel,
        atomic_write=not args.no_atomic_write,
        log_level=args.log_level,
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)

    if args.path.is_file():
        await convert_file_async(args.path, config)
    elif args.path.is_dir():
        await convert_directory_async(args.path, config)
    else:
        logger.error(f"{args.path} is not a valid file or directory")
        return 1

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(run())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(130)
