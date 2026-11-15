#!/data/data/com.termux/files/usr/bin/env python
"""sqlite2json — dump a SQLite database to JSON.

A single-file, dependency-free CLI tool for Linux and Python 3.12+.

Examples
--------
    sqlite2json app.db                          # pretty JSON on stdout
    sqlite2json app.db -o app.json              # write to a file
    sqlite2json app.db --indent 0               # compact, one line
    sqlite2json app.db -t users,orders          # only these tables
    sqlite2json app.db -x logs,events           # everything but these
    sqlite2json app.db --include-views          # views as well as tables
    sqlite2json app.db --include-schema         # {"schema": {...}, "data": {...}}
    sqlite2json app.db --list-tables            # just print table names
    sqlite2json app.db --blobs hex --nan string

Output layout (default)
-----------------------
    {
      "table_a": [ {"col": 1}, {"col": 2} ],
      "table_b": [ ... ]
    }

With --include-schema:
    {
      "schema": { "table_a": {"columns": [...], "sql": "CREATE TABLE ..."} },
      "data":   { "table_a": [ ... ] }
    }

Notes
-----
* The database is opened read-only (falls back to read/write if the read-only
  handle cannot be established, e.g. for WAL files needing recovery).  The
  database is never modified.
* BLOB columns are base64-encoded by default (see --blobs).
* Rows are streamed, so memory use stays flat for very large tables.
* Internal ``sqlite_*`` tables are skipped unless --include-internal is given.
"""

from __future__ import annotations
import argparse
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Iterator, TextIO


__version__ = "1.0.0"
PROG = "sqlite2json"

BLOB_MODES = ("base64", "hex", "null", "error")
NAN_MODES = ("null", "string", "error")
INTERNAL_PREFIX = "sqlite_"

Row = dict[str, Any]
TableStream = tuple[str, Iterator[Row]]


# --------------------------------------------------------------------------- #
# errors / helpers
# --------------------------------------------------------------------------- #
class ExportError(Exception):
    """A user-facing problem (bad input, unsupported value, ...)."""


def quote_ident(name: str) -> str:
    """Quote an SQL identifier, doubling embedded double quotes."""
    if not isinstance(name, str) or not name:
        msg = "encountered an empty identifier"
        raise ExportError(msg)
    return '"' + name.replace('"', '""') + '"'


def convert_value(
    value: Any,
    *,
    blobs: str = "base64",
    nan: str = "null",
    context: str = "",
) -> Any:
    """Convert one SQLite value into something json.dumps can serialise."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if blobs == "base64":
            return base64.b64encode(raw).decode("ascii")
        if blobs == "hex":
            return raw.hex()
        if blobs == "null":
            return None
        msg = f"{context or 'value'}: binary (BLOB) data found; use --blobs base64|hex|null"
        raise ExportError(msg)
    if isinstance(value, float) and not math.isfinite(value):
        if nan == "null":
            return None
        if nan == "string":
            if math.isnan(value):
                return "NaN"
            return "Infinity" if value > 0 else "-Infinity"
        msg = f"{context or 'value'}: non-finite float {value!r}; use --nan null|string"
        raise ExportError(msg)
    return value


# --------------------------------------------------------------------------- #
# database access
# --------------------------------------------------------------------------- #
def open_database(path: str | Path) -> sqlite3.Connection:
    """Open *path* read-only and validate that it really is a SQLite DB."""
    p = Path(path)
    if not p.exists():
        msg = f"database not found: {p}"
        raise ExportError(msg)
    if not p.is_file():
        msg = f"not a regular file: {p}"
        raise ExportError(msg)

    resolved = p.resolve()
    attempts = (
        (resolved.as_uri() + "?mode=ro", {"uri": True}),
        (resolved.as_uri() + "?mode=rw", {"uri": True}),
    )
    last_error: Exception | None = None
    for uri, kwargs in attempts:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, **kwargs)
            conn.execute("PRAGMA schema_version").fetchone()  # force validation
            return conn
        except sqlite3.DatabaseError as exc:  # not a db / unreadable
            last_error = exc
            if conn is not None:
                conn.close()

    msg = f"cannot open {p}: {last_error}"
    raise ExportError(msg) from last_error


def list_tables(
    conn: sqlite3.Connection,
    *,
    include_views: bool = False,
    include_internal: bool = False,
) -> list[str]:
    """Return table (and optionally view) names, alphabetically sorted."""
    kinds = ["table"] + (["view"] if include_views else [])
    placeholders = ",".join("?" * len(kinds))
    query = f"SELECT name FROM sqlite_master WHERE type IN ({placeholders}) ORDER BY name"
    names = [row[0] for row in conn.execute(query, kinds)]
    if not include_internal:
        names = [n for n in names if not n.lower().startswith(INTERNAL_PREFIX)]
    return names


def select_tables(available: list[str], include: list[str], exclude: list[str]) -> list[str]:
    """Apply --tables / --exclude-tables to the list of available tables."""
    if include:
        wanted = set(include)
        missing = sorted(wanted - set(available))
        if missing:
            raise ExportError("unknown table(s): " + ", ".join(missing) + " (use --include-views to export views)")
        selected = [t for t in available if t in wanted]
    else:
        selected = list(available)

    excluded = set(exclude)
    return [t for t in selected if t not in excluded]


def table_schema(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    """Return column metadata plus the original CREATE statement for *table*."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    columns = [
        {
            "name": cid_name[1],
            "type": cid_name[2],
            "notnull": bool(cid_name[3]),
            "default": cid_name[4],
            "pk": bool(cid_name[5]),
        }
        for cid_name in conn.execute(f"PRAGMA table_info({quote_ident(table)})")
    ]
    return {"columns": columns, "sql": row[0] if row else None}


def iter_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    batch_size: int = 1000,
    blobs: str = "base64",
    nan: str = "null",
) -> Iterator[Row]:
    """Yield rows of *table* as dicts, fetching in batches of *batch_size*."""
    cursor = conn.execute(f"SELECT * FROM {quote_ident(table)}")
    columns = [d[0] for d in (cursor.description or ())]
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return
        for row in batch:
            yield {
                column: convert_value(
                    value,
                    blobs=blobs,
                    nan=nan,
                    context=f"{table}.{column}",
                )
                for column, value in zip(columns, row)
            }


# --------------------------------------------------------------------------- #
# JSON writing (streaming)
# --------------------------------------------------------------------------- #
def _j(value: Any, indent: int | None = None) -> str:
    """json.dumps with our defaults (unicode kept as-is)."""
    if indent:
        return json.dumps(value, ensure_ascii=False, indent=indent)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _indent_block(text: str, prefix: str) -> str:
    return text.replace("\n", "\n" + prefix)


def _write_tables_object(
    out: TextIO,
    tables: Iterable[TableStream],
    indent: int,
    level: int,
) -> None:
    """Write ``{"table": [rows...], ...}`` at nesting *level*, inline."""
    write = out.write

    if not indent:  # compact
        write("{")
        first_table = True
        for name, rows in tables:
            write("," if not first_table else "")
            first_table = False
            write(_j(name))
            write(":[")
            first_row = True
            for row in rows:
                write("," if not first_row else "")
                first_row = False
                write(_j(row))
            write("]")
        write("}")
        return

    pad = " " * (indent * (level + 1))
    row_pad = pad + " " * indent
    write("{")
    first_table = True
    for name, rows in tables:
        write("," if not first_table else "")
        first_table = False
        write("\n" + pad + _j(name) + ": [")
        first_row = True
        for row in rows:
            write("," if not first_row else "")
            first_row = False
            write("\n" + row_pad + _indent_block(_j(row, indent), row_pad))
        if not first_row:
            write("\n" + pad)
        write("]")
    if not first_table:
        write("\n" + " " * (indent * level))
    write("}")


def write_document(
    out: TextIO,
    tables: Iterable[TableStream],
    *,
    schema: dict[str, Any] | None = None,
    indent: int = 2,
) -> None:
    """Serialise the whole export document to *out*."""
    if schema is None:
        _write_tables_object(out, tables, indent, 0)
        out.write("\n")
        return

    if not indent:
        out.write('{"schema":')
        out.write(_j(schema))
        out.write(',"data":')
        _write_tables_object(out, tables, 0, 0)
        out.write("}\n")
        return

    pad = " " * indent
    out.write("{\n")
    out.write(pad + '"schema": ' + _indent_block(_j(schema, indent), pad) + ",\n")
    out.write(pad + '"data": ')
    _write_tables_object(out, tables, indent, 1)
    out.write("\n}\n")


# --------------------------------------------------------------------------- #
# output plumbing
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def open_output(path: str) -> Iterator[TextIO]:
    """Yield a text stream for *path* ('-' means stdout, which is not closed)."""
    if path == "-":
        try:
            yield sys.stdout
        finally:
            with contextlib.suppress(BrokenPipeError, ValueError):
                sys.stdout.flush()
        return

    handle = open(path, "w", encoding="utf-8", newline="\n")
    try:
        yield handle
    finally:
        handle.close()


def _split_names(values: list[str] | None) -> list[str]:
    """Flatten repeated/comma-separated CLI values into a list of names."""
    names: list[str] = []
    for value in values or ():
        names.extend(part.strip() for part in value.split(",") if part.strip())
    return names


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Convert a SQLite database to JSON.",
        epilog=(
            "examples:\n"
            "  %(prog)s app.db\n"
            "  %(prog)s app.db -o app.json --indent 0\n"
            "  %(prog)s app.db -t users,orders --include-schema\n"
            "  %(prog)s app.db --list-tables --include-views\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("database", help="path to the .sqlite/.db file")
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        metavar="PATH",
        help="output file, '-' for stdout (default: %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--tables",
        action="append",
        metavar="NAMES",
        help="comma separated tables to export (repeatable)",
    )
    parser.add_argument(
        "-x",
        "--exclude-tables",
        action="append",
        metavar="NAMES",
        help="comma separated tables to skip (repeatable)",
    )
    parser.add_argument(
        "--include-views",
        action="store_true",
        help="export views in addition to tables",
    )
    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="export sqlite_* internal tables (e.g. sqlite_sequence)",
    )
    parser.add_argument(
        "--include-schema",
        action="store_true",
        help='wrap output as {"schema": ..., "data": ...}',
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        metavar="N",
        help="indentation width; 0 produces a single line (default: %(default)s)",
    )
    parser.add_argument(
        "--blobs",
        choices=BLOB_MODES,
        default="base64",
        help="how to encode BLOB values (default: %(default)s)",
    )
    parser.add_argument(
        "--nan",
        dest="nan_mode",
        choices=NAN_MODES,
        default="null",
        help="how to encode NaN/Infinity floats (default: %(default)s)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="rows fetched per round-trip (default: %(default)s)",
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="print the table names that would be exported, then exit",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error messages")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.indent < 0:
        msg = "--indent must be >= 0"
        raise ExportError(msg)
    if args.batch_size < 1:
        msg = "--batch-size must be >= 1"
        raise ExportError(msg)

    conn = open_database(args.database)
    try:
        available = list_tables(
            conn,
            include_views=args.include_views,
            include_internal=args.include_internal,
        )

        if args.list_tables:
            with open_output(args.output) as out:
                for name in available:
                    out.write(name + "\n")
            return 0

        selected = select_tables(
            available,
            _split_names(args.tables),
            _split_names(args.exclude_tables),
        )
        schema = {name: table_schema(conn, name) for name in selected} if args.include_schema else None
        tables: Iterator[TableStream] = (
            (
                name,
                iter_rows(
                    conn,
                    name,
                    batch_size=args.batch_size,
                    blobs=args.blobs,
                    nan=args.nan_mode,
                ),
            )
            for name in selected
        )

        with open_output(args.output) as out:
            write_document(out, tables, schema=schema, indent=args.indent)
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ExportError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        print(f"{PROG}: database error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # e.g. `sqlite2json db.sqlite | head`
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
