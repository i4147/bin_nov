#!/data/data/com.termux/files/usr/bin/env python
"""Search PyPI package names using a compact SQLite FTS5 index or pip.json."""

from __future__ import annotations
import argparse
import heapq
import json
from pathlib import Path
import sqlite3
import sys
from typing import TypeAlias


DB_PATH: Path = Path("/sdcard/data/pip.db")
JSON_PATH: Path = Path("/sdcard/data/pip.json")
TABLE_NAME: str = "packages"
DEFAULT_LIMIT: int = 20
TRIGRAM_MIN: int = 3

PackageRecord: TypeAlias = tuple[str, int, int]


def to_int(value: object) -> int:
    """Convert a JSON value to an integer; use zero for invalid values."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return 0


def load_json_records(json_path: Path) -> list[PackageRecord]:
    """Read package records in [name, monthly_downloads, all_time_downloads] form."""
    try:
        with json_path.open("r", encoding="utf-8") as file:
            data: object = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to read JSON file {json_path}: {exc}"
        raise SystemExit(msg) from exc

    if not isinstance(data, list):
        msg = f"Invalid JSON in {json_path}: expected a top-level list."
        raise SystemExit(msg)

    records: list[PackageRecord] = []
    for item in data:
        if isinstance(item, list) and len(item) >= 3:
            records.append((str(item[0]), to_int(item[1]), to_int(item[2])))

    if not records:
        msg = f"No valid package records found in {json_path}."
        raise SystemExit(msg)

    return records


def create_index(con: sqlite3.Connection) -> None:
    """Replace the package table with a compact trigram FTS5 index."""
    con.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}"')
    con.execute(
        f"""
        CREATE VIRTUAL TABLE "{TABLE_NAME}" USING fts5(
            name,
            monthly_dl UNINDEXED,
            all_time_dl UNINDEXED,
            tokenize='trigram',
            detail=none,
            columnsize=0
        )
        """
    )


def update_database(db_path: Path, json_path: Path) -> None:
    """Build the SQLite index from the JSON package list."""
    records: list[PackageRecord] = load_json_records(json_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Building compact FTS5 index from {json_path}...")
    con: sqlite3.Connection = sqlite3.connect(str(db_path))
    try:
        with con:
            create_index(con)
            con.executemany(
                f"""
                INSERT INTO "{TABLE_NAME}" (name, monthly_dl, all_time_dl)
                VALUES (?, ?, ?)
                """,
                records,
            )

        # Reclaim unused pages and make the completed database as compact as
        # SQLite can make it.
        con.execute("VACUUM")
        con.execute("PRAGMA optimize")
    finally:
        con.close()

    print(f"Database updated: {db_path} ({len(records):,} packages)")


def search_database(
    db_path: Path,
    keyword: str,
    limit: int,
    nonzero_only: bool,
) -> list[PackageRecord]:
    """Search the SQLite FTS5 table and return the most-downloaded matches."""
    if limit <= 0:
        return []

    con: sqlite3.Connection = sqlite3.connect(str(db_path))
    try:
        nonzero_clause: str = (
            " AND (monthly_dl != 0 OR all_time_dl != 0)" if nonzero_only else ""
        )

        # For ordinary ASCII substrings, FTS5's trigram index can accelerate
        # LIKE searches when the pattern contains at least three characters.
        # '%' and '_' are LIKE wildcards, so use literal Python matching for
        # those inputs rather than accidentally treating them as wildcards.
        can_use_trigram_like: bool = (
            len(keyword) >= TRIGRAM_MIN
            and keyword.isascii()
            and "%" not in keyword
            and "_" not in keyword
        )

        if can_use_trigram_like:
            cursor: sqlite3.Cursor = con.execute(
                f"""
                SELECT name, monthly_dl, all_time_dl
                FROM "{TABLE_NAME}"
                WHERE name LIKE ?{nonzero_clause}
                ORDER BY all_time_dl DESC
                LIMIT ?
                """,
                (f"%{keyword}%", limit),
            )
            return [
                (str(row[0]), int(row[1]), int(row[2])) for row in cursor.fetchall()
            ]

        # Short, non-ASCII, or wildcard-containing terms use literal matching.
        # These cases may scan the table, but still return the correct results.
        folded_keyword: str = keyword.casefold()
        cursor = con.execute(
            f"""
            SELECT name, monthly_dl, all_time_dl
            FROM "{TABLE_NAME}"
            {f"WHERE monthly_dl != 0 OR all_time_dl != 0" if nonzero_only else ""}
            """
        )
        matches: list[PackageRecord] = []
        for row in cursor:
            name: str = str(row[0])
            if folded_keyword in name.casefold():
                matches.append((name, int(row[1]), int(row[2])))

        return heapq.nlargest(limit, matches, key=lambda record: record[2])
    finally:
        con.close()


def search_json(
    json_path: Path,
    keyword: str,
    limit: int,
    nonzero_only: bool,
) -> list[PackageRecord]:
    """Search pip.json directly without using SQLite."""
    if limit <= 0:
        return []

    folded_keyword: str = keyword.casefold()
    matches: list[PackageRecord] = []

    for name, monthly_dl, all_time_dl in load_json_records(json_path):
        if nonzero_only and monthly_dl == 0 and all_time_dl == 0:
            continue
        if folded_keyword in name.casefold():
            matches.append((name, monthly_dl, all_time_dl))

    return heapq.nlargest(limit, matches, key=lambda record: record[2])


def resolve_json_path(user_path: str | None, db_path: Path) -> Path:
    """Find pip.json, preferring an explicitly supplied path."""
    if user_path:
        path: Path = Path(user_path).expanduser()
        if not path.is_file():
            msg = f"JSON file not found: {path}"
            raise SystemExit(msg)
        return path

    candidates: tuple[Path, ...] = (
        JSON_PATH,
        Path("pip.json"),
        db_path.parent / "pip.json",
    )
    for path in candidates:
        if path.is_file():
            return path

    msg = "Could not find pip.json. Put it at /sdcard/data/pip.json or provide its location with --json."
    raise SystemExit(msg)


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Search PyPI package names using SQLite FTS5 or pip.json."
    )
    parser.add_argument(
        "keyword",
        nargs="?",
        help="substring to search for (case-insensitive)",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"maximum results to print (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "-z",
        "--nonzero",
        action="store_true",
        help="exclude packages with zero monthly and all-time downloads",
    )
    parser.add_argument(
        "-u",
        "--updatedb",
        action="store_true",
        help="create or replace the SQLite index from pip.json",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        default=True,
        help="search pip.json directly; do not use SQLite",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        help="path to pip.json (default: /sdcard/data/pip.json or ./pip.json)",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=str(DB_PATH),
        help=f"SQLite database path (default: {DB_PATH})",
    )

    args: argparse.Namespace = parser.parse_args()
    db_path: Path = Path(args.db_path).expanduser()

    if args.updatedb and args.json_only:
        parser.error("--updatedb and --json-only cannot be used together")

    if args.updatedb:
        json_path: Path = resolve_json_path(args.json_path, db_path)
        update_database(db_path, json_path)
        return 0

    keyword: str | None = args.keyword
    if keyword is None:
        keyword = input("Search package: ").strip()
    if not keyword:
        print("No keyword given.")
        return 0

    if args.json_only:
        json_path = resolve_json_path(args.json_path, db_path)
        print(f"Searching {json_path}...", file=sys.stderr)
        results: list[PackageRecord] = search_json(
            json_path,
            keyword,
            args.limit,
            args.nonzero,
        )
    else:
        if not db_path.is_file():
            msg = f"Index not found: {db_path}. Run with -u to create it, or use --json-only to search pip.json directly."
            raise SystemExit(msg)
        results = search_database(
            db_path,
            keyword,
            args.limit,
            args.nonzero,
        )

    if not results:
        print(f"No matches for {keyword}.")
        return 0

    for name, monthly_dl, all_time_dl in results:
        print(f"{name} {monthly_dl} {all_time_dl}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
