#!/data/data/com.termux/files/usr/bin/env python
"""Remove the tags or the meta tags of HTML files.
Subcommands: ``tag`` deletes a named element with BeautifulSoup, ``meta`` removes the ``<meta ...>`` tags with a regex (``--pattern`` overrides it) and ``all`` strips every tag of one file; ``-w`` writes in place, the default is a dry run and the marker lines (``<:``/``>:``) are preserved.
Usage: htmlstrip {tag,meta,all} [options] [paths ...] Positional arguments: tagname Tag to remove (e.g.
script, style, iframe) directory Root directory (default: .) file File to process Subcommands: tag Remove a named tag using BeautifulSoup (remove_tag.py).
meta Remove <meta ...> tags via regex (rmeta.py).
all Strip every <...> tag from one file (strip_tags.py).
Options: --extensions --pattern -w, --write Write changes in place (default: dry-run) --preserve-marker Substring; any line containing it is kept verbatim (repeatable; default: '<:' and '>:') Examples: $ htmlstrip tag script pages/ -w $ htmlstrip all page.html Command-line script: run ``python htmlstrip.py`` or the ``htmlstrip`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
from typing import Iterable, Optional, Sequence

from loguru import logger

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    BeautifulSoup = None
    HAS_BS4 = False
LOG = logger
DEFAULT_TAG_EXTS: tuple[str, ...] = (".html", ".txt")
DEFAULT_META_EXTS: tuple[str, ...] = (".html",)
DEFAULT_META_PATTERN: str = r"<meta[^>]*>"
DEFAULT_PRESERVE_MARKERS: tuple[str, ...] = ("<:", ">:")
ALL_TAGS_REGEX = re.compile(r"<[^>]*>")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _iter_files(
    root: Path,
    extensions: Sequence[str],
) -> Iterable[Path]:
    exts = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in extensions)
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def _normalize_exts(raw: Sequence[str], default: Sequence[str]) -> list[str]:
    if not raw:
        return list(default)
    return [e.lower() if e.startswith(".") else "." + e.lower() for e in raw]


def _remove_tag_from_file(path: Path, tag: str) -> bool:
    try:
        src = _read_text(path)
    except OSError as e:
        LOG.error("Cannot read {}: {}", path, e)
        return False
    try:
        soup = BeautifulSoup(src, "html.parser")
    except Exception as e:
        LOG.error("Parse failed for {}: {}", path, e)
        return False
    found = soup.find_all(tag)
    if not found:
        LOG.info("No <{}> found in {}", tag, path)
        return False
    for el in found:
        el.decompose()
    try:
        path.write_text(str(soup), encoding="utf-8")
    except OSError as e:
        LOG.error("Cannot write {}: {}", path, e)
        return False
    print(f"Removed <{tag}> from {path}")
    return True


def cmd_tag(args: argparse.Namespace) -> int:
    if not HAS_BS4:
        LOG.error("The 'tag' subcommand requires beautifulsoup4 (pip install beautifulsoup4).")
        return 2
    root = Path(args.directory)
    if not root.is_dir():
        LOG.error("Not a directory: {}", root)
        return 1
    exts = _normalize_exts(args.extensions, DEFAULT_TAG_EXTS)
    total = 0
    for f in _iter_files(root, exts):
        if _remove_tag_from_file(f, args.tagname):
            total += 1
    print(f"Done. Modified {total} file(s).")
    return 0


_META_RX_CACHE: dict[str, re.Pattern[str]] = {}


def _meta_rx(pattern: str) -> re.Pattern[str]:
    if pattern not in _META_RX_CACHE:
        _META_RX_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
    return _META_RX_CACHE[pattern]


def _remove_meta_from_file(path: Path, pattern: str) -> Optional[bool]:
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        LOG.error("Cannot read {}: {}", path, e)
        return None
    rx = _meta_rx(pattern)
    new_src = rx.sub("", src)
    if new_src == src:
        print(f"No meta tags found or removed in: {path}")
        return False
    try:
        path.write_text(new_src, encoding="utf-8")
    except OSError as e:
        LOG.error("Cannot write {}: {}", path, e)
        return None
    print(f"Removed meta tags from: {path}")
    return True


def cmd_meta(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    print(f"Starting to remove meta tags from HTML files in '{root}' and its subdirectories...\n")
    exts = _normalize_exts(args.extensions, DEFAULT_META_EXTS)
    total = 0
    for f in _iter_files(root, exts):
        if _remove_meta_from_file(f, args.pattern):
            total += 1
    print(f"\nFinished processing. Modified {total} file(s).")
    return 0


def _strip_all_tags(
    source: str,
    preserve_markers: Sequence[str],
) -> tuple[str, list[str]]:
    lines = source.split("\n")
    new_lines: list[str] = []
    removed: list[str] = []
    for line in lines:
        if any(marker in line for marker in preserve_markers):
            new_lines.append(line)
            continue
        cleaned = ALL_TAGS_REGEX.sub("", line)
        new_lines.append(cleaned)
        if cleaned != line:
            removed.append(line)
    return "\n".join(new_lines), removed


def cmd_all(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        LOG.error("Not a file: {}", path)
        return 1
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        LOG.error("Cannot read {}: {}", path, e)
        return 1
    markers = list(args.preserve_marker) if args.preserve_marker else list(DEFAULT_PRESERVE_MARKERS)
    new_source, removed = _strip_all_tags(source, markers)
    for line in removed:
        print(f"-{line}")
    if args.write:
        try:
            path.write_text(new_source, encoding="utf-8")
        except OSError as e:
            LOG.error("Cannot write {}: {}", path, e)
            return 1
        print(f"\nFile updated: {path}")
    else:
        print("\nFile not updated. Re-run with -w to write changes in place.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="htmlstrip.py",
        description="Unified HTML tag / meta tag remover.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tag", help="Remove a named tag using BeautifulSoup (remove_tag.py).")
    p.add_argument("tagname", help="Tag to remove (e.g. script, style, iframe)")
    p.add_argument("directory", nargs="?", default=".", help="Root directory (default: .)")
    p.add_argument(
        "--extensions",
        nargs="*",
        default=list(DEFAULT_TAG_EXTS),
        help=f"File extensions to process (default: {' '.join(DEFAULT_TAG_EXTS)})",
    )
    p.set_defaults(func=cmd_tag)
    p = sub.add_parser("meta", help="Remove <meta ...> tags via regex (rmeta.py).")
    p.add_argument("directory", nargs="?", default=".", help="Root directory (default: .)")
    p.add_argument(
        "--pattern",
        default=DEFAULT_META_PATTERN,
        help=f"Regex matching tags to remove (default: {DEFAULT_META_PATTERN})",
    )
    p.add_argument(
        "--extensions",
        nargs="*",
        default=list(DEFAULT_META_EXTS),
        help=f"File extensions to process (default: {' '.join(DEFAULT_META_EXTS)})",
    )
    p.set_defaults(func=cmd_meta)
    p = sub.add_parser("all", help="Strip every <...> tag from one file (strip_tags.py).")
    p.add_argument("file", help="File to process")
    p.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="Write changes in place (default: dry-run)",
    )
    p.add_argument(
        "--preserve-marker",
        action="append",
        default=None,
        help=("Substring; any line containing it is kept verbatim (repeatable; default: '<:' and '>:')"),
    )
    p.set_defaults(func=cmd_all)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        LOG.warning("Operation cancelled by user")
        return 130
    except Exception as e:
        LOG.opt(exception=True).error(
            "Unexpected error: {}",
            e,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
