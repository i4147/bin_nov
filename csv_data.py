#!/data/data/com.termux/files/usr/bin/env python
"""CSV utilities: CSV->JSON, duplicated-line reports, dpkg inventory.
Analysis / deduplication ------------------------ - ``csv2json.py`` (first two columns -> JSON object) vs ``csv2json2.py`` (DictReader -> JSON list): one ``to-json`` command with ``--mode rows|map`` (new default ``rows``, the safer superset; ``map`` preserves the original first script).
- ``lcsv.py`` vs ``ltxt.py`` count identical lines per extension and report lines seen >= 2 times as ``<ext>.csv`` vs ``<ext>.txt``: one ``dup-lines`` command with ``--format csv|txt``.
The exclusion list formerly read from ``/sdcard/bin`` / the private ``dh.BIN_EXT`` is now ``--exclude`` with a built-in binary-extension default.
- ``savepkgstocsv.py`` (``dpkg-query`` -> installed.tsv/csv in ``/sdcard/backups``) is ``dpkg-export`` with ``-o DIR``.
Examples -------- python3 csv_data_tool.py to-json data.csv python3 csv_data_tool.py to-json data.csv --mode map -o map.json python3 csv_data_tool.py dup-lines --min-count 3 python3 csv_data_tool.py dpkg-export -o ./backups"""

import argparse
from collections import Counter, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import subprocess
import sys


BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".xz",
    ".bz2",
    ".7z",
    ".mp3",
    ".mp4",
    ".mkv",
    ".pyc",
    ".so",
    ".o",
    ".a",
    ".exe",
    ".dll",
    ".bin",
    ".db",
    ".sqlite3",
})
PKG_FIELDS: list[str] = [
    "Package",
    "Version",
    "Architecture",
    "Status",
    "Priority",
    "Section",
    "Installed-Size",
    "Maintainer",
    "Homepage",
    "Description",
    "Source",
    "Essential",
    "Multi-Arch",
    "Origin",
    "Bugs",
]
PKG_FORMAT: str = (
    "${binary:Package}\t${Version}\t${Architecture}\t${Status}\t${Priority}\t"
    "${Section}\t${Installed-Size}\t${Maintainer}\t${Homepage}\t${binary:Summary}\t"
    "${Source}\t${Essential}\t${Multi-Arch}\t${Origin}\t${Bugs}\n"
)


# ---------------------------------------------------------------- to-json
def csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV file as a list of row dicts (header row required)."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def csv_map(path: Path) -> dict[str, str]:
    """Read the first two columns of a CSV file as a dict."""
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header: list[str] | None = next(reader, None)
        if not header or len(header) < 2:
            msg = "Error: CSV must have at least two columns for --mode map"
            raise SystemExit(msg)
        for row in reader:
            if len(row) >= 2 and row[0].strip():
                result[row[0].strip()] = row[1].strip()
    return result


def cmd_to_json(args: argparse.Namespace) -> int:
    """Run the ``to-json`` subcommand."""
    if not args.input.is_file():
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1
    output: Path = args.output or args.input.with_suffix(".json")
    data: object = csv_map(args.input) if args.mode == "map" else csv_rows(args.input)
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Converted: {args.input} -> {output} (mode={args.mode})")
    return 0


# ------------------------------------------------------------- dup-lines
def count_lines(path: Path) -> Counter[str]:
    """Count non-empty stripped lines in one text file."""
    counter: Counter[str] = Counter()
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped: str = line.strip()
            if stripped:
                counter[stripped] += 1
    except OSError as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
    return counter


def group_by_extension(root: Path, excluded: frozenset[str]) -> dict[str, list[Path]]:
    """Group readable files under *root* by extension."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith(".") or ".git" in path.parts:
            continue
        ext: str = path.suffix.lower()
        if ext not in excluded:
            groups[ext].append(path)
    return dict(groups)


def cmd_dup_lines(args: argparse.Namespace) -> int:
    """Report lines that occur at least --min-count times, per extension."""
    excluded: frozenset[str] = BINARY_EXTENSIONS | {(e if e.startswith(".") else "." + e).lower() for e in args.exclude}
    groups: dict[str, list[Path]] = group_by_extension(args.root, excluded)
    if not groups:
        print("No eligible files found.")
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for ext, files in sorted(groups.items()):
        total: Counter[str] = Counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for counter in pool.map(count_lines, files):
                total.update(counter)
        kept: list[tuple[str, int]] = [(ln, n) for ln, n in total.most_common() if n >= args.min_count]
        label: str = ext.lstrip(".") or "no_extension"
        out_path: Path = args.out_dir / f"{label}.{args.format}"
        if args.format == "csv":
            with out_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["number_of_appearance", "line"])
                writer.writerows([n, ln] for ln, n in kept)
        else:
            out_path.write_text("".join(ln + "\n" for ln, _ in kept), encoding="utf-8")
        print(f"{label}: {len(files)} file(s), {len(kept)} repeated line(s) -> {out_path}")
    return 0


# ------------------------------------------------------------ dpkg export
def query_packages() -> list[list[str]]:
    """Return installed dpkg packages sorted by installed size (largest first)."""
    try:
        proc = subprocess.run(["dpkg-query", "-W", f"-f={PKG_FORMAT}"], check=True, capture_output=True, text=True)
    except FileNotFoundError:
        msg = "dpkg-query not found (not a Debian-based system)"
        raise SystemExit(msg) from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip()) from None
    rows: list[list[str]] = []
    for line in proc.stdout.splitlines():
        cols: list[str] = line.split("\t")
        if len(cols) == len(PKG_FIELDS):
            rows.append(cols)
    rows.sort(key=lambda row: int(row[6] or 0), reverse=True)
    return rows


def cmd_dpkg_export(args: argparse.Namespace) -> int:
    """Write installed.tsv / installed.csv for all dpkg packages."""
    rows: list[list[str]] = query_packages()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tsv: Path = args.out_dir / "installed.tsv"
    csv_out: Path = args.out_dir / "installed.csv"
    tsv.write_text("\t".join(PKG_FIELDS) + "\n" + "".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    with csv_out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(PKG_FIELDS)
        writer.writerows(rows)
    print(f"Saved {len(rows)} packages:\n  TSV: {tsv}\n  CSV: {csv_out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="CSV conversion and reporting utilities.")
    sub = parser.add_subparsers(dest="command", required=True)

    tj = sub.add_parser("to-json", help="Convert CSV to JSON")
    tj.add_argument("input", type=Path, help="Input .csv file")
    tj.add_argument("-o", "--output", type=Path, default=None, help="Output (default: input with .json)")
    tj.add_argument(
        "--mode", choices=("rows", "map"), default="rows", help="rows: list of dicts; map: first two columns as object"
    )
    tj.set_defaults(handler=cmd_to_json)

    dl = sub.add_parser("dup-lines", help="Per-extension repeated-line report")
    dl.add_argument("root", nargs="?", type=Path, default=Path.cwd(), help="Root dir (default: .)")
    dl.add_argument("--format", choices=("csv", "txt"), default="csv")
    dl.add_argument("--min-count", type=int, default=2, help="Minimum occurrences (default: 2)")
    dl.add_argument("--exclude", nargs="*", default=[], metavar="EXT", help="Extra extensions to skip")
    dl.add_argument("-o", "--out-dir", type=Path, default=Path.cwd(), help="Report directory")
    dl.add_argument("-j", "--workers", type=int, default=8, help="Reader threads (default: 8)")
    dl.set_defaults(handler=cmd_dup_lines)

    dp = sub.add_parser("dpkg-export", help="Export dpkg package list to TSV+CSV")
    dp.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=Path("/sdcard/backups"),
        help="Output dir (original default: /sdcard/backups)",
    )
    dp.set_defaults(handler=cmd_dpkg_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
