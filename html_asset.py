#!/data/data/com.termux/files/usr/bin/env python
"""Extract inline CSS/JS from HTML files into external asset files.
Analysis / deduplication ------------------------ - ``excss.py`` / ``exjs.py`` (BeautifulSoup, dump every ``<style>`` / ``<script>`` body into randomly named files under ``css/`` / ``js/``, HTML left untouched) -> ``extract --mode dump``.
- ``htmlcleaner.py`` (stdlib parser, hash-named files under ``assets/css|js``, rewrites the HTML to link them) -> ``extract --mode rewrite`` (the new default: it is the only variant that keeps pages working).
Its global ``MIN_INLINE_SIZE`` hack is a normal parameter now, and its regex-based replacement was replaced by exact span splicing from parser positions, which cannot mismatch duplicate blocks.
Everything is stdlib (``html.parser``); BeautifulSoup is not needed.
Examples -------- python3 html_asset_tool.py extract site/ # rewrite+assets python3 html_asset_tool.py extract index.html --mode dump python3 html_asset_tool.py extract .
--assets-dir static --dry-run"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import html.parser
from pathlib import Path
import secrets
import sys


HTML_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".xhtml"})
MAX_FILE_BYTES: int = 50 * 1024 * 1024


@dataclass(slots=True)
class InlineBlock:
    """One inline <style>/<script> block with its source span."""

    kind: str  # "css" | "js"
    content: str
    attrs: dict[str, str | None]
    start: int  # offset of "<style"/"<script"
    end: int  # offset just past "</style>"/"</script>"


@dataclass(slots=True)
class FileResult:
    """Counts from processing one HTML file."""

    path: Path
    css: int = 0
    js: int = 0
    error: str = ""


class InlineParser(html.parser.HTMLParser):
    """Collect inline style/script blocks with exact character spans."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source: str = source
        self._line_starts: list[int] = [0] + [i + 1 for i, ch in enumerate(source) if ch == "\n"]
        self.blocks: list[InlineBlock] = []
        self._open_kind: str | None = None
        self._open_attrs: dict[str, str | None] = {}
        self._open_start: int = 0
        self._chunks: list[str] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map: dict[str, str | None] = dict(attrs)
        if tag == "style":
            self._begin("css", attr_map)
        elif tag == "script" and "src" not in attr_map:
            self._begin("js", attr_map)

    def _begin(self, kind: str, attrs: dict[str, str | None]) -> None:
        self._open_kind = kind
        self._open_attrs = attrs
        self._open_start = self._offset()
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._open_kind is not None:
            self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._open_kind == "js":
            self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._open_kind == "js":
            self._chunks.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        expected: str | None = {"style": "css", "script": "js"}.get(tag)
        if expected is None or expected != self._open_kind:
            return
        close_start: int = self._offset()
        close_end: int = self._source.find(">", close_start) + 1
        content: str = "".join(self._chunks).strip()
        if content:
            self.blocks.append(InlineBlock(expected, content, self._open_attrs, self._open_start, close_end))
        self._open_kind = None
        self._chunks = []


def parse_blocks(source: str, min_size: int) -> list[InlineBlock]:
    """Return inline blocks of at least *min_size* characters."""
    parser = InlineParser(source)
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return []
    return [b for b in parser.blocks if len(b.content) >= min_size]


def attrs_text(attrs: dict[str, str | None], skip: set[str]) -> str:
    """Render leftover HTML attributes for a replacement tag."""
    parts: list[str] = []
    for key, value in attrs.items():
        if key in skip:
            continue
        parts.append(key if value is None else f'{key}="{value}"')
    return (" " + " ".join(parts)) if parts else ""


def unique_file(directory: Path, stem: str, suffix: str) -> Path:
    """First free ``stem[.N]suffix`` path inside *directory*."""
    candidate: Path = directory / f"{stem}{suffix}"
    counter: int = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def process_rewrite(path: Path, assets_name: str, min_size: int, dry_run: bool) -> FileResult:
    """Extract blocks to hash-named files and link them from the HTML."""
    source: str = path.read_text(encoding="utf-8", errors="replace")
    blocks: list[InlineBlock] = parse_blocks(source, min_size)
    if not blocks:
        return FileResult(path)
    result = FileResult(path)
    replacements: list[tuple[int, int, str]] = []
    for block in blocks:
        digest: str = hashlib.sha256(block.content.encode()).hexdigest()[:12]
        subdir: str = "css" if block.kind == "css" else "js"
        target_dir: Path = path.parent / assets_name / subdir
        suffix: str = ".css" if block.kind == "css" else ".js"
        target: Path = target_dir / f"{path.stem}_{digest}{suffix}"
        if target.exists():
            target = unique_file(target_dir, f"{path.stem}_{digest}", suffix)
        href: str = f"{assets_name}/{subdir}/{target.name}"
        if block.kind == "css":
            tag: str = f'<link rel="stylesheet" href="{href}"{attrs_text(block.attrs, {"href", "rel"})}>'
            result.css += 1
        else:
            tag = f'<script src="{href}"{attrs_text(block.attrs, {"src"})}></script>'
            result.js += 1
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(block.content + "\n", encoding="utf-8")
        replacements.append((block.start, block.end, tag))
    if not dry_run:
        rewritten: str = source
        for start, end, tag in sorted(replacements, reverse=True):
            rewritten = rewritten[:start] + tag + rewritten[end:]
        tmp: Path = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(rewritten, encoding="utf-8")
        tmp.replace(path)
    return result


def process_dump(path: Path, out_root: Path, min_size: int, dry_run: bool) -> FileResult:
    """Dump block bodies into css/ / js/ without touching the HTML."""
    source: str = path.read_text(encoding="utf-8", errors="replace")
    result = FileResult(path)
    for block in parse_blocks(source, min_size):
        directory: Path = out_root / ("css" if block.kind == "css" else "js")
        suffix: str = ".css" if block.kind == "css" else ".js"
        if not dry_run:
            directory.mkdir(parents=True, exist_ok=True)
            target: Path = unique_file(directory, secrets.token_hex(5), suffix)
            target.write_text(block.content + "\n", encoding="utf-8")
        if block.kind == "css":
            result.css += 1
        else:
            result.js += 1
    return result


def process_one(job: tuple[Path, str, str, int, bool]) -> FileResult:
    """Dispatch one file to the chosen mode (pool-worker friendly)."""
    path, mode, assets_name, min_size, dry_run = job
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return FileResult(path, error="file too large (>50 MiB)")
        if mode == "dump":
            return process_dump(path, Path.cwd(), min_size, dry_run)
        return process_rewrite(path, assets_name, min_size, dry_run)
    except OSError as exc:
        return FileResult(path, error=str(exc))


def find_html_files(inputs: Sequence[Path]) -> list[Path]:
    """Collect HTML files from file/dir arguments."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob("*") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and path.suffix.lower() in HTML_SUFFIXES and path.stat().st_size > 0:
                found.add(path.resolve())
    return sorted(found)


def cmd_extract(args: argparse.Namespace) -> int:
    """Run the ``extract`` subcommand."""
    files: list[Path] = find_html_files(args.paths)
    if not files:
        print("No HTML files found.")
        return 0
    jobs: list[tuple[Path, str, str, int, bool]] = [
        (p, args.mode, args.assets_dir, args.min_size, args.dry_run) for p in files
    ]
    totals = FileResult(Path())
    errors: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for result in pool.map(process_one, jobs):
            if result.error:
                errors += 1
                print(f"error: {result.path}: {result.error}", file=sys.stderr)
                continue
            totals.css += result.css
            totals.js += result.js
            if not args.quiet and (result.css or result.js):
                verb: str = "would extract" if args.dry_run else "extracted"
                print(f"{result.path}: {verb} {result.css} CSS, {result.js} JS")
    print(f"Summary: {len(files)} file(s), {totals.css} CSS, {totals.js} JS, {errors} error(s).")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Extract inline CSS/JS from HTML.")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="Extract inline <style>/<script> blocks")
    ex.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: .)")
    ex.add_argument(
        "--mode",
        choices=("rewrite", "dump"),
        default="rewrite",
        help="rewrite: assets/ + link tags; dump: css//js/ only",
    )
    ex.add_argument("--assets-dir", default="assets", help="Assets dir name (rewrite mode)")
    ex.add_argument("--min-size", type=int, default=0, help="Skip blocks shorter than N chars")
    ex.add_argument("-w", "--workers", type=int, default=8)
    ex.add_argument("-q", "--quiet", action="store_true")
    ex.add_argument("-n", "--dry-run", action="store_true")
    ex.set_defaults(handler=cmd_extract)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
