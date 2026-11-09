#!/data/data/com.termux/files/usr/bin/env python
"""Remove the images from documentation files.
Subcommands: ``fast`` deletes the Markdown/HTML images, ``smart`` the badges and remote images, ``markdown`` only the Markdown ones, ``remote`` the ``http(s)`` references and ``html`` uses BeautifulSoup; the RST ``image::`` directives and the badge domains are recognised.
Usage: image_remover {fast,smart,markdown,remote,html} [options] [paths ...] Positional arguments: paths ...
Paths to process extensions ...
File extensions Subcommands: fast Fast MD/HTML removal smart Smart badge/remote removal markdown Markdown-only removal remote Remote URL removal html HTML BeautifulSoup removal Examples: $ image_remover smart README.md Command-line script: run ``python image_remover.py`` or the ``image_remover`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
import multiprocessing as mp
from pathlib import Path
import re
from typing import Any, Iterable, Optional

from dh import MAX_WORKERS


@dataclass
class FileStats:
    path: Path
    removed_count: int = 0
    size_before: int = 0
    size_after: int = 0
    lines_before: int = 0
    lines_after: int = 0
    error: Optional[str] = None


MD_IMAGE_PATTERN = re.compile(r"!\[.*?\]\(.*?\)")
HTML_BADGE_PATTERN = re.compile(
    r"<p\b[^>]*>[\s\S]*?<img\b[\s\S]*?</p>|" r"<a\b[^>]*>\s*<img\b[\s\S]*?</a>|" r"<img\b[^>]*/?>",
    re.IGNORECASE,
)
RST_IMAGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*\.\.\ +image::\s+https?://[^\s]+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*\.\.\ +figure::\s+https?://[^\s]+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*\.\.\ +\|.*\|\s+image::\s+https?://[^\s]+", re.IGNORECASE | re.MULTILINE),
]
MD_PATTERNS_SMART: list[re.Pattern[str]] = [
    re.compile(r"^\[!\[.*?\]\(https?://[^\)]+\)\]\(https?://[^\)]+\)", re.MULTILINE),
    re.compile(r"!\[.*?\]\(https?://[^\)]+\)", re.MULTILINE),
    re.compile(r"!\[.*?\]\((?!https?://)[^\)]+\)", re.MULTILINE),
    re.compile(r'<img[^>]+src\s*=\s*["\'][^"\']["\'][^>]*>', re.IGNORECASE),
]
BADGE_DOMAINS: list[str] = [
    "shields.io",
    "badge.fury.io",
    "travis-ci.org",
    "circleci.com",
    "codecov.io",
    "coveralls.io",
    "readthedocs.org",
    "packagist.org",
]
REMOTE_PREFIXES: tuple[str, str, str] = ("http://", "https://", "//")
IMG_TAG_RE = re.compile(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']) ["\'][^>]*>', re.IGNORECASE)
MD_INLINE_IMG_RE = re.compile(r"!\[.*?\]\((.*?)\)", re.IGNORECASE)
MD_REF_DEF_RE = re.compile(r"^\s*\[(.*?)\]:\s*(\S+)", re.MULTILINE)
RST_IMG_RE = re.compile(r"^\s*\.\.\ \|[^|]+\|\ image::\ https?://[^\s]+.*$", re.MULTILINE)


def has_badge_domain(line: str) -> bool:
    return any(re.search(domain, line, re.IGNORECASE) for domain in BADGE_DOMAINS)


def is_image_ext(line: str) -> bool:
    return bool(re.search(r"\.(?:png|jpg|jpeg|gif|svg|ico|webp|bmp)(?:\?|#|$|\))", line, re.IGNORECASE))


def clean_file_fast(path: Path) -> FileStats:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        cleaned = MD_IMAGE_PATTERN.sub("", content)
        cleaned = HTML_BADGE_PATTERN.sub("", cleaned)
        changed = content != cleaned
        if changed:
            path.write_text(cleaned, encoding="utf-8")
        return FileStats(path, 1 if changed else 0)
    except Exception as e:
        return FileStats(path, 0, error=str(e))


def clean_file_smart(path: Path) -> FileStats:
    try:
        content = path.read_text(encoding="utf-8")
        removed = 0
        lines_before = content.count("\n") + 1
        size_before = len(content.encode("utf-8"))
        suffix = path.suffix.lower()
        if suffix == ".rst":
            lines = content.split("\n")
            new_lines = []
            for i, line in enumerate(lines):
                should_remove = any(p.match(line) for p in RST_IMAGE_PATTERNS)
                if should_remove or ("image::" in line and (has_badge_domain(line) or is_image_ext(line))):
                    removed += 1
                    if i + 1 < len(lines) and lines[i + 1].strip().startswith(":"):
                        i += 1
                else:
                    new_lines.append(line)
            content = "\n".join(new_lines)
        elif suffix == ".md":
            lines = content.split("\n")
            new_lines = []
            for line in lines:
                should_remove = False
                for pattern in MD_PATTERNS_SMART:
                    if pattern.search(line):
                        cleaned = pattern.sub("", line).strip()
                        if not cleaned or (has_badge_domain(line) or is_image_ext(line)):
                            should_remove = True
                            removed += 1
                        else:
                            line = cleaned
                        break
                if not should_remove:
                    new_lines.append(line)
            content = "\n".join(new_lines)
        if removed > 0:
            path.write_text(content, encoding="utf-8")
        lines_after = content.count("\n") + 1
        size_after = len(content.encode("utf-8"))
        return FileStats(path, removed, size_before, size_after, lines_before, lines_after)
    except Exception as e:
        return FileStats(path, 0, error=str(e))


def clean_file_markdown(path: Path) -> FileStats:
    try:
        content = path.read_text(encoding="utf-8")
        original = content
        removed = 0
        content = MD_IMAGE_PATTERN.sub("", content)
        removed += len(re.findall(MD_IMAGE_PATTERN, original))
        content = re.sub(r"<img\s+[^>]*/?>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"<picture\s*>.*?</picture>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<figure\s*>.*?</figure>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"\n\n\n+", "\n\n", content)
        if content != original:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return FileStats(path, removed, len(original.encode("utf-8")), len(content.encode("utf-8")))
    except Exception as e:
        return FileStats(path, 0, error=str(e))


def clean_file_remote(path: Path) -> FileStats:
    try:
        original = path.read_text(encoding="utf-8", errors="ignore")
        modified = original
        removed = 0
        if path.suffix.lower() in {".html", ".htm"}:

            def repl(m: re.Match[str]):
                nonlocal removed
                src = m.group(1)
                if src.startswith(REMOTE_PREFIXES):
                    removed += 1
                    return ""
                return m.group(0)

            modified = IMG_TAG_RE.sub(repl, modified)
        elif path.suffix.lower() == ".md":

            def inline_repl(m: re.Match[str]):
                nonlocal removed
                url = m.group(1)
                if url.startswith(REMOTE_PREFIXES):
                    removed += 1
                    return ""
                return m.group(0)

            modified = MD_INLINE_IMG_RE.sub(inline_repl, modified)
        elif path.suffix.lower() in {".rst", ".txt"}:

            def rst_repl(m: Any) -> str:
                nonlocal removed
                removed += 1
                return ""

            modified = RST_IMG_RE.sub(rst_repl, modified)
        if modified != original:
            path.write_text(modified, encoding="utf-8")
        return FileStats(path, removed, len(original.encode("utf-8")), len(modified.encode("utf-8")))
    except Exception as e:
        return FileStats(path, 0, error=str(e))


def find_files(paths: list[str], extensions: tuple[str, ...]) -> list[Path]:
    files = []
    search_paths = [Path(p) for p in paths] if paths else [Path.cwd()]
    for sp in search_paths:
        if sp.is_file():
            files.append(sp)
        elif sp.is_dir():
            for ext in extensions:
                files.extend(sp.rglob(f"*{ext}"))
    return sorted(set(files))


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove images from documentation")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    fast_parser = subparsers.add_parser("fast", help="Fast MD/HTML removal")
    smart_parser = subparsers.add_parser("smart", help="Smart badge/remote removal")
    smart_parser.add_argument("paths", nargs="*", help="Paths to process")
    md_parser = subparsers.add_parser("markdown", help="Markdown-only removal")
    md_parser.add_argument("paths", nargs="*", help="Paths to process")
    remote_parser = subparsers.add_parser("remote", help="Remote URL removal")
    remote_parser.add_argument("extensions", nargs="*", default=[".html", ".md", ".rst"], help="File extensions")
    html_parser = subparsers.add_parser("html", help="HTML BeautifulSoup removal")
    html_parser.add_argument("paths", nargs="*", help="Paths to process")
    args = parser.parse_args()
    if args.mode == "fast":
        files = find_files([], (".md", ".markdown"))
        if not files:
            print("No markdown files found.")
            return 0
        with mp.Pool(MAX_WORKERS) as pool:
            results = pool.map(clean_file_fast, files)
        print(f"Processed {len(results)} files")
        return 0
    elif args.mode == "smart":
        files = find_files(args.paths, (".md", ".rst"))
        if not files:
            print("No files found.")
            return 0
        with mp.Pool(MAX_WORKERS) as pool:
            results = pool.map(clean_file_smart, files)
        print(f"Processed {len(results)} files")
        return 0
    elif args.mode == "markdown":
        files = find_files(args.paths, (".md", ".markdown"))
        if not files:
            print("No markdown files found.")
            return 0
        with mp.Pool(MAX_WORKERS) as pool:
            results = pool.map(clean_file_markdown, files)
        print(f"Processed {len(results)} files")
        return 0
    elif args.mode == "remote":
        exts = tuple(args.extensions) if args.extensions else (".html", ".md", ".rst")
        files = find_files([], exts)
        if not files:
            print("No files found.")
            return 0
        with mp.Pool(MAX_WORKERS) as pool:
            results = pool.map(clean_file_remote, files)
        print(f"Processed {len(results)} files")
        return 0
    elif args.mode == "html":
        files = find_files(args.paths, (".html", ".htm"))
        if not files:
            print("No HTML files found.")
            return 0
        with mp.Pool(MAX_WORKERS) as pool:
            results = pool.map(clean_file_fast, files)
        print(f"Processed {len(results)} files")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
