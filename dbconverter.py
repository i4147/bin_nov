#!/data/data/com.termux/files/usr/bin/env python
"""Convert data files between JSON, JSONL, CSV, TSV, pickle, SQLite, Parquet and Excel.
Sniffs the input format (with the SQLite header check), reads it into a table list and writes the target format inferred from ``-d`` or ``-o``, defaulting the output name to the input path with the new extension.
Usage: dbconverter INPUT -d FORMAT [-o OUTPUT] Positional arguments: input Input file path Options: -d, --dest Target extension, e.g.
csv, .json, sqlite, pkl -o, --output Output path (default: input path with new extension) Examples: $ dbconverter dump.json -d sqlite Command-line script: run ``python dbconverter.py`` or the ``dbconverter`` command.
The module is importable as well."""

import argparse
import json
from pathlib import Path
import pickle
import sqlite3
import sys
from typing import Any, Iterable

import pandas as pd


SQLITE_MAGIC: bytes = b"SQLite format 3\x00"


def is_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def detect_format(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".db", ".sqlite", ".sqlite3"):
        return "sqlite"
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext == ".json":
        return "json"
    if ext == ".csv":
        return "csv"
    if ext == ".tsv":
        return "tsv"
    if ext in (".pkl", ".pickle"):
        return "pkl"
    if ext in (".parquet", ".pq"):
        return "parquet"
    if ext in (".xlsx", ".xls"):
        return "excel"
    msg = f"Unsupported input extension: {ext!r}"
    raise ValueError(msg)


def parse_target(name: str) -> str:
    n = name.lower().lstrip(".")
    if n in ("db", "sqlite", "sqlite3"):
        return "sqlite"
    if n in ("jsonl", "ndjson"):
        return "jsonl"
    if n in ("pkl", "pickle"):
        return "pkl"
    if n in ("parquet", "pq"):
        return "parquet"
    if n in ("xlsx", "xls", "excel"):
        return "excel"
    if n in ("json", "csv", "tsv"):
        return n
    msg = f"Unsupported target format: {name!r}"
    raise ValueError(msg)


def _coerce_to_tables(obj: dict[str, Any]) -> dict[str, pd.DataFrame]:
    if isinstance(obj, pd.DataFrame):
        return {"data": obj}
    if isinstance(obj, dict):
        if obj and all(isinstance(v, pd.DataFrame) for v in obj.values()):
            return dict(obj)
        if obj and all(isinstance(v, list) for v in obj.values()):
            return {k: pd.DataFrame(v) for k, v in obj.items()}
        return {"data": pd.DataFrame([obj])}
    if isinstance(obj, list):
        return {"data": pd.DataFrame(obj)}
    return {"data": pd.DataFrame([{"value": obj}])}


def read_json(path: Path) -> dict[str, pd.DataFrame]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, list):
        return {"data": pd.DataFrame(obj)}
    if isinstance(obj, dict):
        if obj and all(isinstance(v, list) for v in obj.values()):
            return {k: pd.DataFrame(v) for k, v in obj.items()}
        return {"data": pd.DataFrame([obj])}
    msg = f"Unsupported JSON root type: {type(obj).__name__}"
    raise ValueError(msg)


def read_jsonl(path: Path) -> dict[str, pd.DataFrame]:
    return {"data": pd.read_json(path, orient="records", lines=True)}


def read_csv(path: Path) -> dict[str, pd.DataFrame]:
    return {"data": pd.read_csv(path)}


def read_tsv(path: Path) -> dict[str, pd.DataFrame]:
    return {"data": pd.read_csv(path, sep="\t")}


def read_pkl(path: Path) -> dict[str, pd.DataFrame]:
    with open(path, "rb") as f:
        return _coerce_to_tables(pickle.load(f))


def read_sqlite(path: Path) -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            con,
        )["name"].tolist()
        return {n: pd.read_sql_query(f'SELECT * FROM "{n}"', con) for n in names}
    finally:
        con.close()


def read_parquet(path: Path) -> dict[str, pd.DataFrame]:
    return {"data": pd.read_parquet(path)}


def read_excel(path: Path) -> dict[str, pd.DataFrame]:
    return dict(pd.read_excel(path, sheet_name=None))


def _single(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if len(tables) != 1:
        msg = f"this output format supports a single table, got {len(tables)}: {list(tables)}"
        raise ValueError(msg)
    return next(iter(tables.values()))


def write_json(tables: dict[str, pd.DataFrame], path: Path) -> None:
    if len(tables) == 1:
        payload = _single(tables).to_dict(orient="records")
    else:
        payload = {k: df.to_dict(orient="records") for k, df in tables.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str, ensure_ascii=False)


def write_jsonl(tables: dict[str, pd.DataFrame], path: Path) -> None:
    df = _single(tables)
    df.to_json(
        path,
        orient="records",
        lines=True,
        force_ascii=False,
        default_handler=str,
    )


def write_csv(tables: dict[str, pd.DataFrame], path: Path) -> None:
    _single(tables).to_csv(path, index=False)


def write_tsv(tables: dict[str, pd.DataFrame], path: Path) -> None:
    _single(tables).to_csv(path, index=False, sep="\t")


def write_pkl(tables: dict[str, pd.DataFrame], path: Path) -> None:
    obj = next(iter(tables.values())) if len(tables) == 1 else tables
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def write_sqlite(tables: dict[str, pd.DataFrame], path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        for name, df in tables.items():
            df.to_sql(name, con, if_exists="replace", index=False)
    finally:
        con.close()


def write_parquet(tables: dict[str, pd.DataFrame], path: Path) -> None:
    _single(tables).to_parquet(path, index=False)


def write_excel(tables: dict[str, pd.DataFrame], path: Path) -> None:
    with pd.ExcelWriter(path) as xw:
        for name, df in tables.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)


READERS: dict[str, Any] = {
    "json": read_json,
    "jsonl": read_jsonl,
    "csv": read_csv,
    "tsv": read_tsv,
    "pkl": read_pkl,
    "sqlite": read_sqlite,
    "parquet": read_parquet,
    "excel": read_excel,
}
WRITERS: dict[str, Any] = {
    "json": write_json,
    "jsonl": write_jsonl,
    "csv": write_csv,
    "tsv": write_tsv,
    "pkl": write_pkl,
    "sqlite": write_sqlite,
    "parquet": write_parquet,
    "excel": write_excel,
}
EXT_FOR: dict[str, str] = {
    "json": ".json",
    "jsonl": ".jsonl",
    "csv": ".csv",
    "tsv": ".tsv",
    "pkl": ".pkl",
    "sqlite": ".sqlite",
    "parquet": ".parquet",
    "excel": ".xlsx",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert between data container file formats.",
    )
    parser.add_argument("input", help="input file path")
    parser.add_argument(
        "-d",
        "--dest",
        required=True,
        help="target extension, e.g. csv, .json, sqlite, pkl",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="output path (default: input path with new extension)",
    )
    args = parser.parse_args(argv)
    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"input file not found: {input_path}")
    try:
        src_fmt = detect_format(input_path)
        dst_fmt = parse_target(args.dest)
    except ValueError as e:
        parser.error(str(e))
    output_path = Path(args.output) if args.output else input_path.with_suffix(EXT_FOR[dst_fmt])
    tables = READERS[src_fmt](input_path)
    WRITERS[dst_fmt](tables, output_path)
    n = len(tables)
    print(f"{input_path}  [{src_fmt}]  ->  {output_path}  [{dst_fmt}]   ({n} table{'s' if n != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
