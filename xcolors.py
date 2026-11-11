#!/data/data/com.termux/files/usr/bin/env python
"""Extract CSS hex colors (#RGB, #RGBA, #RRGGBB, #RRGGBBAA) from files."""

import argparse
from collections.abc import Iterator
import contextlib
import csv
from dataclasses import dataclass, field
import fnmatch
import html
import json
from multiprocessing import get_context
from pathlib import Path
import re
import sys
from typing import Final, TextIO

from dh import MAX_WORKERS
from loguru import logger


__version__: Final[str] = "2.0.0"
READ_SIZE: Final[int] = 1 << 20
BINARY_PROBE: Final[int] = 8192
MAX_HEX_DIGITS: Final[int] = 8
CARRY_SIZE: Final[int] = MAX_HEX_DIGITS + 1
COLOR_RE: Final[re.Pattern[str]] = re.compile(
    r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{3})(?![0-9A-Fa-f])"
)
FORMATS: Final[frozenset[str]] = frozenset({"text", "json", "jsonl", "csv", "tsv", "md", "html"})
HTML_HEAD: Final[str] = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Colors</title>"
    "<style>"
    "body{font-family:ui-monospace,monospace;padding:1em;}"
    ".sw{display:inline-block;width:1.5em;height:1em;vertical-align:middle;"
    "border:1px solid #bbb;margin-right:0.4em;border-radius:2px;}"
    "section{margin:1em 0;}"
    "h3{font-size:1em;margin:0.3em 0;}"
    "ul{margin:0;padding-left:0;list-style:none;}"
    "li{margin:0.15em 0;}"
    "</style></head><body>"
)
HTML_FOOT: Final[str] = "</body></html>\n"


@dataclass(frozen=True, slots=True)
class ScanConfig:
    dedupe: bool
    no_alpha: bool


@dataclass(frozen=True, slots=True)
class FileResult:
    path: Path
    counts: dict[str, int] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Options:
    dedupe: bool
    no_alpha: bool
    extensions: frozenset[str]
    excludes: tuple[str, ...]
    skip_binary: bool
    max_size: int | None
    fmt: str
    show_counts: bool
    swatches: bool
    sort_by: str
    top: int | None
    min_count: int
    include_empty: bool
    unique: bool


@dataclass(slots=True)
class Summary:
    files: int = 0
    with_colors: int = 0
    total_colors: int = 0
    failures: int = 0
    unique: set[str] = field(default_factory=set)

    def render(self) -> str:
        fail = f" ({self.failures} failures)" if self.failures else ""
        return (
            f"scanned {self.files} files{fail}, "
            f"{self.with_colors} with colors, "
            f"{self.total_colors} occurrences, "
            f"{len(self.unique)} unique colors"
        )


def _normalize(raw: str, config: ScanConfig) -> str | None:
    digits = raw[1:].lower()
    if config.no_alpha and len(digits) in (4, 8):
        return None
    if not config.dedupe:
        return "#" + digits
    if len(digits) in (3, 4):
        digits = "".join(c + c for c in digits)
    if len(digits) == 8 and digits[6:] == "ff":
        digits = digits[:6]
    return "#" + digits


def _extract(path: Path, config: ScanConfig) -> dict[str, int]:
    counts: dict[str, int] = {}
    carry = ""
    with path.open("rb") as handle:
        raw = handle.read(READ_SIZE)
        while raw:
            following = handle.read(READ_SIZE)
            is_final = not following
            text = carry + raw.decode("utf-8", "ignore")
            carry_len = len(carry)
            limit = len(text)
            for match in COLOR_RE.finditer(text):
                end = match.end()
                if end < carry_len:
                    continue
                if not is_final and end == limit:
                    continue
                normalized = _normalize(match.group(0), config)
                if normalized is None:
                    continue
                counts[normalized] = counts.get(normalized, 0) + 1
            carry = text[-CARRY_SIZE:]
            raw = following
    return counts


def process_file(job: tuple[Path, ScanConfig]) -> FileResult:
    path, config = job
    try:
        return FileResult(path=path, counts=_extract(path, config), error=None)
    except Exception as exc:
        return FileResult(path=path, counts=None, error=f"{type(exc).__name__}: {exc}")


def _is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\x00" in handle.read(BINARY_PROBE)


def _is_excluded(path: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    parts = path.parts
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern):
                return True
        elif pattern in parts:
            return True
    return False


def _matches_extensions(path: Path, extensions: frozenset[str]) -> bool:
    if not extensions:
        return True
    return path.suffix.lower().lstrip(".") in extensions


def collect_files(sources: list[Path], opts: Options) -> list[Path]:
    files: list[Path] = []
    for source in sources:
        candidates: Iterator[Path]
        if source.is_dir():
            candidates = (child for child in source.rglob("*") if child.is_file())
        else:
            candidates = iter([source])
        for candidate in candidates:
            if _is_excluded(candidate, opts.excludes):
                continue
            if not _matches_extensions(candidate, opts.extensions):
                continue
            try:
                if opts.max_size is not None and candidate.stat().st_size > opts.max_size:
                    continue
                if opts.skip_binary and _is_binary(candidate):
                    continue
            except OSError as exc:
                logger.error("Cannot stat {}: {}", candidate, exc)
                continue
            files.append(candidate)
    return files


def _sort_items(counts: dict[str, int], sort_by: str) -> list[tuple[str, int]]:
    if sort_by == "count":
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return sorted(counts.items(), key=lambda kv: kv[0])


def _apply_filters(counts: dict[str, int], opts: Options) -> dict[str, int]:
    filtered = {c: n for c, n in counts.items() if n >= opts.min_count}
    if opts.top is not None and len(filtered) > opts.top:
        top = sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))[: opts.top]
        filtered = dict(top)
    return filtered


def _swatch(color: str) -> str:
    digits = color[1:]
    if len(digits) in (3, 4):
        digits = "".join(c + c for c in digits)
    r = int(digits[0:2], 16)
    g = int(digits[2:4], 16)
    b = int(digits[4:6], 16)
    return f"\x1b[48;2;{r};{g};{b}m   \x1b[0m"


def _stream_records(
    results: Iterator[FileResult],
    opts: Options,
    summary: Summary,
) -> Iterator[tuple[Path, list[tuple[str, int]]]]:
    for result in results:
        if result.error is not None:
            summary.failures += 1
            logger.error("Failed to process {}: {}", result.path, result.error)
            continue
        summary.files += 1
        counts = result.counts or {}
        filtered = _apply_filters(counts, opts)
        if filtered:
            summary.with_colors += 1
            summary.total_colors += sum(filtered.values())
            summary.unique.update(filtered.keys())
        if not filtered and not opts.include_empty:
            continue
        yield result.path, _sort_items(filtered, opts.sort_by)


def _aggregate_unique(
    results: Iterator[FileResult],
    opts: Options,
    summary: Summary,
) -> list[tuple[str, int, int]]:
    totals: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    for result in results:
        if result.error is not None:
            summary.failures += 1
            logger.error("Failed to process {}: {}", result.path, result.error)
            continue
        summary.files += 1
        counts = result.counts or {}
        if counts:
            summary.with_colors += 1
            summary.total_colors += sum(counts.values())
            summary.unique.update(counts.keys())
        for color, count in counts.items():
            totals[color] = totals.get(color, 0) + count
            file_counts[color] = file_counts.get(color, 0) + 1
    filtered = {c: n for c, n in totals.items() if n >= opts.min_count}
    if opts.top is not None and len(filtered) > opts.top:
        top = sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))[: opts.top]
        filtered = dict(top)
    ordered = _sort_items(filtered, opts.sort_by)
    return [(color, count, file_counts[color]) for color, count in ordered]


def _emit_stream(
    out: TextIO,
    records: Iterator[tuple[Path, list[tuple[str, int]]]],
    opts: Options,
) -> None:
    if opts.fmt == "text":
        for path, items in records:
            if not items:
                out.write(f"{path}\n")
                continue
            parts: list[str] = []
            for color, count in items:
                token = f"{color}:{count}" if opts.show_counts else color
                parts.append(f"{_swatch(color)} {token}" if opts.swatches else token)
            out.write(f"{path}\t{' '.join(parts)}\n")
    elif opts.fmt == "jsonl":
        for path, items in records:
            payload = {"path": str(path), "colors": dict(items)}
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    elif opts.fmt == "json":
        out.write("[")
        first = True
        for path, items in records:
            if not first:
                out.write(",")
            first = False
            payload = {"path": str(path), "colors": dict(items)}
            out.write(json.dumps(payload, ensure_ascii=False))
        out.write("]\n")
    elif opts.fmt in ("csv", "tsv"):
        delim = "," if opts.fmt == "csv" else "\t"
        writer = csv.writer(out, delimiter=delim, lineterminator="\n")
        writer.writerow(["path", "color"] + (["count"] if opts.show_counts else []))
        for path, items in records:
            for color, count in items:
                row = [str(path), color] + ([str(count)] if opts.show_counts else [])
                writer.writerow(row)
    elif opts.fmt == "md":
        header = ["File", "Color"] + (["Count"] if opts.show_counts else [])
        out.write("| " + " | ".join(header) + " |\n")
        out.write("|" + "|".join("---" for _ in header) + "|\n")
        for path, items in records:
            safe_path = f"`{str(path).replace('|', chr(92) + '|')}`"
            for color, count in items:
                cells = [safe_path, color] + ([str(count)] if opts.show_counts else [])
                out.write("| " + " | ".join(cells) + " |\n")
    elif opts.fmt == "html":
        out.write(HTML_HEAD)
        for path, items in records:
            out.write(f"<section><h3>{html.escape(str(path))}</h3><ul>")
            for color, count in items:
                suffix = f" ({count})" if opts.show_counts else ""
                out.write(f"<li><span class='sw' style='background:{color}'></span>{color}{suffix}</li>")
            out.write("</ul></section>")
        out.write(HTML_FOOT)


def _emit_unique(
    out: TextIO,
    records: list[tuple[str, int, int]],
    opts: Options,
) -> None:
    if opts.fmt == "text":
        for color, count, _files in records:
            token = f"{color}:{count}" if opts.show_counts else color
            if opts.swatches:
                out.write(f"{_swatch(color)} {token}\n")
            else:
                out.write(f"{token}\n")
    elif opts.fmt == "jsonl":
        for color, count, files in records:
            payload = {"color": color, "count": count, "files": files}
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    elif opts.fmt == "json":
        out.write("[")
        for index, (color, count, files) in enumerate(records):
            if index:
                out.write(",")
            payload = {"color": color, "count": count, "files": files}
            out.write(json.dumps(payload, ensure_ascii=False))
        out.write("]\n")
    elif opts.fmt in ("csv", "tsv"):
        delim = "," if opts.fmt == "csv" else "\t"
        writer = csv.writer(out, delimiter=delim, lineterminator="\n")
        writer.writerow(["color", "count", "files"])
        for color, count, files in records:
            writer.writerow([color, str(count), str(files)])
    elif opts.fmt == "md":
        out.write("| Color | Count | Files |\n")
        out.write("|---|---|---|\n")
        for color, count, files in records:
            out.write(f"| {color} | {count} | {files} |\n")
    elif opts.fmt == "html":
        out.write(HTML_HEAD)
        out.write("<section><h3>Palette</h3><ul>")
        for color, count, files in records:
            suffix = f" ({count} in {files} files)" if opts.show_counts else ""
            out.write(f"<li><span class='sw' style='background:{color}'></span>{color}{suffix}</li>")
        out.write("</ul></section>")
        out.write(HTML_FOOT)


def _resolve_sources(inputs: list[str]) -> tuple[list[Path], int]:
    sources = inputs or ["."]
    resolved: list[Path] = []
    missing = 0
    for source in sources:
        path = Path(source)
        if path.is_dir() or path.is_file():
            resolved.append(path)
        else:
            logger.error("Not a file or directory: {}", path)
            missing += 1
    return resolved, missing


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_colors",
        description="Extract CSS hex colors (#RGB, #RGBA, #RRGGBB, #RRGGBBAA) from files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan; defaults to the current directory",
    )
    scan = parser.add_argument_group("color detection")
    scan.add_argument(
        "--dedupe",
        action="store_true",
        help="Normalize #abc -> #aabbcc, #abcd -> #aabbccdd, #rrggbbff -> #rrggbb",
    )
    scan.add_argument(
        "--no-alpha",
        action="store_true",
        help="Ignore 4- and 8-digit hex with alpha",
    )
    sel = parser.add_argument_group("file selection")
    sel.add_argument(
        "--ext",
        metavar="EXT",
        help="Comma-separated extensions to include (e.g. css,scss,html)",
    )
    sel.add_argument(
        "--exclude",
        metavar="PATTERN",
        action="append",
        default=[],
        help="Glob or path-component pattern to skip (repeatable)",
    )
    sel.add_argument(
        "--skip-binary",
        action="store_true",
        help="Skip files that look binary (NUL byte in first KB)",
    )
    sel.add_argument(
        "--max-size",
        type=int,
        metavar="BYTES",
        help="Skip files larger than BYTES",
    )
    out = parser.add_argument_group("output")
    out.add_argument(
        "--format",
        choices=sorted(FORMATS),
        default="text",
        help="Output format",
    )
    out.add_argument("--counts", action="store_true", help="Include occurrence counts")
    out.add_argument(
        "--swatches",
        action="store_true",
        help="Show ANSI color swatches (text format only)",
    )
    out.add_argument(
        "--sort-by",
        choices=("name", "count"),
        default="name",
        help="Sort key",
    )
    out.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Only keep the top N colors (per file, or globally with --unique)",
    )
    out.add_argument(
        "--min-count",
        type=int,
        default=1,
        metavar="N",
        help="Drop colors with fewer than N occurrences",
    )
    out.add_argument(
        "--unique",
        action="store_true",
        help="Report a global palette across all files",
    )
    out.add_argument(
        "--all",
        action="store_true",
        help="Include files that contain no colors",
    )
    out.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="Write output to FILE instead of stdout",
    )
    run = parser.add_argument_group("runtime")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="List selected files and exit",
    )
    run.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    run.add_argument("-q", "--quiet", action="store_true", help="Only log errors")
    run.add_argument("--version", action="store_true", help="Show version and exit")
    return parser


def _configure_logging(verbose: bool, quiet: bool) -> None:
    logger.remove()
    if quiet:
        level = "ERROR"
    elif verbose:
        level = "DEBUG"
    else:
        level = "INFO"
    logger.add(
        sys.stderr,
        level=level,
        colorize=sys.stderr.isatty(),
        format="<level>{level: <7}</level> | {message}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        sys.stdout.write(f"extract_colors {__version__}\n")
        return 0
    _configure_logging(args.verbose, args.quiet)
    if MAX_WORKERS < 1:
        logger.error("--workers must be >= 1")
        return 2
    if args.max_size is not None and args.max_size < 0:
        logger.error("--max-size must be >= 0")
        return 2
    if args.top is not None and args.top < 0:
        logger.error("--top must be >= 0")
        return 2
    if args.min_count < 0:
        logger.error("--min-count must be >= 0")
        return 2
    extensions = frozenset(part.strip().lower().lstrip(".") for part in (args.ext or "").split(",") if part.strip())
    opts = Options(
        dedupe=args.dedupe,
        no_alpha=args.no_alpha,
        extensions=extensions,
        excludes=tuple(args.exclude or ()),
        skip_binary=args.skip_binary,
        max_size=args.max_size,
        fmt=args.format,
        show_counts=args.counts,
        swatches=args.swatches,
        sort_by=args.sort_by,
        top=args.top,
        min_count=args.min_count,
        include_empty=args.all,
        unique=args.unique,
    )
    if opts.swatches and opts.fmt not in ("text", "html"):
        logger.warning("--swatches has no effect for format '{}'", opts.fmt)
    sources, missing = _resolve_sources(args.paths)
    if missing:
        logger.error("{} input path(s) could not be resolved", missing)
    if not sources:
        logger.warning("No files to scan")
        return 0 if missing == 0 else 1
    try:
        files = collect_files(sources, opts)
    except OSError as exc:
        logger.error("Failed to enumerate files: {}", exc)
        return 1
    if args.dry_run:
        for path in files:
            sys.stdout.write(f"{path}\n")
        logger.info("Dry run: {} file(s) would be scanned", len(files))
        return 0
    if not files:
        logger.warning("No files matched after filtering")
        return 0 if missing == 0 else 1
    config = ScanConfig(dedupe=opts.dedupe, no_alpha=opts.no_alpha)
    chunksize = max(1, min(64, len(files) // (MAX_WORKERS * 8)))
    logger.info("Scanning {} file(s) with {} workers", len(files), MAX_WORKERS)
    out: TextIO
    closer = False
    if args.output is not None:
        try:
            out = args.output.open("w", encoding="utf-8", newline="")
            closer = True
        except OSError as exc:
            logger.error("Cannot open {}: {}", args.output, exc)
            return 1
    else:
        out = sys.stdout
    summary = Summary()
    exit_code = 0
    try:
        context = get_context("fork")
        with context.Pool(processes=MAX_WORKERS) as pool:
            results = pool.imap_unordered(
                process_file,
                ((path, config) for path in files),
                chunksize=chunksize,
            )
            if opts.unique:
                records = _aggregate_unique(results, opts, summary)
                _emit_unique(out, records, opts)
            else:
                _emit_stream(out, _stream_records(results, opts, summary), opts)
        logger.info("Done: {}", summary.render())
    except BrokenPipeError:
        logger.debug("Output pipe closed")
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        exit_code = 130
    except Exception as exc:
        logger.error("Scanning aborted: {}", exc)
        exit_code = 1
    finally:
        with contextlib.suppress(Exception):
            out.flush()
        if closer:
            with contextlib.suppress(OSError):
                out.close()
    if missing:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
