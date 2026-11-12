#!/data/data/com.termux/files/usr/bin/env python
"""License-file utilities: find, collect, truncate, strip license text.
Analysis / deduplication ------------------------ - ``licr.py`` finds ``license*`` files and immediately truncates them to empty (via private ``dh`` lookups) -> ``find`` (truncate only with ``--truncate --yes``) and ``collectlic.py`` (concatenate every ``*license*`` file into one bundle, hardcoded ``/sdcard/all2.txt``) -> ``collect -o all2.txt``.
- ``rmlic.py`` removes license *blocks* (blank-line-separated patterns read from ``/sdcard/lic``) from every text file -> ``strip-text``; it now reports first and needs ``--apply`` to rewrite files.
All destructive modes are opt-in; everything lists what it would do.
Examples -------- python3 license_tool.py find ~/projects python3 license_tool.py find .
--truncate --yes python3 license_tool.py collect .
-o all_licenses.txt python3 license_tool.py strip-text .
--patterns ~/lic"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import re
import sys

LICENSE_EXTS: frozenset[str] = frozenset({".md", ".txt", ".rst", ""})
MIN_BLANK_LINES: int = 3
BINARY_SUFFIXES: frozenset[str] = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".pyc",
    ".so",
    ".o",
    ".exe",
    ".dll",
    ".bin",
    ".mp3",
    ".mp4",
    ".db",
})
UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB")


def human_size(num: int) -> str:
    """Format a byte count like ``1.5 KB``."""
    size: float = float(num)
    for unit in UNITS:
        if abs(size) < 1024.0 or unit == UNITS[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def find_license_files(root: Path, mode: str, exts: frozenset[str]) -> list[Path]:
    """License files under *root* (name check depends on *mode*)."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        low: str = path.name.lower()
        if path.suffix.lower() not in exts:
            continue
        if mode == "prefix" and not low.startswith("license"):
            continue
        if mode == "contains" and "license" not in low:
            continue
        found.append(path)
    return sorted(found)


def cmd_find(args: argparse.Namespace) -> int:
    """List license files; optionally truncate them to zero bytes."""
    files: list[Path] = find_license_files(args.root, args.match, LICENSE_EXTS)
    for path in files:
        print(f"{path}  ({human_size(path.stat().st_size)})")
    print(f"Found {len(files)} license file(s).")
    if args.truncate:
        if not args.yes:
            print("Error: --truncate empties files; add --yes to confirm.", file=sys.stderr)
            return 2
        for path in files:
            path.write_text("", encoding="utf-8")
            print(f"truncated: {path}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Concatenate all license files into one bundle."""
    files: list[Path] = [
        p for p in find_license_files(args.root, "contains", LICENSE_EXTS) if p.resolve() != args.output.resolve()
    ]
    print(f"Found {len(files)} file(s)")
    with args.output.open("w", encoding="utf-8") as sink:
        for index, path in enumerate(files):
            try:
                sink.write(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                print(f"Skipping unreadable file: {path}")
                continue
            if index != len(files) - 1:
                sink.write("\n\n\n")
            print(f"Added: {path}")
    print(f"Finished: {args.output} created.")
    return 0


def load_patterns(pattern_file: Path, blank_lines: int) -> list[str]:
    """Split a pattern file into blocks separated by >=N blank lines."""
    content: str = pattern_file.read_text(encoding="utf-8", errors="ignore")
    separator: str = r"\n(?:\s*\n){" + str(blank_lines) + r",}"
    return [block.strip() for block in re.split(separator, content) if block.strip()]


def pattern_regex(block: str) -> re.Pattern[str]:
    """Literal block as a regex, tolerant of whitespace around newlines."""
    return re.compile(re.escape(block).replace(r"\n", r"\s*\n\s*"), re.IGNORECASE | re.MULTILINE)


def strip_one(job: tuple[Path, list[str], bool]) -> tuple[Path, int, int]:
    """Remove all pattern blocks from one file; return (path, before, after)."""
    path, blocks, apply = job
    try:
        original: str = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path, 0, 0
    cleaned: str = original
    for block in blocks:
        cleaned = pattern_regex(block).sub("", cleaned)
    before: int = len(original.encode())
    after: int = len(cleaned.encode())
    if apply and cleaned != original:
        path.write_text(cleaned, encoding="utf-8")
    return path, before, after


def text_files(root: Path, exclude: Path) -> list[Path]:
    """All non-binary files under *root* (pattern file excluded)."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.resolve() == exclude.resolve():
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if b"\x00" in path.read_bytes()[:8192]:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found)


def cmd_strip_text(args: argparse.Namespace) -> int:
    """Remove license-text blocks from all text files."""
    if not args.patterns.is_file():
        print(f"Error: pattern file not found: {args.patterns}", file=sys.stderr)
        return 1
    blocks: list[str] = load_patterns(args.patterns, args.blank_lines)
    if not blocks:
        print("No patterns found. Exiting.")
        return 0
    print(f"Loaded {len(blocks)} pattern block(s) from {args.patterns}")
    files: list[Path] = text_files(args.root, args.patterns)
    jobs: list[tuple[Path, list[str], bool]] = [(p, blocks, args.apply) for p in files]
    saved_total: int = 0
    changed: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for path, before, after in pool.map(strip_one, jobs):
            if before != after:
                changed += 1
                saved_total += before - after
                verb: str = "updated" if args.apply else "would update"
                print(f"{path.name} {verb} (saved {human_size(before - after)})")
    print(
        f"{changed}/{len(files)} file(s), saved {human_size(saved_total)}"
        + ("" if args.apply else " (dry run; add --apply)")
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="License file utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    fi = sub.add_parser("find", help="Find license files (optionally truncate)")
    fi.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    fi.add_argument(
        "--match",
        choices=("prefix", "contains"),
        default="prefix",
        help="prefix: name starts with license (licr); contains: anywhere",
    )
    fi.add_argument("--truncate", action="store_true", help="Empty each file (licr behaviour)")
    fi.add_argument("--yes", action="store_true", help="Confirm --truncate")
    fi.set_defaults(handler=cmd_find)
    co = sub.add_parser("collect", help="Concatenate licenses into one file")
    co.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    co.add_argument(
        "-o", "--output", type=Path, default=Path("all2.txt"), help="Bundle path (original: /sdcard/all2.txt)"
    )
    co.set_defaults(handler=cmd_collect)
    st = sub.add_parser("strip-text", help="Remove license blocks from text files")
    st.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    st.add_argument(
        "--patterns", type=Path, default=Path("/sdcard/lic"), help="Pattern file (original default: /sdcard/lic)"
    )
    st.add_argument(
        "--blank-lines", type=int, default=MIN_BLANK_LINES, help="Blank lines separating pattern blocks (default: 3)"
    )
    st.add_argument("--apply", action="store_true", help="Rewrite files (default: report)")
    st.add_argument("-j", "--workers", type=int, default=8)
    st.set_defaults(handler=cmd_strip_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
