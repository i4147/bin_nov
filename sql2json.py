#!/data/data/com.termux/files/usr/bin/env python
"""Convert SQL INSERT dump files to streaming newline-delimited JSON."""

import argparse
import codecs
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import json
from multiprocessing import Pool, freeze_support
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Sequence

from dh import MAX_WORKERS
from loguru import logger

READ_CHUNK_BYTES: int = 1024 * 1024
DEFAULT_ENCODING: str = "utf-8"
INSERT_START_RE = re.compile(
    r"""
    \bINSERT\s+(?:IGNORE\s+)?INTO\s+
    (?P<table>
        (?:`(?:``|[^`])*`|"(?:""|[^"])*"|\[[^\]]+\]|[\w$.]+)
        (?:\s*\.\s*(?:`(?:``|[^`])*`|"(?:""|[^"])*"|\[[^\]]+\]|[\w$]+))?
    )
    \s*
    (?P<columns>
        \(
            (?:
                `(?:``|[^`])*` |
                "(?:""|[^"])*" |
                \[[^\]]+\] |
                [^()]
            )*
        \)
    )?
    \s+VALUES\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)
IDENTIFIER_RE = re.compile(
    r"""
    ^\s*
    (?:
        `(?P<backtick>(?:``|[^`])*)` |
        "(?P<doublequote>(?:""|[^"])*)" |
        \[(?P<bracket>[^\]]+)\] |
        (?P<plain>[\w$]+)
    )
    \s*$
    """,
    re.VERBOSE,
)
INTEGER_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True, slots=True)
class Job:
    source: Path
    destination: Path
    overwrite: bool


@dataclass(frozen=True, slots=True)
class Result:
    source: Path
    destination: Path
    rows: int = 0
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Convert SQL INSERT dump files to streaming newline-delimited JSON using eight worker processes.")
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="SQL files or directories. Defaults to the current directory recursively.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help=("Directory for generated .jsonl files. By default, output is written beside each source SQL file."),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output file instead of skipping it.",
    )
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Input encoding, default: {DEFAULT_ENCODING}.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail a file when malformed INSERT data is found.",
    )
    return parser.parse_args()


def discover_sql_files(inputs: Sequence[Path]) -> list[Path]:
    roots = list(inputs) if inputs else [Path.cwd()]
    discovered: set[Path] = set()
    for root in roots:
        try:
            if root.is_file():
                if root.suffix.lower() == ".sql":
                    discovered.add(root.resolve())
                else:
                    logger.warning("Skipping non-SQL file: {}", root)
            elif root.is_dir():
                for path in root.rglob("*"):
                    try:
                        if path.is_file() and path.suffix.lower() == ".sql":
                            discovered.add(path.resolve())
                    except OSError as exc:
                        logger.warning("Cannot inspect {}: {}", path, exc)
            else:
                logger.warning("Input does not exist or is inaccessible: {}", root)
        except OSError as exc:
            logger.warning("Cannot inspect input {}: {}", root, exc)
    return sorted(discovered)


def output_path_for(source: Path, output_dir: Path | None) -> Path:
    filename = f"{source.stem}.jsonl"
    if output_dir is None:
        return source.with_name(filename)
    suffix = f"{source.stem}_{abs(hash(str(source.parent))) & 0xFFFFFFFF:08x}.jsonl"
    return output_dir / suffix


def normalize_identifier(token: str) -> str:
    token = token.strip()
    match = IDENTIFIER_RE.match(token)
    if not match:
        return token
    groups = match.groupdict()
    if groups["backtick"] is not None:
        return groups["backtick"].replace("``", "`")
    if groups["doublequote"] is not None:
        return groups["doublequote"].replace('""', '"')
    if groups["bracket"] is not None:
        return groups["bracket"]
    return groups["plain"] or token


def split_top_level(text: str, separator: str = ",") -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == "'" and i + 1 < len(text):
                i += 2
                continue
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == separator and depth == 0:
            result.append(text[start:i].strip())
            start = i + 1
        i += 1
    result.append(text[start:].strip())
    return result


def parse_columns(column_group: str | None) -> list[str] | None:
    if column_group is None:
        return None
    content = column_group.strip()[1:-1]
    columns = [normalize_identifier(column) for column in split_top_level(content) if column.strip()]
    return columns or None


def decode_sql_string(value: str) -> str:
    content = value[1:-1].replace("''", "'")
    replacements = {
        r"\\": "\\",
        r"\0": "\0",
        r"\b": "\b",
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\Z": "\x1a",
        r"\'": "'",
        r"\"": '"',
    }
    for escaped, replacement in replacements.items():
        content = content.replace(escaped, replacement)
    return content


def parse_value(token: str) -> Any:
    value = token.strip()
    upper = value.upper()
    if upper == "NULL":
        return None
    if upper == "TRUE":
        return True
    if upper == "FALSE":
        return False
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return decode_sql_string(value)
    if INTEGER_RE.fullmatch(value):
        try:
            return int(value)
        except ValueError:
            return value
    if FLOAT_RE.fullmatch(value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def iter_insert_statements(
    path: Path,
    encoding: str,
    chunk_bytes: int = READ_CHUNK_BYTES,
) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    statement: list[str] = []
    quote: str | None = None
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            text = decoder.decode(chunk)
            for char in text:
                statement.append(char)
                if quote is not None:
                    if char == quote:
                        quote = None
                    elif char == "\\" and quote == "'":
                        pass
                    continue
                if char in ("'", '"', "`"):
                    quote = char
                elif char == ";":
                    sql = "".join(statement)
                    statement.clear()
                    if INSERT_START_RE.search(sql):
                        yield sql
        tail = decoder.decode(b"", final=True)
        if tail:
            statement.append(tail)
    remaining = "".join(statement)
    if INSERT_START_RE.search(remaining):
        yield remaining


def extract_tuples(values_text: str) -> Iterator[str]:
    depth = 0
    quote: str | None = None
    start: int | None = None
    i = 0
    while i < len(values_text):
        char = values_text[i]
        if quote is not None:
            if char == "\\" and quote == "'" and i + 1 < len(values_text):
                i += 2
                continue
            if char == quote:
                if i + 1 < len(values_text) and values_text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield values_text[start:i]
                start = None
        i += 1


def rows_from_statement(statement: str) -> Iterator[dict[str, Any]]:
    match = INSERT_START_RE.search(statement)
    if match is None:
        return
    table = normalize_identifier(match.group("table").split(".")[-1])
    columns = parse_columns(match.group("columns"))
    values_text = statement[match.end() :]
    for tuple_text in extract_tuples(values_text):
        values = [parse_value(item) for item in split_top_level(tuple_text)]
        if columns is None:
            row: dict[str, Any] = {
                "_table": table,
                "_values": values,
            }
        else:
            row = {"_table": table}
            row.update({column: values[index] if index < len(values) else None for index, column in enumerate(columns)})
            if len(values) > len(columns):
                row["_extra_values"] = values[len(columns) :]
        yield row


def convert_file(job: Job, encoding: str, strict: bool) -> Result:
    source = job.source
    destination = job.destination
    try:
        if destination.exists() and not job.overwrite:
            return Result(source, destination, error="output exists (use --overwrite)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        rows_written = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                for statement in iter_insert_statements(source, encoding):
                    try:
                        for row in rows_from_statement(statement):
                            json.dump(
                                row,
                                output,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                            output.write("\n")
                            rows_written += 1
                    except (ValueError, TypeError) as exc:
                        if strict:
                            raise
                        logger.warning("Skipping malformed INSERT in {}: {}", source, exc)
                output.flush()
                os.fsync(output.fileno())
            temp_path.replace(destination)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return Result(source, destination, rows=rows_written)
    except Exception as exc:
        return Result(source, destination, error=f"{type(exc).__name__}: {exc}")


def worker(payload: tuple[Job, str, bool]) -> Result:
    return convert_file(*payload)


def main() -> int:
    args = parse_args()
    files = discover_sql_files(args.inputs)
    if not files:
        logger.error("No .sql files found.")
        return 2
    output_dir = args.output_dir.resolve() if args.output_dir else None
    jobs = [
        Job(
            source=source,
            destination=output_path_for(source, output_dir),
            overwrite=args.overwrite,
        )
        for source in files
    ]
    payloads = ((job, args.encoding, args.strict) for job in jobs)
    chunksize = max(1, min(32, len(jobs) // (MAX_WORKERS * 4) or 1))
    failures = 0
    total_rows = 0
    with Pool(processes=MAX_WORKERS) as pool:
        for result in pool.imap_unordered(worker, payloads, chunksize=chunksize):
            if result.error is not None:
                failures += 1
                logger.error("{}: {}", result.source, result.error)
            else:
                total_rows += result.rows
                print(
                    "%s -> %s (%d rows)",
                    result.source,
                    result.destination,
                    result.rows,
                )
    print(
        "Completed: %d file(s), %d row(s), %d failure(s).",
        len(files),
        total_rows,
        failures,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
