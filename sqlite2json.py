#!/data/data/com.termux/files/usr/bin/env python
"""Convert a SQLite database to JSON."""

import argparse
import base64
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Callable

from dh import MAX_WORKERS

BLOB_BASE64: str = "__blob_base64"
BLOB_HEX: str = "__blob_hex"
BATCH_SIZE: int = 5000


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def make_blob_encoder(blob_encoding: str) -> Callable[[bytes], Any]:
    encoders: dict[str, Callable[[bytes], Any]] = {
        "base64": lambda d: {BLOB_BASE64: base64.b64encode(d).decode("ascii")},
        "hex": lambda d: {BLOB_HEX: d.hex()},
        "base64-plain": lambda d: base64.b64encode(d).decode("ascii"),
        "hex-plain": lambda d: d.hex(),
    }
    try:
        return encoders[blob_encoding]
    except KeyError:
        msg = f"Unsupported blob encoding: {blob_encoding}"
        raise ValueError(msg) from None


def encode_value(value: Any, blob_fn: Callable[[bytes], Any]) -> Any:
    if value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return blob_fn(bytes(value))
    return str(value)


def open_readonly(db_path: str, text_errors: str) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors=text_errors)
    return conn


def get_tables(conn: sqlite3.Connection, include_internal: bool = False) -> list[str]:
    sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    if not include_internal:
        sql += r" AND name NOT LIKE 'sqlite\_%' ESCAPE '\'"
    sql += " ORDER BY name"
    return [row[0] for row in conn.execute(sql)]


def write_table_array(
    conn: sqlite3.Connection,
    table: str,
    fp: Path,
    blob_fn: Callable[[bytes], Any],
    indent: int,
    ensure_ascii: bool,
) -> None:
    nl = "\n" if indent else ""
    pad1 = " " * indent
    pad2 = pad1 * 2
    separators = None if indent else (",", ":")
    cur = conn.execute(f"SELECT * FROM {quote_identifier(table)}")
    cols = [d[0] for d in cur.description]
    first = True
    while True:
        batch = cur.fetchmany(BATCH_SIZE)
        if not batch:
            break
        for row in batch:
            obj = {c: encode_value(v, blob_fn) for c, v in zip(cols, row)}
            text = json.dumps(
                obj,
                ensure_ascii=ensure_ascii,
                indent=indent or None,
                separators=separators,
            )
            if indent:
                text = text.replace("\n", "\n" + pad2)
            fp.write(("[" if first else ",") + nl + pad2 + text)
            first = False
    fp.write("[]" if first else nl + pad1 + "]")


def export_table(args: tuple) -> tuple[str, str | None]:
    db_path, table, frag_path, blob_encoding, text_errors, indent, ensure_ascii = args
    try:
        blob_fn = make_blob_encoder(blob_encoding)
        with (
            closing(open_readonly(db_path, text_errors)) as conn,
            open(frag_path, "w", encoding="utf-8", errors="replace", newline="\n") as fp,
        ):
            write_table_array(conn, table, fp, blob_fn, indent, ensure_ascii)
        return table, None
    except Exception as exc:
        with open(frag_path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write("[]")
        return table, f"Error processing table {table!r}: {exc}"


def convert_sqlite_to_json(
    db_path: Path,
    output_path: Path,
    *,
    blob_encoding: str = "base64",
    indent: int = 2,
    ensure_ascii: bool = False,
    text_errors: str = "replace",
    include_internal: bool = False,
    jobs: int = 1,
    fail_fast: bool = False,
) -> list[str]:
    make_blob_encoder(blob_encoding)
    indent = max(0, indent)
    warnings: dict[str, str] = {}
    with closing(open_readonly(str(db_path), text_errors)) as conn:
        tables = get_tables(conn, include_internal)
    output_path = output_path.resolve()
    out_dir = output_path.parent
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".sqlite2json_") as tmp:
        tmp_dir = Path(tmp)
        frags = {t: tmp_dir / f"{i}.part" for i, t in enumerate(tables)}
        tasks = [
            (
                str(db_path),
                t,
                str(frags[t]),
                blob_encoding,
                text_errors,
                indent,
                ensure_ascii,
            )
            for t in tables
        ]
        workers = min(jobs, len(tasks))
        if workers > 1:
            pool = ProcessPoolExecutor(max_workers=workers)
            try:
                futures = [pool.submit(export_table, t) for t in tasks]
                for future in as_completed(futures):
                    table, warning = future.result()
                    if warning:
                        warnings[table] = warning
                        if fail_fast:
                            raise RuntimeError(warning)
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown()
        else:
            for task in tasks:
                table, warning = export_table(task)
                if warning:
                    warnings[table] = warning
                    if fail_fast:
                        raise RuntimeError(warning)
        nl = "\n" if indent else ""
        pad1 = " " * indent
        colon = ": " if indent else ":"
        final_tmp = tmp_dir / "final.json"
        with open(final_tmp, "w", encoding="utf-8", errors="replace", newline="\n") as out:
            if not tables:
                out.write("{}")
            else:
                out.write("{" + nl)
                for i, table in enumerate(tables):
                    if i:
                        out.write("," + nl)
                    out.write(pad1 + json.dumps(table, ensure_ascii=ensure_ascii) + colon)
                    with open(frags[table], "r", encoding="utf-8", newline="\n") as frag:
                        shutil.copyfileobj(frag, out, 1 << 20)
                out.write(nl + "}")
            out.write("\n")
        os.replace(final_tmp, output_path)
    return [warnings[t] for t in tables if t in warnings]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a SQLite database to JSON.")
    parser.add_argument("database", type=Path, help="SQLite database file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON file (default: <database>.json)",
    )
    parser.add_argument(
        "--blob-encoding",
        choices=("base64", "hex", "base64-plain", "hex-plain"),
        default="base64",
        help="How to encode BLOB values (default: base64)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation, 0 for compact output (default: 2)",
    )
    parser.add_argument(
        "--ensure-ascii",
        action="store_true",
        help="Escape non-ASCII characters in JSON",
    )
    parser.add_argument(
        "--text-errors",
        choices=("replace", "ignore", "strict", "surrogateescape"),
        default="replace",
        help="How to handle invalid UTF-8 text (default: replace)",
    )
    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="Include sqlite_% internal tables",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at first table error",
    )
    args = parser.parse_args(argv)
    if not args.database.is_file():
        print(f"Error: database file not found: {args.database}", file=sys.stderr)
        return 1
    output_path = args.output or args.database.with_name(args.database.name + ".json")
    if output_path.resolve() == args.database.resolve():
        print("Error: output path must differ from the database path", file=sys.stderr)
        return 1
    try:
        warnings = convert_sqlite_to_json(
            args.database,
            output_path,
            blob_encoding=args.blob_encoding,
            indent=args.indent,
            ensure_ascii=args.ensure_ascii,
            text_errors=args.text_errors,
            include_internal=args.include_internal,
            jobs=max(1, MAX_WORKERS),
            fail_fast=args.fail_fast,
        )
    except sqlite3.Error as exc:
        print(f"SQLite error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {args.database} to {output_path}")
    if warnings:
        print("Warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
