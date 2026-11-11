#!/data/data/com.termux/files/usr/bin/env python
"""Find duplicate folders by content hash or by tree structure.
Analysis / deduplication ------------------------ ``find_dup_folders.py`` (content hash per folder, JSON dump) and ``dupdirs.py`` (content hash + ``--structure`` mode, multiprocessing, nested-duplicate filtering) overlap almost completely; they are one tool with two ``--mode`` values.
``xxhash`` is used when installed and ``hashlib.blake2b`` (stdlib, equally collision-resistant for this job) otherwise, so the tool runs with zero dependencies.
The hardcoded ``/sdcard/dupdirs.json`` output is ``-o dupdirs.json``.
How it works: every folder under the root gets a signature built from its file names alone (structure) or from file names + content hashes (content).
Folders sharing a signature are reported as a group; a folder that merely *contains* another duplicate folder can be hidden with ``--prune-nested``.
Examples -------- python3 duplicate_folders_tool.py find ~/projects python3 duplicate_folders_tool.py find .
--mode structure python3 duplicate_folders_tool.py find .
--prune-nested -o report.json"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import sys

from dh import MAX_WORKERS, SKIP_DIRS


CHUNK_SIZE: int = 1 << 20


def file_digest(path: Path) -> str:
    """Return a content hash of one file (xxhash if available)."""
    try:
        import xxhash

        hasher = xxhash.xxh64()
    except ImportError:
        hasher = hashlib.blake2b(digest_size=8)
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                hasher.update(chunk)
    except OSError:
        return "unreadable"
    return str(hasher.hexdigest())


def folder_files(folder: Path) -> list[Path]:
    """Return all files inside *folder* (sorted), skipping junk dirs."""
    files: list[Path] = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.parts):
            files.append(path)
    return files


def folder_signature(job: tuple[Path, str]) -> tuple[Path, str]:
    """Return (folder, signature) for content or structure mode."""
    folder, mode = job
    lines: list[str] = []
    for path in folder_files(folder):
        rel: str = str(path.relative_to(folder))
        lines.append(rel if mode == "structure" else f"{rel}:{file_digest(path)}")
    digest: str = hashlib.blake2b("\n".join(lines).encode(), digest_size=16).hexdigest()
    return folder, digest if lines else "empty"


def all_folders(root: Path) -> list[Path]:
    """Return every non-junk directory under *root* (including *root*)."""
    folders: list[Path] = [root]
    for path in root.rglob("*"):
        if path.is_dir() and not SKIP_DIRS.intersection(path.parts):
            folders.append(path)
    return sorted(set(folders))


def is_nested_pair(a: Path, b: Path) -> bool:
    """Return True if one folder contains the other."""
    return a in b.parents or b in a.parents


def prune_nested_groups(groups: dict[str, list[Path]]) -> dict[str, list[Path]]:
    """Drop groups whose folders are all nested inside another group."""
    kept: dict[str, list[Path]] = {}
    all_dupes: list[Path] = [p for paths in groups.values() for p in paths]
    for digest, paths in groups.items():
        outer: list[Path] = [p for p in paths if not any(other != p and other in p.parents for other in all_dupes)]
        if len(outer) > 1:
            kept[digest] = outer
    return kept


def cmd_find(args: argparse.Namespace) -> int:
    """Run the ``find`` subcommand."""
    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1
    folders: list[Path] = all_folders(root)
    jobs: list[tuple[Path, str]] = [(folder, args.mode) for folder in folders]
    by_digest: dict[str, list[Path]] = defaultdict(list)
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for folder, digest in pool.map(folder_signature, jobs):
            if digest != "empty":
                by_digest[digest].append(folder)
    groups: dict[str, list[Path]] = {d: sorted(ps) for d, ps in by_digest.items() if len(ps) > 1}
    if args.prune_nested:
        groups = prune_nested_groups(groups)
    if not groups:
        print("No duplicate folders found.")
        return 0
    print(f"Duplicate folder groups ({args.mode}): {len(groups)}")
    for digest, paths in sorted(groups.items()):
        print(f"\nGroup {digest}:")
        for path in paths:
            print(f"  - {path}")
    if args.output is not None:
        payload: dict[str, list[str]] = {d: [str(p) for p in ps] for d, ps in groups.items()}
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Find duplicate folders by content or structure.")
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find", help="Scan a tree for duplicate folders")
    find.add_argument("root", nargs="?", type=Path, default=Path.cwd(), help="Root dir (default: .)")
    find.add_argument(
        "--mode",
        choices=("content", "structure"),
        default="content",
        help="content: names+hashes; structure: names only (default: content)",
    )
    find.add_argument(
        "-s", "--structure", action="store_const", const="structure", dest="mode", help="Shortcut for --mode structure"
    )
    find.add_argument(
        "--prune-nested", action="store_true", help="Hide duplicate groups nested inside other duplicates"
    )
    find.add_argument("-j", "--workers", type=int, default=MAX_WORKERS, help="Workers (default: 8)")
    find.add_argument("-o", "--output", type=Path, default=None, help="Also write groups as JSON")
    find.set_defaults(handler=cmd_find)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
