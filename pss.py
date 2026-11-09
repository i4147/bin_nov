#!/data/data/com.termux/files/usr/bin/env python
"""Search a local PyPI package database by substring.
Uses the SQLite FTS5 trigram index at ``/sdcard/data/pip.db`` to find the package names containing the keyword and prints the best ``-n`` matches.
Usage: pss KEYWORD [-n LIMIT] Positional arguments: keyword Substring to search for (case-insensitive) Options: -n, --limit Examples: $ pss yaml -n 30 Command-line script: run ``python pss.py`` or the ``pss`` command.
The module is importable as well."""

import argparse
import heapq
from pathlib import Path
import sqlite3
import sys
from typing import Any


DB_PATH: Path = Path("/sdcard/data/pip.db")
DEFAULT_LIMIT: int = 20
TRIGRAM_MIN: int = 3


def get_table_info(con: sqlite3.Connection) -> tuple[bool,]:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in cur.fetchall()]
    if not tables:
        sys.exit("No tables found in database.")
    chosen = None
    for t in tables:
        cur = con.execute(f'PRAGMA table_info("{t}")')
        cols = cur.fetchall()
        if len(cols) == 2:
            chosen = (t, [c[1] for c in cols])
            break
    if chosen is None:
        t = tables[0]
        cur = con.execute(f'PRAGMA table_info("{t}")')
        cols = [c[1] for c in cur.fetchall()]
        if len(cols) < 2:
            sys.exit(f"Table '{t}' must have at least two columns.")
        chosen = (t, cols)
    table, col_names = chosen
    name_col, dl_col = col_names[0], col_names[1]
    cur = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    row = cur.fetchone()
    is_fts5 = bool(row and row[0] and "fts5" in row[0].lower())
    return table, name_col, dl_col, is_fts5


def search(
    con: sqlite3.Connection,
    table: str,
    name_col: str,
    dl_col: str,
    is_fts5: bool,
    keyword: str,
    limit: int,
):
    kw = keyword.lower()
    q_table = f'"{table}"'
    q_name = f'"{name_col}"'
    q_dl = f'"{dl_col}"'

    def run_like():
        escaped = kw.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        sql = f"SELECT {q_name}, {q_dl} FROM {q_table} WHERE {q_name} LIKE ? ESCAPE '\\'"
        return con.execute(sql, (f"%{escaped}%",))

    cur = None
    if is_fts5 and len(kw) >= TRIGRAM_MIN:
        fts_query = '"' + kw.replace('"', '""') + '"'
        sql = f"SELECT {q_name}, {q_dl} FROM {q_table} WHERE {q_name} MATCH ?"
        try:
            cur = con.execute(sql, (fts_query,))
        except sqlite3.OperationalError:
            cur = run_like()
    else:
        cur = run_like()
    return heapq.nlargest(limit, cur, key=lambda r: r[1])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Search PyPI packages by substring (SQLite FTS5 trigram).",
    )
    ap.add_argument("keyword", nargs="?", help="substring to search for (case-insensitive)")
    ap.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max results (default: {DEFAULT_LIMIT})",
    )
    args = ap.parse_args()
    if not DB_PATH.exists():
        sys.exit(f"Index not found: {DB_PATH}")
    keyword = args.keyword
    if not keyword:
        keyword = input("Search package: ").strip()
        if not keyword:
            print("No keyword given.")
            return
    con = sqlite3.connect(str(DB_PATH))
    try:
        table, name_col, dl_col, is_fts5 = get_table_info(con)
        rows = search(con, table, name_col, dl_col, is_fts5, keyword, args.limit)
    finally:
        con.close()
    if not rows:
        print(f"No matches for '{keyword}'.")
        return
    for name, dl in rows:
        print(f"{name}  {dl}")


if __name__ == "__main__":
    main()
