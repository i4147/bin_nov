#!/data/data/com.termux/files/usr/bin/env python
"""Size-based file listings: largest files, thresholds, same-size groups.
Analysis / deduplication ------------------------ - ``top11.py`` (N largest, default 11, via ``dh``) and ``top12.py`` (heap-based top 10 despite the name) -> ``largest -n N`` (heapq, works on huge trees without sorting everything).
- ``find_big_files.py`` (everything over MiB threshold; it referenced an undefined ``should_skip``) -> ``above --mb 1``.
- ``similar_size_files.py`` (groups sharing a byte size) -> ``similar``.
- ``ssize.py`` (top-level entries by size, ANSI colors, grand total) and ``sortbysize.py`` (same thing written to ``<dirname>.json``) -> ``entries`` with optional ``--json FILE``.
- ``outcast.py`` (copy the single largest file out of a hardcoded Telegram cache dir) -> ``copy-largest SRC` with the original paths as defaults.
Examples -------- python3 file_size_tool.py largest ~ -n 20 python3 file_size_tool.py above .
--mb 50 python3 file_size_tool.py similar .
python3 file_size_tool.py entries .
--json sizes.json"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
import heapq
import json
from pathlib import Path
import secrets
import shutil
import string
import sys

DEFAULT_THRESHOLD: int = 1024 * 1024
TELEGRAM_CACHE: Path = Path("/sdcard/Android/data/org.telegram.messenger/cache")
UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num: int) -> str:
    """Format a byte count like ``1.5 MB``."""
    size: float = float(num)
    for unit in UNITS:
        if abs(size) < 1024.0 or unit == UNITS[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def iter_files(root: Path) -> list[tuple[Path, int]]:
    """Return (path, size) for every non-symlink file under *root*."""
    found: list[tuple[Path, int]] = []
    for path in root.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        try:
            found.append((path, path.stat().st_size))
        except OSError:
            continue
    return found


def dir_size(path: Path) -> int:
    """Recursive size of a file or directory (symlinks not followed)."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total: int = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def cmd_largest(args: argparse.Namespace) -> int:
    """Print the N largest files under a directory."""
    count: int = max(1, args.number)
    top: list[tuple[int, str]] = []
    for path, size in iter_files(args.root):
        item: tuple[int, str] = (size, str(path))
        if len(top) < count:
            heapq.heappush(top, item)
        elif size > top[0][0]:
            heapq.heapreplace(top, item)
    rows: list[tuple[int, str]] = sorted(top, reverse=True)
    if not rows:
        print("No files found.")
        return 0
    width: int = min(max(len(path) for _size, path in rows), 80)
    print(f"{'No.':<4} {'File Path':<{width}} {'Size':>12}")
    print("-" * (width + 20))
    for index, (size, path) in enumerate(rows, start=1):
        shown: str = path if len(path) <= width else "..." + path[-(width - 3) :]
        print(f"{index:<4} {shown:<{width}} {human_size(size):>12}")
    return 0


def cmd_above(args: argparse.Namespace) -> int:
    """Print files larger than a threshold."""
    threshold: int = int(args.mb * 1024 * 1024) if args.mb is not None else args.bytes
    hits: list[tuple[Path, int]] = [(p, s) for p, s in iter_files(args.root) if s > threshold]
    for path, size in sorted(hits, key=lambda item: item[1], reverse=True):
        label: object = path.relative_to(args.root) if args.root in path.parents else path
        print(f"{label} : {human_size(size)}")
    print(f"# {len(hits)} file(s) above {human_size(threshold)}", file=sys.stderr)
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    """Group files that share the exact same byte size."""
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path, size in iter_files(args.root):
        if size >= args.min_bytes:
            by_size[size].append(path)
    groups: int = 0
    for size in sorted(by_size):
        paths: list[Path] = by_size[size]
        if len(paths) > 1:
            groups += 1
            print(f"{human_size(size)} ({size} bytes):")
            for path in sorted(paths):
                print(f"    - {path}")
    print(f"# {groups} same-size group(s)", file=sys.stderr)
    return 0


def cmd_entries(args: argparse.Namespace) -> int:
    """List top-level entries with (recursive) sizes, optionally as JSON."""
    items: list[dict[str, object]] = []
    total: int = 0
    for path in sorted(args.root.iterdir()):
        if path.is_symlink():
            continue
        size: int = dir_size(path)
        total += size
        items.append({"name": path.name, "size": size})
    items.sort(key=lambda item: int(str(item["size"])), reverse=not args.ascending)
    for item in items:
        print(f"{item['name']} : {human_size(int(str(item['size'])))}")
    print(f"\ntotal: {human_size(total)}")
    if args.json is not None:
        out: Path = args.json if str(args.json) != "-" else Path(f"{args.root.resolve().name}.json")
        out.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    return 0


def random_name(length: int = 6) -> str:
    """Random lowercase filename stem (outcast behaviour)."""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(length))


def cmd_copy_largest(args: argparse.Namespace) -> int:
    """Copy the single largest file out of a directory."""
    files: list[tuple[Path, int]] = iter_files(args.source)
    if not files:
        print(f"No files in {args.source}", file=sys.stderr)
        return 1
    biggest, size = max(files, key=lambda item: item[1])
    dest: Path = args.dest or Path("/sdcard/Download") / f"{random_name()}.mkv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(biggest, dest)
    print(f"Largest ({human_size(size)}): {biggest} -> {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Find/list files by size.")
    sub = parser.add_subparsers(dest="command", required=True)
    lg = sub.add_parser("largest", help="N largest files in a tree")
    lg.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    lg.add_argument("-n", "--number", type=int, default=11, help="How many (originals: 11 and 10)")
    lg.set_defaults(handler=cmd_largest)
    ab = sub.add_parser("above", help="Files over a size threshold")
    ab.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    ab.add_argument("--mb", type=float, default=None, help="Threshold in MiB (original default: 1)")
    ab.add_argument("--bytes", type=int, default=DEFAULT_THRESHOLD, help="Threshold in bytes")
    ab.set_defaults(handler=cmd_above)
    si = sub.add_parser("similar", help="Groups of files with identical sizes")
    si.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    si.add_argument("--min-bytes", type=int, default=1, help="Ignore tiny files (default: 1)")
    si.set_defaults(handler=cmd_similar)
    en = sub.add_parser("entries", help="Top-level entries sorted by recursive size")
    en.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    en.add_argument("--ascending", action="store_true", help="Smallest first (ssize order)")
    en.add_argument(
        "--json",
        nargs="?",
        const=Path("-"),
        type=Path,
        default=None,
        metavar="FILE",
        help="Also write JSON (no FILE = <dirname>.json)",
    )
    en.set_defaults(handler=cmd_entries)
    cl = sub.add_parser("copy-largest", help="Copy out the single largest file")
    cl.add_argument(
        "source", nargs="?", type=Path, default=TELEGRAM_CACHE, help="Source dir (original default: Telegram cache)"
    )
    cl.add_argument(
        "-o", "--dest", type=Path, default=None, help="Destination (original default: /sdcard/Download/<random>.mkv)"
    )
    cl.set_defaults(handler=cmd_copy_largest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
