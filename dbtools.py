#!/data/data/com.termux/files/usr/bin/env python
"""SQLite/JSON conversion toolkit.
Subcommands: ``add-files`` stores every file of the directory (text truncated, binaries 7z-compressed) in a table, ``sqlite-to-json`` dumps a database to one or per-table JSON files, ``md-to-sqlite`` parses a ruff-style rule file, ``search-rule`` looks a rule up, ``mdb-to-json`` converts Access databases through pyodbc and ``sql-to-json`` turns INSERT dumps into NDJSON.
Usage: dbtools {add-files,sqlite-to-json,md-to-sqlite,search-rule,mdb-to-json,sql-to-json} [options] Positional arguments: database code Rule code (e.g.
TRY400).
Prompts if omitted.
inputs ...
Files or directories.
Defaults to CWD recursively.
Subcommands: add-files Add every file in the current directory to a SQLite table.
sqlite-to-json Convert a SQLite database to JSON.
md-to-sqlite Parse a ruff-style Markdown file into SQLite.
search-rule Look up a Ruff rule from a SQLite database.
mdb-to-json Convert .mdb/.accdb files to JSON (requires pyodbc).
sql-to-json Convert SQL INSERT dumps to newline-delimited JSON.
Options: --db Target SQLite database.
--table Table name (default: current directory name).
--prompt Interactively prompt for the table name (add7db).
--compress Compress binary files with 7z (add7db behaviour).
--text-only Force text mode (add2db behaviour).
--max-chars Truncate text files to N characters.
--encodings Comma-separated list of encodings to try, in order.
-o, --output Output file (single mode) or directory (per-table mode).
-m, --mode Output shape: one JSON file, or one JSON per table.
--indent JSON indentation.
...
plus 12 more; run ``dbtools --help`` for the full list.
Examples: $ dbtools add-files --db files.db --table docs Command-line script: run ``python dbtools.py`` or the ``dbtools`` command.
The module is importable as well."""

import argparse
import base64
import codecs
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import io
import json
from multiprocessing import Pool, freeze_support
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import traceback
from typing import Any, Iterable, Sequence, TYPE_CHECKING

from dh import MAX_WORKERS, SQL_MAX_WORKERS
from loguru import logger

try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import pyodbc
except ImportError:
    pyodbc = None
log = logger


def _setup_logging(verbose: bool = False) -> None:
    pass


DEFAULT_TEXT_ENCODINGS: tuple[str, ...] = ("utf-8", "latin-1", "cp1252", "iso-8859-1")
DEFAULT_TEXT_CHAR_LIMIT: int = 1024 * 1024


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def _init_files_table(cursor: sqlite3.Cursor, table: str) -> None:
    cursor.execute(
        f'CREATE TABLE IF NOT EXISTS "{table}" ('
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "filename TEXT NOT NULL, "
        "file_contents BLOB, "
        "compressed BOOLEAN DEFAULT 0, "
        "original_size INTEGER DEFAULT 0, "
        "compressed_size INTEGER DEFAULT 0)"
    )


def _compress_blob(data: bytes) -> str | None:
    if py7zr is None:
        msg = "py7zr is required for --compress. Install with: pip install py7zr"
        raise RuntimeError(msg)
    try:
        buf = io.BytesIO()
        with py7zr.SevenZipFile(buf, "w") as zf:
            zf.writestr("content", data)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print(f"    Compression error: {exc}")
        return None


def _read_file(
    path: Path,
    encodings: Sequence[str],
    max_chars: int | None = None,
) -> tuple[str | None, bool]:
    for enc in encodings:
        try:
            with path.open("r", encoding=enc) as f:
                text = f.read()
            if max_chars is not None:
                text = text[:max_chars]
            return text, False
        except (UnicodeDecodeError, UnicodeError, PermissionError):
            continue
        except Exception as exc:
            return f"[Error reading file: {exc!s}]", False
    return None, True


def _collect_cwd_files(
    folder: Path,
    encodings: Sequence[str],
    compress: bool,
    max_chars: int | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_file():
            continue
        size = entry.stat().st_size
        human = f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
        print(f"  Processing: {entry.name} ({human})")
        if compress:
            text, is_binary = _read_file(entry, ("utf-8",), None)
            if is_binary:
                try:
                    data = entry.read_bytes()
                except Exception as exc:
                    items.append({
                        "filename": entry.name,
                        "contents": f"[Error reading file: {exc!s}]",
                        "compressed": 0,
                        "original_size": 0,
                        "compressed_size": 0,
                    })
                    continue
                encoded = _compress_blob(data)
                if encoded:
                    items.append({
                        "filename": entry.name,
                        "contents": encoded,
                        "compressed": 1,
                        "original_size": len(data),
                        "compressed_size": len(encoded),
                    })
                    print(f"    ✓ Compressed {len(data) / 1024:.1f}KB to {len(encoded) / 1024:.1f}KB")
                else:
                    items.append({
                        "filename": entry.name,
                        "contents": "[Binary file - compression failed]",
                        "compressed": 0,
                        "original_size": len(data),
                        "compressed_size": 0,
                    })
            else:
                size_bytes = len((text or "").encode("utf-8", errors="replace"))
                items.append({
                    "filename": entry.name,
                    "contents": text,
                    "compressed": 0,
                    "original_size": size_bytes,
                    "compressed_size": 0,
                })
                print(f"    ✓ Stored as text ({size_bytes / 1024:.1f}KB)")
        else:
            text, is_binary = _read_file(entry, encodings, max_chars)
            if is_binary:
                text = "[Binary file content not stored]"
            items.append({
                "filename": entry.name,
                "contents": text,
                "compressed": 0,
                "original_size": len(text or ""),
                "compressed_size": 0,
            })
    return items


def cmd_add_files(args: argparse.Namespace) -> int:
    folder = Path.cwd()
    default_table = folder.name
    table = args.table
    if args.prompt:
        answer = input(f"Enter folder name (default: {default_table}): ").strip()
        table = answer or default_table
    if not table:
        table = default_table
    db_path = Path(args.db)
    if not db_path.parent.exists():
        print(f"Error: directory does not exist: {db_path.parent}", file=sys.stderr)
        return 1
    encodings: tuple[str, ...] = (
        tuple(e.strip() for e in args.encodings.split(",") if e.strip()) if args.encodings else DEFAULT_TEXT_ENCODINGS
    )
    compress = args.compress and not args.text_only
    max_chars = args.max_chars
    if not compress and max_chars is None:
        max_chars = DEFAULT_TEXT_CHAR_LIMIT
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        if _table_exists(cursor, table):
            print(f"Folder name '{table}' already exists in database!")
            if args.prompt:
                answer = input("Please enter a different name: ").strip()
                table = answer or f"{table}_new"
            else:
                table = f"{table}_new"
            print(f"Using '{table}' as default")
        _init_files_table(cursor, table)
        print(f"\nScanning current directory: {folder}")
        items = _collect_cwd_files(folder, encodings, compress, max_chars)
        if not items:
            print("No files found in current directory!")
            return 0
        cursor.executemany(
            f'INSERT INTO "{table}" '
            "(filename, file_contents, compressed, original_size, compressed_size) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    it["filename"],
                    it["contents"],
                    it.get("compressed", 0),
                    it.get("original_size", 0),
                    it.get("compressed_size", 0),
                )
                for it in items
            ],
        )
        conn.commit()
    total_orig = sum(i.get("original_size", 0) for i in items)
    total_comp = sum(i.get("compressed_size", 0) for i in items)
    print(f"\n✅ Successfully added {len(items)} files to table '{table}'")
    print(f"   Total size: {total_orig / 1024 / 1024:.2f}MB")
    if total_comp:
        saved = (1 - total_comp / total_orig) * 100 if total_orig else 0
        print(f"   Compressed payload: {total_comp / 1024 / 1024:.2f}MB ({saved:.1f}% saved)")
    return 0


def _serialize_value(value: Any, blob_format: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        if blob_format == "hex":
            return value.hex()
        if blob_format == "prefixed":
            return f"<BLOB:{value.hex()}>"
        if blob_format == "decode":
            return value.decode("utf-8", errors="ignore")
        if blob_format == "ignore":
            return None
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(v, blob_format) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize_value(v, blob_format) for k, v in value.items()}
    return str(value)


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [row[0] for row in cur.fetchall()]


def _dump_single(
    db_path: Path,
    output: Path,
    indent: int | None,
    blob_format: str,
    verbose: bool,
) -> tuple[int, int]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        tables = _list_tables(conn)
        result: dict[str, list[dict[str, Any]]] = {}
        for tbl in tables:
            rows = conn.execute(f'SELECT * FROM "{tbl}"').fetchall()
            result[tbl] = [{k: _serialize_value(row[k], blob_format) for k in row} for row in rows]
    output.write_text(
        json.dumps(result, indent=indent, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(tables), sum(len(v) for v in result.values())


def _dump_per_table(
    db_path: Path,
    outdir: Path,
    indent: int | None,
    blob_format: str,
    verbose: bool,
) -> tuple[int, int]:
    outdir.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        tables = _list_tables(conn)
        for tbl in tables:
            try:
                rows = conn.execute(f'SELECT * FROM "{tbl}"').fetchall()
                data = [{k: _serialize_value(row[k], blob_format) for k in row} for row in rows]
                out = outdir / f"{tbl}.json"
                out.write_text(
                    json.dumps(data, indent=indent, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                total_rows += len(data)
                if verbose:
                    print(f"  ✅ Saved {len(data)} rows to {out.name}")
            except Exception as exc:
                if verbose:
                    print(f"  ❌ Error converting table '{tbl}': {exc}")
    return len(tables), total_rows


def cmd_sqlite_to_json(args: argparse.Namespace) -> int:
    db: Path = args.database
    if not db.is_file():
        print(f"Error: database file not found: {db}", file=sys.stderr)
        return 1
    indent = 0 if args.compact else args.indent
    blob_format = args.blob_format or ("decode" if args.mode == "per-table" else "hex")
    verbose = not args.no_verbose
    try:
        if args.mode == "single":
            output = args.output or db.with_suffix(".json")
            n_tables, n_rows = _dump_single(db, output, indent, blob_format, verbose)
            if verbose:
                print(f"✓ Converted {db} → {output} ({n_tables} tables, {n_rows} rows)")
        else:
            outdir = args.output or Path(f"{db.stem}_json")
            n_tables, n_rows = _dump_per_table(db, Path(outdir), indent, blob_format, verbose)
            if verbose:
                print(f"✓ Converted {db} → {outdir} ({n_tables} tables, {n_rows} rows)")
    except sqlite3.DatabaseError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"File I/O error: {exc}", file=sys.stderr)
        return 1
    return 0


_MD_RULE_RE = re.compile(
    r"^#\s+(.*?)\s+\((.*?)\)\s*\n(.*?)(?=\n#\s+|\Z)",
    re.DOTALL | re.MULTILINE,
)


def _md_extract_section(body: str, label: str) -> str | None:
    pattern = rf"##\s+{label}\s*\n(.*?)(?=\n##\s+|\Z)"
    match = re.search(pattern, body, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def cmd_md_to_sqlite(args: argparse.Namespace) -> int:
    md_path: Path = args.md
    db_path: Path = args.db
    if not md_path.is_file():
        print(f"Error: {md_path} not found", file=sys.stderr)
        return 1
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ruff_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "code TEXT UNIQUE, "
            "name TEXT, "
            "what_it_does TEXT, "
            "why_it_bad TEXT, "
            "example TEXT, "
            "fix_safety TEXT, "
            "options TEXT, "
            "references_list TEXT)"
        )
        text = md_path.read_text(encoding="utf-8")
        count = 0
        for name, code, body in _MD_RULE_RE.findall(text):
            cur.execute(
                "INSERT OR REPLACE INTO ruff_rules "
                "(code, name, what_it_does, why_it_bad, example, "
                " fix_safety, options, references_list) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code.strip(),
                    name.strip(),
                    _md_extract_section(body, r"What it does"),
                    _md_extract_section(body, r"Why is this bad\??"),
                    _md_extract_section(body, r"Example"),
                    _md_extract_section(body, r"Fix safety"),
                    _md_extract_section(body, r"Options"),
                    _md_extract_section(body, r"References"),
                ),
            )
            count += 1
        conn.commit()
    print(f"Success! Saved {count} rules into '{db_path}'.")
    return 0


def cmd_search_rule(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        return 1
    code = args.code
    if not code:
        code = input("Enter Ruff rule code to look up (e.g., TRY400): ")
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM ruff_rules WHERE code = ?", (code.strip().upper(),)).fetchone()
    if not row:
        print(f"❌ No rule found matching code: {code}")
        return 1
    print("-" * 40)
    print(f"📜 RULE: {row['name']} ({row['code']})")
    print("-" * 40)
    print(f"\n💡 WHAT IT DOES:\n{row['what_it_does']}")
    print(f"\n⚠️ WHY IT IS BAD:\n{row['why_it_bad']}")
    print(f"\n💻 EXAMPLE:\n{row['example']}")
    if row["fix_safety"]:
        print(f"\n🔒 FIX SAFETY:\n{row['fix_safety']}")
    if row["options"]:
        print(f"\n⚙️ OPTIONS:\n{row['options']}")
    if row["references_list"]:
        print(f"\n🔗 REFERENCES:\n{row['references_list']}")
    print("-" * 40)
    return 0


MDB_EXTS: set[str] = {".mdb", ".accdb"}
MDB_FETCH: int = 1000


def _mdb_serialize(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.hex()
    if isinstance(v, (list, tuple)):
        return [_mdb_serialize(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _mdb_serialize(x) for k, x in v.items()}
    return str(v)


def _mdb_connect(path: Path):
    if pyodbc is None:
        msg = "pyodbc is required for mdb-to-json. Install with: pip install pyodbc"
        raise RuntimeError(msg)
    src = str(path.resolve())
    drivers = [
        "Microsoft Access Driver (*.mdb, *.accdb)",
        "Microsoft Access Driver (*.mdb)",
        "MDBTools",
    ]
    last: Exception | None = None
    for drv in drivers:
        try:
            return pyodbc.connect(f"DRIVER={{{drv}}};DBQ={src};", autocommit=True, timeout=30)
        except pyodbc.Error as exc:
            last = exc
    msg = f"Could not connect to {path} with any known ODBC driver. Last error: {last}"
    raise RuntimeError(msg)


def _mdb_convert(
    src: str,
    dst: str | None = None,
    overwrite: bool = False,
    pretty: bool = False,
    tables: list[str] | None = None,
) -> tuple[str, bool, str]:
    src_path = Path(src)
    try:
        if not src_path.is_file():
            return str(src_path), False, "Not a file"
        out = Path(dst) if dst else src_path.with_suffix(".json")
        if out.exists() and not overwrite:
            return str(src_path), False, f"Output exists (use --overwrite): {out}"
        out.parent.mkdir(parents=True, exist_ok=True)
        conn = _mdb_connect(src_path)
        try:
            cur = conn.cursor()
            names: list[str] = []
            for row in cur.tables(tableType="TABLE"):
                tbl_name = row.table_name
                if tbl_name and not tbl_name.startswith("MSys"):
                    names.append(tbl_name)
            if tables:
                allowed = {t.lower() for t in tables}
                names = [n for n in names if n.lower() in allowed]
            indent = 2 if pretty else None
            with out.open("w", encoding="utf-8") as fh:
                fh.write("{\n")
                fh.write(f'  "_source": {json.dumps(str(src_path))},\n')
                fh.write(f'  "_converted_at": {json.dumps(datetime.utcnow().isoformat() + "Z")},\n')
                fh.write('  "tables": {\n')
                for idx, tbl in enumerate(names):
                    try:
                        escaped = tbl.replace("]", "]]")
                        cur.execute(f"SELECT * FROM [{escaped}]")
                        fh.write(f"    {json.dumps(tbl)}: [\n")
                        cols = [d[0] for d in cur.description] if cur.description else []
                        first = True
                        while True:
                            chunk = cur.fetchmany(MDB_FETCH)
                            if not chunk:
                                break
                            for row in chunk:
                                obj = {cols[i]: _mdb_serialize(row[i]) for i in range(len(cols))}
                                fh.write("" if first else ",\n")
                                first = False
                                fh.write("      ")
                                fh.write(json.dumps(obj, ensure_ascii=False, default=str))
                        fh.write("\n    ]")
                    except Exception as exc:
                        log.warning("Table {} in {} failed: {}", tbl, src_path, exc)
                        fh.write(f'    {json.dumps(tbl)}: {{"_error": {json.dumps(str(exc))}}}')
                    fh.write(",\n" if idx < len(names) - 1 else "\n")
                fh.write("  }\n}\n")
            kb = out.stat().st_size / 1024
            return str(src_path), True, f"Wrote {out} ({kb:.1f} KB)"
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    except Exception as exc:
        log.debug("Failure on {}:\n{}", src_path, traceback.format_exc(limit=3))
        return str(src_path), False, f"{type(exc).__name__}: {exc}"


def _mdb_collect(paths: Sequence[str]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        out.append(rp)

    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            if p.suffix.lower() in MDB_EXTS:
                add(p)
            else:
                log.warning("Skipping non-MDB file: {}", p)
        elif p.is_dir():
            for ext in MDB_EXTS:
                for f in p.rglob(f"*{ext}"):
                    if f.is_file():
                        add(f)
                for f in p.rglob(f"*{ext.upper()}"):
                    if f.is_file():
                        add(f)
        else:
            log.warning("Path does not exist: {}", p)
    return out


def cmd_mdb_to_json(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    inputs = args.inputs or ["."]
    files = _mdb_collect(inputs)
    if not files:
        log.error("No .mdb/.accdb files found in: {}", inputs)
        return 2
    print(f"Found {len(files)} MDB file(s). Using {MAX_WORKERS} workers.")
    tasks: list[tuple[str, str | None]] = []
    for f in files:
        if args.output_dir:
            outdir = Path(args.output_dir).expanduser().resolve()
            dst: str | None = str(outdir / (f.stem + ".json"))
        else:
            dst = None
        tasks.append((str(f), dst))
    ok = 0
    fail = 0
    total = len(tasks)
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
            futures = {pool.submit(_mdb_convert, s, d, args.overwrite, args.pretty, args.tables): s for s, d in tasks}
            for fut in as_completed(futures):
                src = futures[fut]
                done += 1
                try:
                    _, good, msg = fut.result()
                except Exception as exc:
                    good, msg = False, f"Unhandled: {exc}"
                if good:
                    ok += 1
                    print(f"[{done}/{total}] OK   {src} — {msg}")
                else:
                    fail += 1
                    log.error("[{}/{}] FAIL {} — {}", done, total, src, msg)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        return 130
    print(f"Done. Successes: {ok}, Failures: {fail}, Total: {total}")
    return 0 if fail == 0 else 1


SQL_CHUNK: int = 1024 * 1024
SQL_DEFAULT_ENCODING: str = "utf-8"
_SQL_INSERT_RE = re.compile(
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
_SQL_IDENT_RE = re.compile(
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
_SQL_INT_RE = re.compile(r"^[+-]?\d+$")
_SQL_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True, slots=True)
class SqlJob:
    source: Path
    destination: Path
    overwrite: bool


@dataclass(frozen=True, slots=True)
class SqlResult:
    source: Path
    destination: Path
    rows: int = 0
    error: str | None = None


def _sql_unquote_ident(s: str) -> str:
    s = s.strip()
    m = _SQL_IDENT_RE.match(s)
    if not m:
        return s
    g = m.groupdict()
    if g["backtick"] is not None:
        return g["backtick"].replace("``", "`")
    if g["doublequote"] is not None:
        return g["doublequote"].replace('""', '"')
    if g["bracket"] is not None:
        return g["bracket"]
    return g["plain"] or s


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    out: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(s):
        ch = s[i]
        if quote is not None:
            if ch == "\\" and quote == "'" and i + 1 < len(s):
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(s) and s[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == sep and depth == 0:
            out.append(s[start:i].strip())
            start = i + 1
        i += 1
    out.append(s[start:].strip())
    return out


def _parse_column_list(cols: str | None) -> list[str] | None:
    if cols is None:
        return None
    inner = cols.strip()[1:-1]
    names = [_sql_unquote_ident(c) for c in _split_top_level(inner) if c.strip()]
    return names or None


def _unescape_sql_string(s: str) -> str:
    inner = s[1:-1].replace("''", "'")
    repl = {
        "\\\\": "\\",
        "\\0": "\x00",
        "\\b": "\x08",
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
        "\\Z": "\x1a",
        "\\'": "'",
        '\\"': '"',
    }
    for k, v in repl.items():
        inner = inner.replace(k, v)
    return inner


def _parse_sql_literal(tok: str) -> Any:
    t = tok.strip()
    up = t.upper()
    if up == "NULL":
        return None
    if up == "TRUE":
        return True
    if up == "FALSE":
        return False
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return _unescape_sql_string(t)
    if _SQL_INT_RE.fullmatch(t):
        try:
            return int(t)
        except ValueError:
            return t
    if _SQL_FLOAT_RE.fullmatch(t):
        try:
            return float(t)
        except ValueError:
            return t
    return t


def _iter_insert_statements(path: Path, encoding: str) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    buf: list[str] = []
    quote: str | None = None
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(SQL_CHUNK)
            if not chunk:
                break
            for ch in decoder.decode(chunk):
                buf.append(ch)
                if quote is not None:
                    if ch == quote:
                        quote = None
                    elif ch == "\\" and quote == "'":
                        pass
                    continue
                if ch in ("'", '"', "`"):
                    quote = ch
                elif ch == ";":
                    stmt = "".join(buf)
                    buf.clear()
                    if _SQL_INSERT_RE.search(stmt):
                        yield stmt
        tail = decoder.decode(b"", final=True)
        if tail:
            buf.append(tail)
    remaining = "".join(buf)
    if _SQL_INSERT_RE.search(remaining):
        yield remaining


def _iter_tuple_bodies(body: str) -> Iterator[str]:
    depth = 0
    quote: str | None = None
    start: int | None = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote is not None:
            if ch == "\\" and quote == "'" and i + 1 < len(body):
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield body[start:i]
                start = None
        i += 1


def _parse_insert_rows(stmt: str) -> Iterator[dict[str, Any]]:
    m = _SQL_INSERT_RE.search(stmt)
    if m is None:
        return
    table = _sql_unquote_ident(m.group("table").split(".")[-1])
    cols = _parse_column_list(m.group("columns"))
    body = stmt[m.end() :]
    for raw in _iter_tuple_bodies(body):
        values = [_parse_sql_literal(x) for x in _split_top_level(raw)]
        if cols is None:
            yield {"_table": table, "_values": values}
        else:
            row: dict[str, Any] = {"_table": table}
            row.update({name: values[i] if i < len(values) else None for i, name in enumerate(cols)})
            if len(values) > len(cols):
                row["_extra_values"] = values[len(cols) :]
            yield row


def _convert_sql_file(job: SqlJob, encoding: str, strict: bool) -> SqlResult:
    src, dst = job.source, job.destination
    try:
        if dst.exists() and not job.overwrite:
            return SqlResult(src, dst, error="output exists (use --overwrite)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent, text=True)
        tmp = Path(tmp_path)
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                for stmt in _iter_insert_statements(src, encoding):
                    try:
                        for row in _parse_insert_rows(stmt):
                            json.dump(
                                row,
                                out,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                            out.write("\n")
                            count += 1
                    except (ValueError, TypeError) as exc:
                        if strict:
                            raise
                        log.warning("Skipping malformed INSERT in {}: {}", src, exc)
                out.flush()
                os.fsync(out.fileno())
            tmp.replace(dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return SqlResult(src, dst, rows=count)
    except Exception as exc:
        return SqlResult(src, dst, error=f"{type(exc).__name__}: {exc}")


def _convert_sql_file_star(args_tuple: tuple[SqlJob, str, bool]) -> SqlResult:
    return _convert_sql_file(*args_tuple)


def _collect_sql_inputs(paths: Sequence[Path]) -> list[Path]:
    roots = list(paths) if paths else [Path.cwd()]
    seen: set[Path] = set()
    for root in roots:
        try:
            if root.is_file():
                if root.suffix.lower() == ".sql":
                    seen.add(root.resolve())
                else:
                    log.warning("Skipping non-SQL file: {}", root)
            elif root.is_dir():
                for f in root.rglob("*"):
                    try:
                        if f.is_file() and f.suffix.lower() == ".sql":
                            seen.add(f.resolve())
                    except OSError as exc:
                        log.warning("Cannot inspect {}: {}", f, exc)
            else:
                log.warning("Input does not exist or is inaccessible: {}", root)
        except OSError as exc:
            log.warning("Cannot inspect input {}: {}", root, exc)
    return sorted(seen)


def _sql_output_path(src: Path, outdir: Path | None) -> Path:
    if outdir is None:
        return src.with_name(f"{src.stem}.jsonl")
    unique = f"{src.stem}_{abs(hash(str(src.parent))) & 0xFFFFFFFF:08x}.jsonl"
    return outdir / unique


def cmd_sql_to_json(args: argparse.Namespace) -> int:
    _setup_logging(False)
    files = _collect_sql_inputs(args.inputs)
    if not files:
        log.error("No .sql files found.")
        return 2
    outdir = args.output_dir.resolve() if args.output_dir else None
    jobs = [
        SqlJob(
            source=f,
            destination=_sql_output_path(f, outdir),
            overwrite=args.overwrite,
        )
        for f in files
    ]
    tasks = ((j, args.encoding, args.strict) for j in jobs)
    chunksize = max(1, min(32, len(jobs) // (SQL_MAX_WORKERS * 4) or 1))
    fails = 0
    total_rows = 0
    with Pool(processes=SQL_MAX_WORKERS) as pool:
        for res in pool.imap_unordered(_convert_sql_file_star, tasks, chunksize=chunksize):
            if res.error is not None:
                fails += 1
                log.error("{}: {}", res.source, res.error)
            else:
                total_rows += res.rows
                print(f"{res.source} -> {res.destination} ({res.rows} rows)")
    print(f"Completed: {len(files)} file(s), {total_rows} row(s), {fails} failure(s).")
    return 1 if fails else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified SQLite/JSON conversion toolkit.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ap = sub.add_parser(
        "add-files",
        help="Add every file in the current directory to a SQLite table.",
    )
    ap.add_argument("--db", default="/sdcard/pkgs.db", help="Target SQLite database.")
    ap.add_argument("--table", help="Table name (default: current directory name).")
    ap.add_argument(
        "--prompt",
        action="store_true",
        help="Interactively prompt for the table name (add7db).",
    )
    ap.add_argument(
        "--compress",
        action="store_true",
        help="Compress binary files with 7z (add7db behaviour).",
    )
    ap.add_argument("--text-only", action="store_true", help="Force text mode (add2db behaviour).")
    ap.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="Truncate text files to N characters.",
    )
    ap.add_argument("--encodings", help="Comma-separated list of encodings to try, in order.")
    ap.set_defaults(func=cmd_add_files)
    sp = sub.add_parser("sqlite-to-json", help="Convert a SQLite database to JSON.")
    sp.add_argument("database", type=Path)
    sp.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file (single mode) or directory (per-table mode).",
    )
    sp.add_argument(
        "-m",
        "--mode",
        choices=["single", "per-table"],
        default="single",
        help="Output shape: one JSON file, or one JSON per table.",
    )
    sp.add_argument("--indent", type=int, default=2, help="JSON indentation.")
    sp.add_argument("--compact", action="store_true", help="Compact JSON (no indentation).")
    sp.add_argument(
        "--blob-format",
        choices=["hex", "prefixed", "decode", "ignore"],
        default=None,
        help="How to encode BLOB columns.",
    )
    sp.add_argument("--no-verbose", action="store_true", help="Suppress progress output.")
    sp.set_defaults(func=cmd_sqlite_to_json)
    mp = sub.add_parser("md-to-sqlite", help="Parse a ruff-style Markdown file into SQLite.")
    mp.add_argument("--md", type=Path, default=Path("ruff.md"))
    mp.add_argument("--db", type=Path, default=Path("ruff_rules.db"))
    mp.set_defaults(func=cmd_md_to_sqlite)
    rp = sub.add_parser("search-rule", help="Look up a Ruff rule from a SQLite database.")
    rp.add_argument("code", nargs="?", help="Rule code (e.g. TRY400). Prompts if omitted.")
    rp.add_argument("--db", default="/sdcard/data/ruff.db")
    rp.set_defaults(func=cmd_search_rule)
    xp = sub.add_parser("mdb-to-json", help="Convert .mdb/.accdb files to JSON (requires pyodbc).")
    xp.add_argument("inputs", nargs="*", help="Files or directories. Defaults to CWD recursively.")
    xp.add_argument("-o", "--output-dir", help="Directory for generated JSON.")
    xp.add_argument("-f", "--overwrite", action="store_true")
    xp.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    xp.add_argument("-t", "--tables", nargs="+", help="Only convert the specified tables.")
    xp.add_argument("-v", "--verbose", action="store_true")
    xp.set_defaults(func=cmd_mdb_to_json)
    qp = sub.add_parser("sql-to-json", help="Convert SQL INSERT dumps to newline-delimited JSON.")
    qp.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Files or directories. Defaults to CWD recursively.",
    )
    qp.add_argument("-o", "--output-dir", type=Path)
    qp.add_argument("--overwrite", action="store_true")
    qp.add_argument("--encoding", default=SQL_DEFAULT_ENCODING)
    qp.add_argument(
        "--strict",
        action="store_true",
        help="Fail a file on the first malformed INSERT.",
    )
    qp.set_defaults(func=cmd_sql_to_json)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(False)
    return args.func(args)


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
