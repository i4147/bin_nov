#!/data/data/com.termux/files/usr/bin/env python
"""Convert a chat export into JSON, CSV or SQLite.
Parses the ``[YYYY-MM-DD HH:MM:SS] user: message`` lines into per-user message groups, optionally merges consecutive messages of the same user or removes the timestamps, and writes the chosen output format.
Usage: tchat2json INPUT [--time] [--merge] [--remove] [-o OUTPUT] Positional arguments: input ...
Options: -t, --time -o, --output -m, --merge -r, --remove Examples: $ tchat2json chat.txt -o chat.json Command-line script: run ``python tchat2json.py`` or the ``tchat2json`` command.
The module is importable as well."""

import argparse
import csv
import json
from pathlib import Path
import re
import sqlite3
import sys


LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(.*?):\s?(.*)$")


def parse_chat(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for line in text.splitlines():
        match = LINE_RE.match(line)
        if match:
            time, user, msg = match.groups()
            messages.append({"time": time, "user": user.strip(), "msg": msg.rstrip()})
        elif messages and line.strip():
            messages[-1]["msg"] += "\n" + line
    return messages


def group_by_user(messages: list[dict[str, str]]) -> list[dict[str, object]]:
    users: dict[str, list[str]] = {}
    order: list[str] = []
    for m in messages:
        u = m["user"]
        if u not in users:
            users[u] = []
            order.append(u)
        if m["msg"] not in users[u]:
            users[u].append(m["msg"])
    return [{"user": u, "msg": users[u]} for u in order]


def write_json(records: list[dict[str, object]], dst: Path) -> None:
    dst.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(records: list[dict[str, object]], dst: Path, fields: list[str]) -> None:
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = dict(r)
            if isinstance(row.get("msg"), list):
                row["msg"] = json.dumps(row["msg"], ensure_ascii=False)
            writer.writerow(row)


def write_db(records: list[dict[str, object]], dst: Path, fields: list[str]) -> None:
    conn = sqlite3.connect(dst)
    cur = conn.cursor()
    cols = ", ".join(f"{f} TEXT" for f in fields)
    cur.execute(f"CREATE TABLE messages ({cols})")
    placeholders = ", ".join(f":{f}" for f in fields)
    rows = []
    for r in records:
        row = dict(r)
        if isinstance(row.get("msg"), list):
            row["msg"] = json.dumps(row["msg"], ensure_ascii=False)
        rows.append(row)
    cur.executemany(f"INSERT INTO messages ({', '.join(fields)}) VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="*", type=Path)
    parser.add_argument("-t", "--time", action="store_true")
    parser.add_argument("-o", "--output", choices=["json", "csv", "db"], default="json")
    parser.add_argument("-m", "--merge", action="store_true", default=True)
    parser.add_argument("-r", "--remove", action="store_true")
    args = parser.parse_args()
    if args.input:
        srcs = list(args.input)
    else:
        srcs = sorted(Path.cwd().glob("*.txt"))
        if not srcs:
            sys.exit("error: no .txt files found in current directory")
    for src in srcs:
        if not src.is_file():
            sys.exit(f"error: file not found: {src}")
    for src in srcs:
        messages = parse_chat(src.read_text(encoding="utf-8"))
        messages = [m for m in messages if m["msg"].strip() != "[No Text/Media]"]
        if args.merge:
            records: list[dict[str, object]] = group_by_user(messages)
            fields = ["user", "msg"]
        else:
            if not args.time:
                for m in messages:
                    m.pop("time", None)
            records = messages
            fields = ["time", "user", "msg"] if args.time else ["user", "msg"]
        ext = {"json": ".json", "csv": ".csv", "db": ".db"}[args.output]
        dst: Path = src.with_suffix(ext)
        if args.output == "json":
            write_json(records, dst)
        elif args.output == "csv":
            write_csv(records, dst, fields)
        else:
            write_db(records, dst, fields)
        print(f"Parsed {len(records)} records -> {dst}")
        if args.remove:
            src.unlink()
            print(f"Removed original file: {src}")


if __name__ == "__main__":
    main()
