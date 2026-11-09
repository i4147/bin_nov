#!/data/data/com.termux/files/usr/bin/env python
"""Report the files whose encoding is not UTF-8.
Detects the encoding from the BOM or by decoding attempts, prints the non-UTF-8 files and with ``-a`` converts them to UTF-8 in place.
Usage: nonutf8 [-a] [paths ...] Options: -a, --apply Convert detected non-UTF-8 files to UTF-8 in place Examples: $ nonutf8 -a docs/ Command-line script: run ``python nonutf8.py`` or the ``nonutf8`` command.
The module is importable as well."""

import argparse
import codecs
from pathlib import Path
from typing import Iterator, Optional


BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def detect_encoding(data: bytes) -> Optional[str]:
    for bom, enc in BOMS:
        if data.startswith(bom):
            return enc
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            data.decode(enc)
        except UnicodeDecodeError:
            continue
        return enc
    return None


def iter_files(root: Path) -> Iterator[Path]:
    for p in root.rglob("*"):
        if p.is_symlink() or ".git" in p.parts:
            continue
        if p.is_file() and not p.is_symlink():
            yield p


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="encoding-report",
        description="Report files whose encoding is not UTF-8, recursively.",
    )
    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="convert detected non-UTF-8 files to UTF-8 in place",
    )
    args = parser.parse_args()
    root: Path = Path.cwd()
    found: int = 0
    converted: int = 0
    for path in sorted(iter_files(root)):
        try:
            data: bytes = path.read_bytes()
        except OSError as exc:
            print(f"error {path.relative_to(root)}: {exc}")
            continue
        enc: Optional[str] = detect_encoding(data)
        if enc is None or enc == "utf-8":
            continue
        rel: Path = path.relative_to(root)
        found += 1
        if args.apply:
            text: str = data.decode(enc)
            path.write_bytes(text.encode("utf-8"))
            converted += 1
            print(f"converted {rel} ({enc} -> utf-8)")
        else:
            print(f"non-utf8 {rel} ({enc})")
    if args.apply:
        print(f"scanned {root}, non-utf8 found: {found}, converted: {converted}")
    else:
        print(f"scanned {root}, non-utf8 found: {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
