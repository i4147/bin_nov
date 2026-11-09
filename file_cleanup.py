#!/data/data/com.termux/files/usr/bin/env python
"""Text and JSON line-cleaning toolkit.
Subcommands: ``analyze`` prints the line statistics, ``sort-dedupe`` sorts and/or removes the duplicate lines (case-insensitive, reversed and range options), ``dedupe-seq`` drops the adjacent duplicates, ``drop-same-char`` removes the separator lines and ``dedupe-json`` deduplicates a JSON list of objects.
The large files are read through an mmap.
Usage: file_cleanup {analyze,sort-dedupe,dedupe-seq,drop-same-char,dedupe-json} [options] Positional arguments: file Text file to analyze Subcommands: analyze Show line statistics for a text file sort-dedupe Sort and/or remove duplicate lines in a text file dedupe-seq Remove sequential (adjacent) duplicate lines drop-same-char Remove lines made of a single repeated character dedupe-json Deduplicate a JSON list of dicts Options: --encoding -o, --output Output file (default: overwrite input) --sort Sort lines (default: on) --no-sort Do not sort lines --unique Remove duplicate lines (default: on) --no-unique Do not remove duplicates -r, --reverse Sort in reverse order -i, --case-insensitive Case-insensitive sorting --skip-empty Skip empty lines when reading --start-line 1-based first line of the range to process (soniq.py) ...
plus 13 more; run ``file_cleanup --help`` for the full list.
Examples: $ file_cleanup sort-dedupe words.txt $ file_cleanup drop-same-char notes.txt Command-line script: run ``python file_cleanup.py`` or the ``file_cleanup`` command.
The module is importable as well."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import heapq
import json
import mmap
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Callable, Iterable, Optional, Sequence

from dh import MAX_WORKERS


MMAP_THRESHOLD: int = 1_048_576
DEFAULT_CHUNK_SIZE: int = 100_000
DEFAULT_ENCODING: str = "utf-8"


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def read_lines(path: Path, encoding: str = DEFAULT_ENCODING, skip_empty: bool = False) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    lines: list[str] = []
    size = path.stat().st_size
    if size > MMAP_THRESHOLD:
        with (
            path.open("rb") as f,
            mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm,
        ):
            start = 0
            n = len(mm)
            while start < n:
                end = mm.find(b"\n", start)
                chunk = mm[start:] if end == -1 else mm[start:end]
                try:
                    line = chunk.decode(encoding).rstrip("\r\n")
                    if not skip_empty or line.strip():
                        lines.append(line)
                except UnicodeDecodeError:
                    pass
                if end == -1:
                    break
                start = end + 1
    else:
        with path.open("r", encoding=encoding) as f:
            for raw in f:
                line = raw.rstrip("\r\n")
                if not skip_empty or line.strip():
                    lines.append(line)
    return lines


def write_lines(path: Path, lines: Sequence[str], encoding: str = DEFAULT_ENCODING) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding) as f:
        for line in lines:
            f.write(line + "\n")


def dedupe_preserving_order(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    seen: set = set()
    unique: list[str] = []
    removed: list[str] = []
    for line in lines:
        if line in seen:
            removed.append(line)
        else:
            seen.add(line)
            unique.append(line)
    return unique, removed


def backup_file(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


def sort_lines(
    lines: list[str],
    *,
    key: Optional[Callable] = None,
    reverse: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    workers: int = 1,
) -> list[str]:
    if len(lines) <= chunk_size:
        return sorted(lines, key=key, reverse=reverse)
    tmp_dir = Path(tempfile.gettempdir())
    chunk_files: list[Path] = []
    for i in range(0, len(lines), chunk_size):
        piece = sorted(lines[i : i + chunk_size], key=key, reverse=reverse)
        cf = tmp_dir / f".fc_sort_{os.getpid()}_{i}.tmp"
        cf.write_text("\n".join(piece), encoding="utf-8")
        chunk_files.append(cf)
    handles = [cf.open("r", encoding="utf-8") for cf in chunk_files]
    try:
        merged = heapq.merge(*handles, key=key, reverse=reverse)
        return [ln.rstrip("\n") for ln in merged]
    finally:
        for h in handles:
            h.close()
        for cf in chunk_files:
            cf.unlink(missing_ok=True)


def cmd_analyze(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        err(f"File not found: {path}")
        return 1
    lines = read_lines(path, args.encoding, skip_empty=False)
    counter = Counter(lines)
    total = len(lines)
    unique = len(counter)
    dup = sum(c - 1 for c in counter.values() if c > 1)
    max_len = max((len(x) for x in lines), default=0)
    avg_len = (sum(len(x) for x in lines) / total) if total else 0.0
    top10 = counter.most_common(10)
    print("=" * 44)
    print(f"File Analysis: {path.name}")
    print("=" * 44)
    print("\nBasic Statistics:")
    print(f"  Size: {path.stat().st_size} bytes")
    print(f"  Total lines: {total:,}")
    print(f"  Unique lines: {unique:,}")
    print(f"  Duplicate lines: {dup:,} ({(dup / total * 100) if total else 0:.1f}%)")
    print("\nLine Length Statistics:")
    print(f"  Maximum: {max_len} characters")
    print(f"  Average: {avg_len:.1f} characters")
    if top10:
        print("\nMost Common Lines (Top 10):")
        for line, cnt in top10:
            shown = line[:47] + "..." if len(line) > 50 else line
            print(f"  ({cnt}x) {shown}")
    print("=" * 44)
    return 0


def cmd_sort_dedupe(args: argparse.Namespace) -> int:
    input_path = Path(args.file)
    if not input_path.is_file():
        err(f"File not found: {input_path}")
        return 1
    output_path = Path(args.output) if args.output else input_path
    all_lines = read_lines(input_path, args.encoding, args.skip_empty)
    original_count = len(all_lines)
    original_size = input_path.stat().st_size
    range_mode = args.start_line is not None or args.end_line is not None
    if range_mode:
        if args.start_line is None or args.end_line is None:
            err("Both --start-line and --end-line must be provided together.")
            return 1
        s, e = args.start_line - 1, args.end_line
        if s < 0 or e <= s or s >= len(all_lines):
            err(f"Invalid line range: {args.start_line}-{args.end_line} (file has {len(all_lines)} lines)")
            return 1
        before, middle, after = all_lines[:s], all_lines[s:e], all_lines[e:]
    else:
        before, middle, after = [], all_lines, []
    key = (lambda x: x.lower()) if args.case_insensitive else None
    if args.sort:
        middle = sort_lines(
            middle,
            key=key,
            reverse=args.reverse,
            chunk_size=args.chunk_size,
            workers=MAX_WORKERS,
        )
    removed: list[str] = []
    if args.unique:
        middle, removed = dedupe_preserving_order(middle)
    final_lines = before + middle + after
    removed_count = len(removed)
    stats = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "input_file": str(input_path),
        "output_file": str(output_path),
        "original_lines": original_count,
        "final_lines": len(final_lines),
        "duplicate_lines": removed_count,
        "original_size_bytes": original_size,
        "after_size_bytes": sum(len(x.encode(args.encoding)) + 1 for x in final_lines),
        "range": [args.start_line, args.end_line] if range_mode else None,
        "sorted": bool(args.sort),
        "unique": bool(args.unique),
        "dry_run": bool(args.dry_run),
    }
    stats["size_reduction_bytes"] = stats["original_size_bytes"] - stats["after_size_bytes"]
    if args.dry_run:
        info("DRY RUN — no files modified.")
    else:
        if args.backup and output_path == input_path:
            bak = backup_file(input_path)
            info(f"Backup created: {bak.name}")
        if output_path == input_path:
            fd, tmp_name = tempfile.mkstemp(dir=str(input_path.parent))
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                write_lines(tmp, final_lines, args.encoding)
                tmp.replace(input_path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        else:
            write_lines(output_path, final_lines, args.encoding)
        info(f"Output written: {output_path}")
    print("=" * 44)
    print(f"Input file: {input_path}")
    print(f"Output file: {output_path}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'NORMAL'}")
    print("-" * 44)
    print(f"Original lines: {original_count:,}")
    print(f"Final lines:    {len(final_lines):,}")
    if removed_count > 0:
        pct = removed_count / original_count * 100 if original_count else 0
        print(f"Duplicates removed: {removed_count:,} ({pct:.1f}%)")
    if not args.quiet and removed:
        print("\nRemoved lines (up to 50):")
        for line in removed[:50]:
            shown = line[:77] + "..." if len(line) > 80 else line
            print(f"  {shown}")
        if len(removed) > 50:
            print(f"  ... ({len(removed) - 50} more not shown)")
    elif args.quiet and removed:
        print("  (Use without --quiet to see the actual duplicate lines)")
    print("=" * 44)
    if args.report:
        try:
            Path(args.report).write_text(
                json.dumps(stats, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            info(f"Report saved: {args.report}")
        except Exception as e:
            err(f"Error saving report: {e}")
            return 1
    return 0


def cmd_dedupe_seq(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        err(f"File not found: {path}")
        return 1
    try:
        lines = path.read_text(encoding=args.encoding).splitlines(keepends=True)
    except Exception as e:
        err(f"Error reading {path}: {e}")
        return 1
    ignore_blanks = not args.include_blanks

    def blank(s: str) -> bool:
        return s.strip() == ""

    dupes: list[tuple[int, str]] = []
    i = 0
    while i < len(lines) - 1:
        w, p = lines[i], lines[i + 1]
        if ignore_blanks and (blank(w) or blank(p)):
            i += 1
            continue
        if w == p:
            dupes.append((i + 1, w.rstrip("\n")))
            i += 1
        i += 1
    if not dupes:
        print("✓ No sequential duplicates found.")
        return 0
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}📄 {path.name}")
    for lineno, content in dupes:
        print(f"  Line {lineno}: {content}")
        print(f"  Line {lineno + 1}: {content}")
    if args.dry_run:
        print(f"\n[DRY RUN] Would remove {len(dupes)} duplicate line(s).")
        return 0
    apply = args.yes
    if not args.yes:
        ans = input(f"\n  Remove duplicates from {path.name}? (y/n/a/q): ").strip().lower()
        if ans == "q":
            print("Quitting.")
            return 0
        apply = ans in ("y", "a")
    if not apply:
        print("  ⏭️  Skipped")
        return 0
    new_lines = lines[:]
    for lineno, _ in reversed(dupes):
        del new_lines[lineno]
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_text("".join(lines), encoding=args.encoding)
    path.write_text("".join(new_lines), encoding=args.encoding)
    print(f"  ✅ Fixed (backup: {bak.name}, {len(dupes)} line(s) removed)")
    return 0


def _is_same_char_line(line: str) -> bool:
    body = line.rstrip("\n")
    if len(body) <= 1:
        return False
    return all(c == body[0] for c in body)


def cmd_drop_same_char(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        err(f"File not found: {path}")
        return 1
    with path.open("r", encoding=args.encoding) as f:
        lines = f.readlines()
    kept = [ln for ln in lines if not _is_same_char_line(ln)]
    removed = len(lines) - len(kept)
    if args.dry_run:
        info(f"[DRY RUN] Would remove {removed} same-character line(s) from {path.name}.")
        return 0
    with path.open("w", encoding=args.encoding) as f:
        f.writelines(kept)
    info(f"Removed {removed} same-character line(s) from {path.name}.")
    return 0


def cmd_dedupe_json(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        err(f"File not found: {path}")
        return 1
    try:
        data = json.loads(path.read_text(encoding=args.encoding))
    except json.JSONDecodeError as e:
        err(f"Invalid JSON in {path}: {e}")
        return 1
    if not isinstance(data, list):
        err("Top-level JSON value must be a list.")
        return 1
    seen: set = set()
    unique: list[Any] = []
    for item in data:
        if isinstance(item, dict):
            if args.key:
                raw = item.get(args.key, "")
            else:
                raw = json.dumps(item, sort_keys=True, ensure_ascii=False)
            dedupe_key = raw.lower().strip() if args.lower else raw
        else:
            dedupe_key = item
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique.append(item)
    if args.sort_by:

        def sort_key(x: Any) -> str:
            if isinstance(x, dict):
                v = x.get(args.sort_by, "")
                return v.lower() if isinstance(v, str) else str(v)
            return ""

        unique.sort(key=sort_key)
    if args.dry_run:
        info(f"[DRY RUN] {len(data)} → {len(unique)} entries (would remove {len(data) - len(unique)}).")
        return 0
    path.write_text(
        json.dumps(unique, indent=args.indent, ensure_ascii=False),
        encoding=args.encoding,
    )
    info(f"{path.name}: {len(data)} → {len(unique)} entries ({len(data) - len(unique)} removed).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file_cleanup.py",
        description="Unified text/JSON line-cleaning toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  sonic.py --analyze              ->  analyze\n"
            "  sonic.py / soniq.py / soniq2.py ->  sort-dedupe\n"
            "  soniq.py --quiet                ->  sort-dedupe --quiet\n"
            "  soniq.py <f> <start> <end>      ->  sort-dedupe <f> --start-line S --end-line E\n"
            "  detect_repeated_lines.py        ->  dedupe-seq\n"
            "  samecharlines.py                ->  drop-same-char\n"
            "  juniq.py                        ->  dedupe-json --key src\n"
            "  sort_quotes.py                  ->  dedupe-json --key quote --lower --sort-by author\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("analyze", help="Show line statistics for a text file")
    p.add_argument("file", help="Text file to analyze")
    p.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"File encoding (default: {DEFAULT_ENCODING})",
    )
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("sort-dedupe", help="Sort and/or remove duplicate lines in a text file")
    p.add_argument("file", help="Input file")
    p.add_argument("-o", "--output", default=None, help="Output file (default: overwrite input)")
    p.add_argument(
        "--sort",
        dest="sort",
        action="store_true",
        default=True,
        help="Sort lines (default: on)",
    )
    p.add_argument("--no-sort", dest="sort", action="store_false", help="Do not sort lines")
    p.add_argument(
        "--unique",
        dest="unique",
        action="store_true",
        default=True,
        help="Remove duplicate lines (default: on)",
    )
    p.add_argument(
        "--no-unique",
        dest="unique",
        action="store_false",
        help="Do not remove duplicates",
    )
    p.add_argument("-r", "--reverse", action="store_true", help="Sort in reverse order")
    p.add_argument("-i", "--case-insensitive", action="store_true", help="Case-insensitive sorting")
    p.add_argument("--skip-empty", action="store_true", help="Skip empty lines when reading")
    p.add_argument(
        "--start-line",
        type=int,
        default=None,
        help="1-based first line of the range to process (soniq.py)",
    )
    p.add_argument(
        "--end-line",
        type=int,
        default=None,
        help="1-based inclusive last line of the range to process",
    )
    p.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak backup before overwriting input",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview only — do not modify any files")
    p.add_argument("--report", metavar="PATH", help="Save a JSON report of statistics to PATH")
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print counts — don't list removed lines",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"External-sort chunk size in lines (default: {DEFAULT_CHUNK_SIZE})",
    )
    p.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"File encoding (default: {DEFAULT_ENCODING})",
    )
    p.set_defaults(func=cmd_sort_dedupe)
    p = sub.add_parser("dedupe-seq", help="Remove sequential (adjacent) duplicate lines")
    p.add_argument("file", help="Input file")
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Auto-confirm removal (skip the interactive prompt)",
    )
    p.add_argument(
        "-b",
        "--include-blanks",
        action="store_true",
        help="Include blank lines in duplicate detection (default: skip blanks)",
    )
    p.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"File encoding (default: {DEFAULT_ENCODING})",
    )
    p.set_defaults(func=cmd_dedupe_seq)
    p = sub.add_parser("drop-same-char", help="Remove lines made of a single repeated character")
    p.add_argument("file", help="Input file")
    p.add_argument("--dry-run", action="store_true", help="Preview only — do not modify the file")
    p.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"File encoding (default: {DEFAULT_ENCODING})",
    )
    p.set_defaults(func=cmd_drop_same_char)
    p = sub.add_parser("dedupe-json", help="Deduplicate a JSON list of dicts")
    p.add_argument("file", help="JSON file containing a list")
    p.add_argument(
        "--key",
        default=None,
        help="Field to deduplicate by (default: full-dict identity, as in juniq.py when no --key is given)",
    )
    p.add_argument(
        "--lower",
        action="store_true",
        help="Case-insensitive + strip when comparing keys (sort_quotes.py behavior)",
    )
    p.add_argument("--sort-by", default=None, help="Sort output entries by this field (lowercased)")
    p.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent when writing output (default: 2)",
    )
    p.add_argument("--dry-run", action="store_true", help="Report counts without writing the file")
    p.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"File encoding (default: {DEFAULT_ENCODING})",
    )
    p.set_defaults(func=cmd_dedupe_json)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user.")
        sys.exit(1)
