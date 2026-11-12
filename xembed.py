#!/data/data/com.termux/files/usr/bin/env python
"""Recursively extract embedded base64 data-URIs from files."""

import argparse
import base64
import binascii
from collections import deque
from collections.abc import Iterator, Sequence
import concurrent.futures
from dataclasses import asdict, dataclass, field
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from typing import Final
from urllib.parse import quote

from atomicio import atomic_write
from dh import MAX_WORKERS

PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_+/-])"
    r"data:"
    r"(?P<mime>[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)?"
    r"(?P<params>(?:;[A-Za-z0-9.+=_-]+)*?)"
    r";base64,"
    r"(?P<data>[A-Za-z0-9+/_-]+={0,2})",
    re.DOTALL,
)
MIME_EXT: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/webp": ".webp",
    "image/png": ".png",
    "image/gif": ".gif",
    "text/plain": ".txt",
    "text/css": ".css",
    "text/html": ".html",
    "application/javascript": ".js",
    "application/json": ".json",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/x-font-woff": ".woff",
    "application/vnd.ms-fontobject": ".eot",
    "application/octet-stream": "",
}
SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"%PDF-", ".pdf"),
    (b"wOF2", ".woff2"),
    (b"wOFF", ".woff"),
    (b"OTTO", ".otf"),
    (b"\x00\x01\x00\x00", ".ttf"),
    (b"\x00\x00\x01\x00", ".ico"),
)
TEXT_ASSET_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".css",
    ".svg",
    ".js",
    ".json",
    ".html",
    ".htm",
    ".txt",
    ".xml",
})
DEFAULT_EXCLUDES: Final[tuple[str, ...]] = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
)
DEFAULT_SUFFIXES: Final[tuple[str, ...]] = (".html", ".css", ".svg", ".js", ".json")


@dataclass(slots=True, frozen=True)
class Options:
    root: Path
    assets: Path
    suffixes: frozenset[str]
    exclude: frozenset[str]
    min_size: int
    replace: bool
    backup: bool
    dry_run: bool
    manifest: bool
    verbose: bool
    overwrite: bool
    max_passes: int


@dataclass(slots=True)
class ManifestEntry:
    mime: str
    size: int
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Stats:
    files_scanned: int = 0
    files_changed: int = 0
    found: int = 0
    saved: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0
    passes: int = 0
    manifest: dict[str, ManifestEntry] = field(default_factory=dict)


def decode_payload(raw: str) -> bytes | None:
    cleaned = raw.replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        return None


def sniff_extension(payload: bytes) -> str | None:
    for signature, ext in SIGNATURES:
        if payload.startswith(signature):
            return ext
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    head = payload[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return ".svg"
    return None


def resolve_extension(mime: str | None, payload: bytes) -> str:
    if mime:
        key = mime.lower().split(";", 1)[0].strip()
        ext = MIME_EXT.get(key)
        if ext is None:
            guessed = mimetypes.guess_extension(key, strict=False)
            if guessed:
                if guessed == ".jpe":
                    guessed = ".jpg"
                ext = guessed
        if ext:
            return ext
    return sniff_extension(payload) or ".bin"


def safe_relative_to(path: Path, start: Path) -> Path:
    try:
        return path.relative_to(start, walk_up=True)
    except TypeError:
        try:
            return path.relative_to(start)
        except ValueError:
            return path


def build_link(asset: Path, source: Path) -> str:
    relative = safe_relative_to(asset.resolve(), source.parent.resolve())
    return quote(relative.as_posix())


def should_skip_dir(name: str, exclude: frozenset[str]) -> bool:
    return name in exclude or name.startswith(".")


def iter_sources(opts: Options) -> Iterator[Path]:
    assets_resolved = opts.assets.resolve()
    root = opts.root
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not should_skip_dir(d, opts.exclude) and (current / d).resolve() != assets_resolved
        )
        for name in sorted(filenames):
            path = current / name
            if path.suffix.lower() in opts.suffixes:
                yield path


def process_file(
    path: Path,
    opts: Options,
    stats: Stats,
    *,
    is_asset: bool = False,
) -> list[Path]:
    newly_created_text_assets: list[Path] = []
    try:
        original = path.read_bytes()
    except OSError as exc:
        if opts.verbose:
            print(f"error reading {path}: {exc}", file=sys.stderr)
        stats.errors += 1
        return newly_created_text_assets
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        if opts.verbose:
            print(f"skip (not utf-8): {path}", file=sys.stderr)
        stats.skipped += 1
        return newly_created_text_assets
    stats.files_scanned += 1
    try:
        source_label = path.relative_to(opts.root).as_posix()
    except ValueError:
        try:
            source_label = path.relative_to(opts.assets).as_posix()
        except ValueError:
            source_label = str(path)

    def substitute(match: re.Match[str]) -> str:
        stats.found += 1
        payload = decode_payload(match["data"])
        if payload is None or len(payload) < opts.min_size:
            stats.skipped += 1
            return match.group(0)
        ext = resolve_extension(match["mime"], payload)
        digest = hashlib.sha256(payload).hexdigest()[:16]
        name = f"{digest}{ext}"
        target = opts.assets / name
        entry = stats.manifest.get(name)
        created_this_run = False
        if entry is None:
            entry = ManifestEntry(mime=match["mime"] or "", size=len(payload))
            stats.manifest[name] = entry
            if target.exists() and not opts.overwrite:
                stats.duplicates += 1
            else:
                stats.saved += 1
                created_this_run = True
                if not opts.dry_run:
                    opts.assets.mkdir(parents=True, exist_ok=True)
                    if opts.overwrite or not target.exists():
                        target.write_bytes(payload)
        else:
            stats.duplicates += 1
        if source_label not in entry.sources:
            entry.sources.append(source_label)
        if created_this_run and ext.lower() in TEXT_ASSET_SUFFIXES:
            newly_created_text_assets.append(target)
        if not opts.replace:
            return match.group(0)
        return build_link(target, path)

    try:
        updated = PATTERN.sub(substitute, text)
    except re.error as exc:
        if opts.verbose:
            print(f"regex error in {path}: {exc}", file=sys.stderr)
        stats.errors += 1
        return newly_created_text_assets
    if not opts.replace or updated == text:
        return newly_created_text_assets
    stats.files_changed += 1
    if opts.dry_run:
        if opts.verbose:
            print(f"[dry-run] would rewrite: {path}")
        return newly_created_text_assets
    if opts.backup and not is_asset:
        bak = path.with_name(f"{path.name}.bak")
        try:
            bak.write_bytes(original)
        except OSError as exc:
            print(f"warning: could not write backup {bak}: {exc}", file=sys.stderr)
    try:
        atomic_write(path, updated.encode("utf-8"))
        if opts.verbose:
            print(f"rewrote: {path}")
    except OSError as exc:
        print(f"error writing {path}: {exc}", file=sys.stderr)
        stats.errors += 1
    return newly_created_text_assets


def run_until_clean(opts: Options, stats: Stats) -> None:
    queue: deque[Path] = deque(iter_sources(opts))
    seen: set[Path] = set()
    pass_no = 0
    while queue and pass_no < opts.max_passes:
        pass_no += 1
        stats.passes = pass_no
        if opts.verbose:
            print(f"--- pass {pass_no} ({len(queue)} files) ---")
        batch = list(queue)
        queue.clear()
        new_assets: list[Path] = []
        if MAX_WORKERS == 1 or len(batch) < 4:
            for path in batch:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                is_asset = opts.assets in resolved.parents or resolved.parent == opts.assets
                created = process_file(path, opts, stats, is_asset=is_asset)
                new_assets.extend(created)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {}
                for path in batch:
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    is_asset = opts.assets in resolved.parents or resolved.parent == opts.assets
                    fut = pool.submit(process_file, path, opts, stats, is_asset=is_asset)
                    futures[fut] = path
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        created = fut.result()
                        new_assets.extend(created)
                    except Exception as exc:
                        print(f"worker error: {exc}", file=sys.stderr)
                        stats.errors += 1
        for asset in new_assets:
            resolved = asset.resolve()
            if resolved not in seen:
                queue.append(asset)
        if opts.verbose and not queue:
            print(f"--- clean after {pass_no} pass(es) ---")
    if queue and pass_no >= opts.max_passes:
        print(
            f"warning: reached max-passes={opts.max_passes}; {len(queue)} file(s) still pending",
            file=sys.stderr,
        )


def write_manifest(opts: Options, stats: Stats) -> None:
    if not opts.manifest or opts.dry_run or not stats.manifest:
        return
    opts.assets.mkdir(parents=True, exist_ok=True)
    target = opts.assets / "manifest.json"
    merged: dict[str, dict[str, object]] = {}
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = raw
        except (json.JSONDecodeError, OSError) as exc:
            if opts.verbose:
                print(f"warning: could not read existing manifest: {exc}", file=sys.stderr)
    for name, entry in stats.manifest.items():
        current = asdict(entry)
        previous = merged.get(name)
        if isinstance(previous, dict):
            known = previous.get("sources", [])
            if isinstance(known, list):
                current["sources"] = sorted(set(known) | set(entry.sources))
        merged[name] = current
    target.write_text(
        json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if opts.verbose:
        print(f"wrote manifest: {target}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively extract embedded base64 data-URIs.  "
            "When -x is used, newly written text assets (CSS/SVG/…) that still "
            "contain data-URIs are re-processed until none remain."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-x",
        "--replace",
        action="store_true",
        help="replace data-URIs with relative links and keep extracting until clean",
    )
    parser.add_argument("--root", type=Path, default=Path(), help="directory to scan")
    parser.add_argument(
        "--assets",
        type=Path,
        default=Path("assets"),
        help="output folder (relative paths resolved against --root)",
    )
    parser.add_argument(
        "--suffixes",
        nargs="+",
        default=list(DEFAULT_SUFFIXES),
        help="file suffixes to scan in the source tree",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=list(DEFAULT_EXCLUDES),
        help="directory basenames to skip",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="ignore decoded objects smaller than this",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="write .bak copies of original source files before rewriting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report actions without writing anything",
    )
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="write/update assets/manifest.json",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing asset files that share the same hash name",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print per-file actions and pass progress",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=32,
        metavar="N",
        help="safety limit on iterative extraction passes",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    assets: Path = args.assets if args.assets.is_absolute() else (root / args.assets)
    assets = assets.resolve()
    opts = Options(
        root=root,
        assets=assets,
        suffixes=frozenset(s if s.startswith(".") else f".{s}" for s in (x.lower() for x in args.suffixes)),
        exclude=frozenset(args.exclude),
        min_size=max(0, args.min_size),
        replace=args.replace,
        backup=args.backup,
        dry_run=args.dry_run,
        manifest=args.manifest,
        verbose=args.verbose,
        #        workers=max(1, MAX_WORKERS),
        overwrite=args.overwrite,
        max_passes=max(1, args.max_passes),
    )
    stats = Stats()
    run_until_clean(opts, stats)
    write_manifest(opts, stats)
    prefix = "[dry-run] " if opts.dry_run else ""
    print(f"{prefix}passes         : {stats.passes}")
    print(f"{prefix}files scanned  : {stats.files_scanned}")
    print(f"{prefix}base64 found   : {stats.found}")
    print(f"{prefix}assets saved   : {stats.saved}")
    print(f"{prefix}duplicates     : {stats.duplicates}")
    print(f"{prefix}skipped        : {stats.skipped}")
    if opts.replace:
        print(f"{prefix}files rewritten: {stats.files_changed}")
    if stats.errors:
        print(f"{prefix}errors         : {stats.errors}", file=sys.stderr)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
