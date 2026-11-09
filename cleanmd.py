#!/data/data/com.termux/files/usr/bin/env python
"""Remove the image references from Markdown, RST and HTML files.
Deletes the inline images, the reference-style images and the linked badges (``--badges``), optionally only the remote ones, the unused link definitions and the surrounding whitespace; ``--backup``, ``--html-parser`` and ``--report`` control the behaviour.
Usage: cleanmd [options] [paths ...] Positional arguments: paths ...
Files or directories to scan (default: current directory) Options: --ext File extensions to process (default: .md .markdown .rst .html .htm) --remote-only Only remove remote image references (http://, https://, //) --badges Also strip badge / shield link blocks --aggressive-defs Remove every link definition line, even non-image ones --backup Write a .bak copy before modifying each file --html-parser HTML parsing backend (default: regex) --report Output verbosity (default: simple) -v, --verbose Examples: $ cleanmd --remote-only docs/ Command-line script: run ``python cleanmd.py`` or the ``cleanmd`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger


try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
log = logger
_REMOTE_PREFIXES: tuple[str, str, str] = ("http://", "https://", "//")
_BADGE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"shields\.io",
        r"img\.shields\.io",
        r"badge\.fury\.io",
        r"badges\.gitter\.im",
        r"travis-ci\.org",
        r"travis-ci\.com",
        r"circleci\.com",
        r"codecov\.io",
        r"coveralls\.io",
        r"readthedocs\.org",
        r"readthedocs\.io",
        r"github\.com/.*/workflows/.*badge",
        r"ci\.appveyor\.com",
        r"dev\.azure\.com",
        r"scrutinizer-ci\.com",
        r"packagist\.org",
        r"david-dm\.org",
        r"snyk\.io",
        r"badges\.greenkeeper\.io",
        r"api\.codacy\.com",
        r"goreportcard\.com",
        r"opencollective\.com",
        r"buymeacoffee\.com",
        r"patreon\.com",
    )
)
_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp")
RE_MD_BADGE = re.compile(r"\[!\[[^\]]*\]\(([^\)]+)\)\]\(([^\)]+)\)")
RE_MD_INLINE = re.compile(r"!\[([^\[\]]*)\]\(([^\)]+)\)")
RE_MD_REF_IMG = re.compile(r"!\[([^\[\]]*)\]\[([^\[\]]+)\]")
RE_MD_IMAGE_DEF = re.compile(
    r'^\s*\[([^\[\]]+)\]:\s*(\S+)(?:\s+["\'][^"\']*["\'])?\s*$',
    re.MULTILINE,
)
RE_HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
RE_HTML_PICTURE = re.compile(r"<picture\b[^>]*>.*?</picture>", re.DOTALL | re.IGNORECASE)
RE_HTML_FIGURE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.DOTALL | re.IGNORECASE)
RE_HTML_IMG_SRC = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
RE_RST_IMAGE = re.compile(r"^\s*\.\.\s+image::\s+(\S+)")
RE_RST_FIGURE = re.compile(r"^\s*\.\.\s+figure::\s+(\S+)")
RE_RST_SUBST_IMAGE = re.compile(r"^\s*\.\.\s+\|[^|]+\|\s+image::\s+(\S+)")
RE_RST_SUBST_REPLACE = re.compile(
    r"^\s*\.\.\s+\|[^|]+\|\s+replace::\s+(\S+\.(?:png|jpg|jpeg|gif|svg|ico|webp|bmp))",
    re.IGNORECASE,
)
_RST_PATTERNS: tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]] = (
    RE_RST_IMAGE,
    RE_RST_FIGURE,
    RE_RST_SUBST_IMAGE,
    RE_RST_SUBST_REPLACE,
)
_RE_BLANKLINES = re.compile(r"\n{3,}")


@dataclass
class Result:
    path: Path
    removed_refs: int
    lines_before: int
    lines_after: int
    size_before: int
    size_after: int
    error: Optional[str] = None


def is_remote(url: str) -> bool:
    return url.startswith(_REMOTE_PREFIXES)


def is_badge(url: str) -> bool:
    return any(p.search(url) for p in _BADGE_PATTERNS)


def is_image_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(_IMAGE_EXTS)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _finalize(text: str, ends_nl: bool) -> str:
    text = _RE_BLANKLINES.sub("\n\n", text)
    if ends_nl and not text.endswith("\n"):
        text += "\n"
    return text


def process_markdown(
    text: str,
    remote_only: bool,
    badges: bool,
    aggressive_defs: bool,
) -> tuple[str, int]:
    removed = 0
    ends_nl = text.endswith("\n")
    if badges:

        def repl_badge(m: re.Match[str]) -> str:
            nonlocal removed
            img_url, link_url = m.group(1), m.group(2)
            if remote_only and not (is_remote(img_url) or is_remote(link_url)):
                return m.group(0)
            if is_badge(img_url) or is_badge(link_url):
                removed += 1
                return ""
            return m.group(0)

        text = RE_MD_BADGE.sub(repl_badge, text)
    remote_defs: set[str] = set()
    if remote_only:
        for m in RE_MD_IMAGE_DEF.finditer(text):
            if is_remote(m.group(2)):
                remote_defs.add(m.group(1))

    def repl_inline(m: re.Match[str]) -> str:
        nonlocal removed
        if remote_only and not is_remote(m.group(2)):
            return m.group(0)
        removed += 1
        return ""

    text = RE_MD_INLINE.sub(repl_inline, text)

    def repl_ref(m: re.Match[str]) -> str:
        nonlocal removed
        if remote_only and m.group(2) not in remote_defs:
            return m.group(0)
        removed += 1
        return ""

    text = RE_MD_REF_IMG.sub(repl_ref, text)

    def repl_def(m: re.Match[str]) -> str:
        nonlocal removed
        url = m.group(2)
        if remote_only:
            if is_remote(url):
                removed += 1
                return ""
            return m.group(0)
        if aggressive_defs or is_image_url(url):
            removed += 1
            return ""
        return m.group(0)

    text = RE_MD_IMAGE_DEF.sub(repl_def, text)

    def repl_img(m: re.Match[str]) -> str:
        nonlocal removed
        tag = m.group(0)
        if remote_only:
            sm = RE_HTML_IMG_SRC.search(tag)
            if sm and not is_remote(sm.group(1)):
                return tag
        removed += 1
        return ""

    text = RE_HTML_IMG.sub(repl_img, text)
    if not remote_only:

        def repl_block(m: re.Match[str]) -> str:
            nonlocal removed
            removed += 1
            return ""

        text = RE_HTML_PICTURE.sub(repl_block, text)
        text = RE_HTML_FIGURE.sub(repl_block, text)
    return _finalize(text, ends_nl), removed


def process_rst(text: str, remote_only: bool) -> tuple[str, int]:
    lines = text.split("\n")
    out: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        matched: Optional[re.Match[str]] = None
        for pat in _RST_PATTERNS:
            matched = pat.match(line)
            if matched:
                break
        if matched is not None:
            url = matched.group(1)
            if remote_only and not is_remote(url):
                out.append(line)
            else:
                removed += 1
                j = i + 1
                while j < len(lines) and lines[j].strip().startswith(":"):
                    j += 1
                i = j
                continue
        else:
            out.append(line)
        i += 1
    return _finalize("\n".join(out), text.endswith("\n")), removed


def _process_html_regex(text: str, remote_only: bool) -> tuple[str, int]:
    removed = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal removed
        tag = m.group(0)
        if remote_only:
            sm = RE_HTML_IMG_SRC.search(tag)
            if sm and not is_remote(sm.group(1)):
                return tag
        removed += 1
        return ""

    return RE_HTML_IMG.sub(repl, text), removed


def _process_html_bs4(text: str, remote_only: bool) -> tuple[str, int]:
    soup = BeautifulSoup(text, "html.parser")
    removed = 0
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if isinstance(src, list):
            src = src[0] if src else ""
        if remote_only and src and not is_remote(src):
            continue
        img.decompose()
        removed += 1
    for tag in soup.find_all(style=True):
        parts = [p.strip() for p in tag["style"].split(";") if p.strip()]
        kept: list[str] = []
        for part in parts:
            if "background-image" not in part.lower():
                kept.append(part)
                continue
            m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", part)
            if m and remote_only and not is_remote(m.group(1)):
                kept.append(part)
                continue
            removed += 1
        if kept:
            tag["style"] = "; ".join(kept)
        else:
            del tag["style"]
    return str(soup), removed


def process_html(text: str, remote_only: bool, html_parser: str) -> tuple[str, int]:
    if html_parser == "bs4":
        if not _HAS_BS4:
            msg = "beautifulsoup4 is required for --html-parser bs4"
            raise RuntimeError(msg)
        return _process_html_bs4(text, remote_only)
    return _process_html_regex(text, remote_only)


def process_file(
    path: Path,
    remote_only: bool,
    badges: bool,
    aggressive_defs: bool,
    html_parser: str,
    backup: bool,
) -> Result:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return Result(path, 0, 0, 0, 0, 0, str(e))
    lines_before = text.count("\n") + 1
    size_before = len(text.encode("utf-8"))
    ext = path.suffix.lower()
    try:
        if ext in (".md", ".markdown"):
            new_text, removed = process_markdown(text, remote_only, badges, aggressive_defs)
        elif ext in (".rst", ".txt"):
            new_text, removed = process_rst(text, remote_only)
        elif ext in (".html", ".htm"):
            new_text, removed = process_html(text, remote_only, html_parser)
        else:
            return Result(path, 0, lines_before, lines_before, size_before, size_before)
    except Exception as e:
        return Result(path, 0, lines_before, lines_before, size_before, size_before, str(e))
    if removed == 0:
        return Result(path, 0, lines_before, lines_before, size_before, size_before)
    try:
        if backup:
            path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return Result(path, 0, lines_before, lines_before, size_before, size_before, str(e))
    lines_after = new_text.count("\n") + 1
    size_after = len(new_text.encode("utf-8"))
    return Result(path, removed, lines_before, lines_after, size_before, size_after)


def discover_files(paths: Sequence[Path], exts: Sequence[str]) -> list[Path]:
    norm = {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
    found: set[Path] = set()
    for p in paths:
        if p.is_file():
            if p.suffix.lower() in norm:
                found.add(p.resolve())
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in norm:
                    found.add(f.resolve())
        else:
            log.warning("Path does not exist: {}", p)
    return sorted(found)


def print_result(r: Result, report: str) -> None:
    if r.error:
        print(f"ERROR: {r.path}: {r.error}")
        return
    if r.removed_refs > 0:
        print(f"Updated: {r.path} ({r.removed_refs} removed)")
    elif report == "simple":
        print(f"Skipped (no changes): {r.path}")


def print_summary(results: Sequence[Result]) -> None:
    modified = [r for r in results if r.removed_refs > 0]
    errors = [r for r in results if r.error]
    total_removed = sum(r.removed_refs for r in modified)
    lb = sum(r.lines_before for r in results)
    la = sum(r.lines_after for r in results)
    sb = sum(r.size_before for r in results)
    sa = sum(r.size_after for r in results)
    print("=" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Files modified: {len(modified)}")
    if errors:
        print(f"Errors: {len(errors)}")
    print(f"Total image references removed: {total_removed}")
    print(f"Total lines: {lb} -> {la} ({lb - la:+d})")
    print(f"Total size: {fmt_size(sb)} -> {fmt_size(sa)} ({fmt_size(sb - sa)} saved)")
    if sb > 0:
        print(f"Overall reduction: {(sb - sa) / sb * 100:.1f}%")
    print("-" * 40)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remove_images",
        description="Remove image references from Markdown / RST / HTML files.",
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to scan (default: current directory)",
    )
    p.add_argument(
        "--ext",
        nargs="+",
        default=None,
        help="File extensions to process (default: .md .markdown .rst .html .htm)",
    )
    p.add_argument(
        "--remote-only",
        action="store_true",
        help="Only remove remote image references (http://, https://, //)",
    )
    p.add_argument("--badges", action="store_true", help="Also strip badge / shield link blocks")
    p.add_argument(
        "--aggressive-defs",
        action="store_true",
        help="Remove every link definition line, even non-image ones",
    )
    p.add_argument(
        "--backup",
        action="store_true",
        help="Write a .bak copy before modifying each file",
    )
    p.add_argument(
        "--html-parser",
        choices=("regex", "bs4"),
        default="regex",
        help="HTML parsing backend (default: regex)",
    )
    p.add_argument(
        "--report",
        choices=("none", "simple", "detailed"),
        default="simple",
        help="Output verbosity (default: simple)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    exts = args.ext or [".md", ".markdown", ".rst", ".html", ".htm"]
    paths = args.paths or [Path.cwd()]
    files = discover_files(paths, exts)
    if not files:
        print("No matching files found.")
        return 0
    if args.report != "none":
        print(f"Processing {len(files)} file(s) with {MAX_WORKERS} worker(s)...")
    results: list[Result] = []
    try:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {
                ex.submit(
                    process_file,
                    f,
                    args.remote_only,
                    args.badges,
                    args.aggressive_defs,
                    args.html_parser,
                    args.backup,
                ): f
                for f in files
            }
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception as e:
                    log.error("Worker failed: {}", e)
                    continue
                results.append(r)
                if args.report != "none":
                    print_result(r, args.report)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1
    if args.report == "detailed":
        print_summary(results)
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
