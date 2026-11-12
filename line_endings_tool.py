#!/data/data/com.termux/files/usr/bin/env python
"""Normalize line endings in text files (CRLF -> LF, or LF -> CRLF).
Analysis / deduplication ------------------------ ``dostounix.py`` (hand-rolled CRLF->LF) and ``pydos2unix.py`` (the third-party ``dos2unix`` package) are the same converter; the stdlib byte replacement below covers both, so no dependency remains.
Binary files (private ``dh.is_binary`` before) are skipped by a NUL-byte sniff on the first 8 KiB.
``--to dos`` is the inverse conversion, exposed for symmetry.
Examples -------- python3 line_endings_tool.py convert .
--dry-run python3 line_endings_tool.py convert notes.txt src/ python3 line_endings_tool.py convert .
--to dos -q"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys

DEFAULT_WORKERS: int = 8
SNIFF_BYTES: int = 8192
SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "venv",
    ".venv",
})


def is_binary(path: Path) -> bool:
    """Heuristic: a NUL byte in the first chunk means binary."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(SNIFF_BYTES)
    except OSError:
        return True


def convert_bytes(data: bytes, to: str) -> bytes:
    """Convert line endings: ``to="unix"`` gives LF, ``to="dos"`` CRLF."""
    normalized: bytes = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized if to == "unix" else normalized.replace(b"\n", b"\r\n")


def convert_file(job: tuple[Path, str, bool]) -> tuple[Path, bool, str]:
    """Convert one file; return (path, changed, detail)."""
    path, to, dry_run = job
    try:
        original: bytes = path.read_bytes()
        converted: bytes = convert_bytes(original, to)
        if converted == original:
            return path, False, "already ok"
        if not dry_run:
            path.write_bytes(converted)
        return path, True, "converted"
    except OSError as exc:
        return path, False, f"error: {exc}"


def collect_files(inputs: Sequence[Path]) -> list[Path]:
    """Collect non-binary files from file/dir arguments."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob("*") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and not SKIP_DIRS.intersection(path.parts) and not is_binary(path):
                found.add(path)
    return sorted(found)


def cmd_convert(args: argparse.Namespace) -> int:
    """Run the ``convert`` subcommand."""
    files: list[Path] = collect_files(args.paths)
    if not files:
        print("No text files found.")
        return 0
    jobs: list[tuple[Path, str, bool]] = [(p, args.to, args.dry_run) for p in files]
    changed: int = 0
    errors: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for path, did_change, detail in pool.map(convert_file, jobs):
            changed += 1 if did_change else 0
            errors += 1 if detail.startswith("error") else 0
            if not args.quiet and (did_change or args.verbose):
                prefix: str = "Would convert" if args.dry_run and did_change else detail
                print(f"{prefix}: {path}")
    verb: str = "Would convert" if args.dry_run else "Converted"
    print(f"{verb} {changed}/{len(files)} file(s) to {args.to} endings; {errors} error(s).")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Normalize text file line endings.")
    sub = parser.add_subparsers(dest="command", required=True)
    conv = sub.add_parser("convert", help="Convert CRLF <-> LF in text files")
    conv.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: current dir)")
    conv.add_argument("--to", choices=("unix", "dos"), default="unix", help="Target endings (default: unix)")
    conv.add_argument("-n", "--dry-run", action="store_true", help="Report only")
    conv.add_argument("-q", "--quiet", action="store_true", help="Only print the summary")
    conv.add_argument("-v", "--verbose", action="store_true", help="Also list unchanged files")
    conv.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS, help="Workers (default: 8)")
    conv.set_defaults(handler=cmd_convert)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
