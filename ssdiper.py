#!/data/data/com.termux/files/usr/bin/env python
"""simz: perceptual hash comparison and grouping for local files."""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import importlib
from itertools import combinations
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, TypeAlias, TypedDict

from dh import SKIP_DIRS


Digest: TypeAlias = Callable[[bytes], Any]
Compare: TypeAlias = Callable[[Any, Any], int]
Backend: TypeAlias = tuple[Digest, Compare]


class PairRecord(TypedDict):
    algorithm: str
    file1: str
    file2: str
    similarity_score: int


NAMES: tuple[str, ...] = ("ssdeep", "tlsh", "sdhash", "minhash", "simhash")
GROUP_PATTERN: re.Pattern[str] = re.compile(r"group\d{3,}$")


class UnionFind:
    """Weighted union-find with path compression over :class:`Path` keys."""

    def __init__(self) -> None:
        self._parent: dict[Path, Path] = {}
        self._rank: dict[Path, int] = {}

    def find(self, path: Path) -> Path:
        self._parent.setdefault(path, path)
        self._rank.setdefault(path, 0)
        root = path
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[path] != root:
            nxt = self._parent[path]
            self._parent[path] = root
            path = nxt
        return root

    def union(self, first: Path, second: Path) -> bool:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return False
        first_rank = self._rank[first_root]
        second_rank = self._rank[second_root]
        if first_rank < second_rank:
            self._parent[first_root] = second_root
        elif first_rank > second_rank:
            self._parent[second_root] = first_root
        else:
            self._parent[second_root] = first_root
            self._rank[first_root] = first_rank + 1
        return True

    def groups(self) -> list[list[Path]]:
        buckets: dict[Path, list[Path]] = defaultdict(list)
        for path in list(self._parent):
            buckets[self.find(path)].append(path)
        return [sorted(group, key=str) for group in buckets.values() if len(group) > 1]


def is_name_pair(first: Path, second: Path) -> bool:
    """Return True if one filename is the other with ``_1`` inserted before a dot.

    Handles multi-extension names such as ``foo.tar.gz`` / ``foo.tar_1.gz``
    and ``foo.tar.gz`` / ``foo_1.tar.gz``.
    """
    for original, candidate in ((first.name, second.name), (second.name, first.name)):
        for index in range(len(original) + 1):
            if index == len(original) or original[index] == ".":
                if original[:index] + "_1" + original[index:] == candidate:
                    return True
    return False


def backend(name: str) -> Backend:
    if name == "tlsh":
        module = importlib.import_module("tlsh")

        def digest_tlsh(data: bytes) -> str | None:
            result = module.hash(data)
            if result in ("TNULL", "NULL", ""):
                return None
            return str(result)

        def compare_tlsh(first: str, second: str) -> int:
            return max(0, 100 - int(module.diff(first, second)))

        return digest_tlsh, compare_tlsh

    if name in ("sdhash", "ssdeep"):
        module = importlib.import_module(name)
        digest_fn = getattr(module, "hash" if name == "ssdeep" else "sdhash")
        return digest_fn, module.compare

    if name == "simhash":
        from simhash import Simhash  # type: ignore[import-untyped]

        def digest_simhash(data: bytes) -> int:
            counts = Counter(data[index : index + 3].hex() for index in range(max(1, len(data) - 2)))
            return int(Simhash(counts, f=64).value)

        def compare_simhash(first: int, second: int) -> int:
            return 100 - round(100 * (first ^ second).bit_count() / 64)

        return digest_simhash, compare_simhash

    from datasketch import MinHash  # type: ignore[import-untyped]
    import numpy as np

    def digest_minhash(data: bytes) -> Any:
        minhash = MinHash(num_perm=128)
        ngrams = max(1, len(data) - 4)
        for start in range(0, ngrams, 1024):
            batch = {data[index : index + 5] for index in range(start, min(start + 1024, ngrams))}
            minhash.update_batch(batch)
        return minhash.hashvalues

    def compare_minhash(first: Any, second: Any) -> int:
        matches = int(np.count_nonzero(first == second))
        return round(100 * matches / len(first))

    return digest_minhash, compare_minhash


def inspect_file(
    path: Path,
    backends: dict[str, Backend],
) -> tuple[dict[str, Any], list[str]]:
    results: dict[str, Any] = {}
    issues: list[str] = []
    try:
        data = path.read_bytes()
    except OSError as error:
        return results, [f"{path}: {error}"]
    if not data:
        return results, issues
    for name, (digest, _) in backends.items():
        try:
            result = digest(data)
        except Exception as error:
            issues.append(f"{path} ({name}): {error}")
            continue
        if result is not None:
            results[name] = result
    return results, issues


def matches_filters(
    path: Path,
    *,
    extensions: tuple[str, ...],
    exclude: tuple[str, ...],
    min_size: int,
    max_size: int | None,
) -> bool:
    if extensions and path.suffix.lower() not in extensions:
        return False
    for pattern in exclude:
        if path.match(pattern):
            return False
    if min_size > 0 or max_size is not None:
        try:
            size = path.stat().st_size
        except OSError:
            return True  # let read_bytes surface the error
        if size < min_size:
            return False
        if max_size is not None and size > max_size:
            return False
    return True


def move_groups(
    groups: list[list[Path]],
    root: Path,
    *,
    dry_run: bool,
) -> list[dict[str, str | list[str]]]:
    moves: list[dict[str, str | list[str]]] = []
    index = 0
    for group in sorted(groups, key=lambda g: [str(p) for p in g]):
        while True:
            index += 1
            destination = root / f"group{index:03}"
            if not destination.exists():
                break
        moves.append({
            "destination": str(destination),
            "files": [str(p) for p in group],
        })
        if dry_run:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        for path in group:
            path.rename(destination / path.name)
    return moves


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare and group files by perceptual hash.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-a", "--all", action="store_true", help="report all five algorithms")
    mode.add_argument("--alg", choices=NAMES, default="ssdeep", help="algorithm to use")
    parser.add_argument(
        "-g",
        "--group-similar",
        action="store_true",
        help="move same-directory name/_1 pairs that pass the selected algorithm",
    )
    parser.add_argument(
        "--group-mode",
        choices=("both", "name", "score"),
        default="both",
        help=(
            "grouping criterion for -g: 'both' = name pattern + score; "
            "'name' = name pattern only; 'score' = any pair meeting threshold"
        ),
    )
    parser.add_argument("--threshold", type=int, default=0, help="minimum score to report (0..100)")
    parser.add_argument("--jobs", type=int, default=1, help="number of parallel file readers")
    parser.add_argument("--output", type=Path, default=Path("simz.json"), help="JSON report path")
    parser.add_argument(
        "--move-root",
        type=Path,
        default=None,
        help="root directory for group folders (default: cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report and preview moves without touching the filesystem",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--extensions",
        action="append",
        default=[],
        metavar="EXT",
        help="only process these extensions (repeatable, e.g. .txt)",
    )
    parser.add_argument("--min-size", type=int, default=0, help="minimum file size in bytes")
    parser.add_argument("--max-size", type=int, default=None, help="maximum file size in bytes")
    return parser


def main() -> int:
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
    )

    parser = build_parser()
    args = parser.parse_args()
    if not 0 <= args.threshold <= 100:
        parser.error("threshold must be between 0 and 100")
    if args.jobs < 1:
        parser.error("jobs must be at least 1")
    if args.min_size < 0:
        parser.error("min-size must be non-negative")
    if args.max_size is not None and args.max_size < args.min_size:
        parser.error("max-size must be >= min-size")

    extensions = tuple(ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in args.extensions)
    exclude = tuple(args.exclude)

    output_path: Path = args.output.absolute()
    move_root: Path = (args.move_root or Path.cwd()).absolute()
    errors: list[str] = []
    backends: dict[str, Backend] = {}
    unavailable: dict[str, str] = {}
    selected_names = NAMES if args.all else (args.alg,)
    for name in selected_names:
        try:
            backends[name] = backend(name)
        except (ImportError, AttributeError, OSError) as error:
            unavailable[name] = str(error)

    name_only_grouping = args.group_similar and args.group_mode == "name"
    if not backends and not name_only_grouping:
        parser.error(f"No usable backends: {unavailable}")
    if unavailable:
        print("Unavailable:", unavailable, file=sys.stderr)

    union = UnionFind()
    files: list[Path] = []
    hashes: dict[str, list[tuple[Path, Any]]] = defaultdict(list)

    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    ) as progress:
        scan_task = progress.add_task("Scanning", total=None)
        scanned = 0
        for base, directories, names in os.walk(
            ".",
            onerror=lambda error: errors.append(str(error)),
        ):
            directories[:] = sorted(
                directory
                for directory in directories
                if not GROUP_PATTERN.fullmatch(directory) and directory not in SKIP_DIRS
            )
            for name in sorted(names):
                path = Path(base) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if path.absolute() == output_path:
                    continue
                if not matches_filters(
                    path,
                    extensions=extensions,
                    exclude=exclude,
                    min_size=args.min_size,
                    max_size=args.max_size,
                ):
                    continue
                files.append(path)
                scanned += 1
                progress.advance(scan_task)
        progress.update(scan_task, total=scanned)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            inspected = pool.map(lambda p: inspect_file(p, backends), files)
            for path, (results, issues) in progress.track(
                zip(files, inspected),
                total=len(files),
                description="Hashing",
            ):
                errors.extend(issues)
                for name, result in results.items():
                    hashes[name].append((path, result))

        # Name-only grouping does not depend on hash scores.
        if name_only_grouping:
            path_set = set(files)
            for path in files:
                fname = path.name
                for index in range(len(fname) + 1):
                    if index == len(fname) or fname[index] == ".":
                        candidate = path.parent / (fname[:index] + "_1" + fname[index:])
                        if candidate in path_set:
                            union.union(path, candidate)

        pair_records: list[PairRecord] = []
        for name, (_, compare) in backends.items():
            rows = hashes[name]
            pair_count = len(rows) * (len(rows) - 1) // 2
            for (first_path, first_hash), (second_path, second_hash) in progress.track(
                combinations(rows, 2),
                total=pair_count,
                description=f"Comparing {name}",
            ):
                try:
                    score = int(compare(first_hash, second_hash))
                except Exception as error:
                    errors.append(f"{first_path}, {second_path} ({name}): {error}")
                    continue
                if score == 0 or score < args.threshold:
                    continue
                pair_records.append({
                    "algorithm": name,
                    "file1": str(first_path),
                    "file2": str(second_path),
                    "similarity_score": score,
                })
                if not args.group_similar or args.group_mode == "name":
                    continue
                if first_path.parent != second_path.parent:
                    continue
                if args.group_mode == "score":
                    union.union(first_path, second_path)
                elif is_name_pair(first_path, second_path):
                    union.union(first_path, second_path)

        pair_records.sort(key=lambda r: r["similarity_score"], reverse=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        groups = union.groups() if args.group_similar else []
        report: dict[str, Any] = {
            "files": len(files),
            "threshold": args.threshold,
            "unavailable": unavailable,
            "hashed": {name: len(hashes[name]) for name in backends},
            "pairs": pair_records,
            "errors": errors,
        }
        if args.group_similar:
            report["group_mode"] = args.group_mode
            report["groups"] = [[str(p) for p in group] for group in groups]

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(report, temp_file, indent=2)
                temp_file.write("\n")
            os.replace(temp_path, output_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        if args.group_similar and groups:
            moves = move_groups(groups, move_root, dry_run=args.dry_run)
            moves_path = output_path.with_name(output_path.stem + ".groups.json")
            with open(moves_path, "w", encoding="utf-8") as handle:
                json.dump(moves, handle, indent=2)
                handle.write("\n")
            verb = "Would move" if args.dry_run else "Moved"
            print(f"{verb} {sum(len(g) for g in groups)} file(s) into {len(groups)} group(s): {moves_path}")

    print(f"Report: {output_path} ({len(errors)} errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
