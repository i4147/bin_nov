#!/data/data/com.termux/files/usr/bin/env python
"""Find and manage the exact and fuzzy duplicate files.
Hashes the files, groups the exact duplicates and the ones above a similarity threshold (ssdeep), and the ``scan``/``report``/``move`` subcommands copy, move, delete or export the groups as CSV, JSON or a similarity matrix.
Usage: fuzzy_dedup {scan,report,move} [options] [--root DIR] Positional arguments: threshold Similarity threshold from 0 through 100.
Subcommands: scan Scan, compare, and optionally copy, move, or delete groups.
report Generate CSV, JSON, or matrix reports.
move Move fuzzy groups into numbered output directories.
Options: --root Directory or file to scan.
--output Output directory.
--profile Compatibility profile for the original scanners.
--action Action for matched groups.
--include-symlinks Include symbolic links.
--ignore Directory name to ignore.
Repeat for multiple names.
--min-size Minimum file size in bytes.
--max-bytes Maximum number of bytes read from each file.
--exclude-exact Do not fuzzy-compare byte-identical files.
--mark-on-match Prevent already matched files from entering later groups.
...
plus 6 more; run ``fuzzy_dedup --help`` for the full list.
Examples: $ fuzzy_dedup scan 80 --root ~/photos Command-line script: run ``python fuzzy_dedup.py`` or the ``fuzzy_dedup`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Iterable, Iterator, Mapping, Sequence

try:
    import ssdeep
except ImportError as exc:
    msg = "Missing dependency: ssdeep. Install it with: pip install ssdeep"
    raise SystemExit(msg) from exc
try:
    import xxhash
except ImportError as exc:
    msg = "Missing dependency: xxhash. Install it with: pip install xxhash"
    raise SystemExit(msg) from exc
try:
    from colorama import (
        Fore,
        Style,
        init as colorama_init,
    )
except ImportError:
    Fore = None
    Style = None
    colorama_init = None
try:
    from tabulate import tabulate
except ImportError:
    tabulate = None
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None
DEFAULT_IGNORED_DIRECTORIES: frozenset[str] = frozenset({".git", "__pycache__", "node_modules"})


@dataclass(frozen=True, slots=True)
class FileHash:
    path: Path
    exact: str
    fuzzy: str


@dataclass(frozen=True, slots=True)
class ScanOptions:
    root: Path
    include_symlinks: bool
    ignored_directories: frozenset[str]
    min_size: int
    max_bytes: int | None


def progress(
    values: Iterable[Path],
    *,
    total: int | None = None,
    description: str = "",
) -> Iterable[Path]:
    if tqdm is None:
        return values
    return tqdm(values, total=total, desc=description)


def iter_files(options: ScanOptions) -> Iterator[Path]:
    root = options.root.resolve()
    if not root.exists():
        msg = f"Search path does not exist: {root}"
        raise FileNotFoundError(msg)
    if root.is_file():
        if options.include_symlinks or not root.is_symlink():
            yield root
        return
    for current, directories, filenames in os.walk(
        root,
        followlinks=options.include_symlinks,
    ):
        directories[:] = [name for name in directories if name not in options.ignored_directories]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            if not options.include_symlinks and path.is_symlink():
                continue
            try:
                if path.stat().st_size >= options.min_size:
                    yield path
            except OSError as exc:
                print(f"Error accessing {path}: {exc}", file=sys.stderr)


def read_for_hash(path: Path, max_bytes: int | None) -> bytes:
    with path.open("rb") as file:
        if max_bytes is None:
            return file.read()
        return file.read(max_bytes)


def hash_file(path: Path, max_bytes: int | None) -> FileHash | None:
    try:
        data = read_for_hash(path, max_bytes)
        exact = xxhash.xxh64(data).hexdigest()
        fuzzy = ssdeep.hash(data)
        return FileHash(path=path, exact=exact, fuzzy=fuzzy)
    except OSError as exc:
        print(f"Error reading file {path}: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Error hashing file {path}: {exc}", file=sys.stderr)
    return None


def hash_files(
    paths: Sequence[Path],
    *,
    max_bytes: int | None,
) -> dict[Path, FileHash]:
    result: dict[Path, FileHash] = {}
    for path in progress(paths, total=len(paths), description="Hashing"):
        hashed = hash_file(path, max_bytes)
        if hashed is not None:
            result[path] = hashed
    return result


def exact_duplicate_groups(
    hashes: Mapping[Path, FileHash],
) -> dict[str, list[Path]]:
    groups: defaultdict[str, list[Path]] = defaultdict(list)
    for item in hashes.values():
        groups[item.exact].append(item.path)
    return {digest: paths for digest, paths in groups.items() if len(paths) > 1}


def fuzzy_groups(
    hashes: Mapping[Path, FileHash],
    threshold: int,
    *,
    exclude_exact_duplicates: bool = False,
    mark_on_match: bool = True,
    minimum_group_size: int = 2,
) -> list[list[Path]]:
    exact_groups = exact_duplicate_groups(hashes)
    excluded: set[Path] = set()
    if exclude_exact_duplicates:
        excluded = {path for paths in exact_groups.values() for path in paths}
    candidates = [path for path in hashes if path not in excluded]
    matched: set[Path] = set()
    groups: list[list[Path]] = []
    for index, first in enumerate(
        progress(
            candidates,
            total=len(candidates),
            description="Finding similarities",
        )
    ):
        if first in matched:
            continue
        group = [first]
        local_matches: set[Path] = {first}
        first_hash = hashes[first].fuzzy
        for other in candidates[index + 1 :]:
            if other in matched:
                continue
            try:
                score = ssdeep.compare(first_hash, hashes[other].fuzzy)
            except Exception as exc:
                print(
                    f"Error comparing {first} and {other}: {exc}",
                    file=sys.stderr,
                )
                continue
            if score >= threshold:
                group.append(other)
                local_matches.add(other)
                if mark_on_match:
                    matched.add(other)
        if len(group) >= minimum_group_size:
            groups.append(group)
            matched.update(local_matches)
        elif mark_on_match:
            matched.discard(first)
    return groups


def print_exact_duplicates(
    exact_groups: Mapping[str, Sequence[Path]],
) -> None:
    if not exact_groups:
        return
    print("=" * 40)
    print("DUPLICATES (100% identical)")
    for digest, paths in exact_groups.items():
        print(f"Hash: {digest}")
        for path in paths:
            print(f"- {path}")
    print("-" * 40)


def destination_group(output: Path, number: int, prefix: str) -> Path:
    directory = output / f"{prefix}_{number}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def copy_groups(
    groups: Sequence[Sequence[Path]],
    output: Path,
    *,
    prefix: str = "similarity_group",
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for number, group in enumerate(groups, start=1):
        directory = destination_group(output, number, prefix)
        for source in group:
            destination = directory / source.name
            try:
                shutil.copy2(source, destination)
                print(f"Copied {source} to {directory}")
            except OSError as exc:
                print(f"Failed to copy {source}: {exc}", file=sys.stderr)


def move_groups(
    groups: Sequence[Sequence[Path]],
    output: Path,
    *,
    prefix: str = "group",
    delete_rest: bool = False,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for number, group in enumerate(groups, start=1):
        directory = destination_group(output, number, prefix)
        selected = group[1:] if delete_rest else group
        for source in selected:
            if delete_rest:
                try:
                    source.unlink()
                    print(f"Deleted {source}")
                except OSError as exc:
                    print(f"Failed to delete {source}: {exc}", file=sys.stderr)
                continue
            destination = directory / source.name
            if destination.exists():
                print(
                    f"Skipping existing destination: {destination}",
                    file=sys.stderr,
                )
                continue
            try:
                shutil.move(str(source), str(destination))
                print(f"Moved {source} to {directory}")
            except OSError as exc:
                print(f"Failed to move {source}: {exc}", file=sys.stderr)


def print_pair_report(
    groups: Sequence[Sequence[Path]],
    *,
    root: Path,
    hashes: Mapping[Path, FileHash],
    threshold: int,
) -> None:
    print("\n--- Fuzzy Duplicate Sets ---")
    for group in groups:
        first = group[0]
        print(f"\nFile: {first.relative_to(root)}")
        for other in group[1:]:
            score = ssdeep.compare(
                hashes[first].fuzzy,
                hashes[other].fuzzy,
            )
            print(f"- Similar: {other.relative_to(root)} (Score: {score})")


def write_group_report(
    groups: Sequence[Sequence[Path]],
    output: Path,
    report_format: str,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if report_format == "csv":
        report_path = output / "similar_report.csv"
        with report_path.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.writer(file)
            writer.writerow(["Group", "File"])
            for number, group in enumerate(groups, start=1):
                for path in group:
                    writer.writerow([number, str(path)])
        print(f"CSV report written to {report_path}")
        return
    if report_format == "json":
        report_path = output / "similar_report.json"
        payload = {f"group_{number}": [str(path) for path in group] for number, group in enumerate(groups, start=1)}
        with report_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
        print(f"JSON report written to {report_path}")
        return
    msg = f"Unsupported report format: {report_format}"
    raise ValueError(msg)


def score_text(score: int | str, threshold: int) -> str:
    text = str(score)
    if Fore is None or Style is None:
        return text
    if score == 100 or (isinstance(score, int) and score >= threshold + 10):
        return f"{Fore.GREEN}{text}{Style.RESET_ALL}"
    if isinstance(score, int) and score >= threshold:
        return f"{Fore.YELLOW}{text}{Style.RESET_ALL}"
    return f"{Fore.RED}{text}{Style.RESET_ALL}"


def write_similarity_matrix(
    hashes: Mapping[Path, FileHash],
    threshold: int,
    output: Path,
    *,
    display: bool,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    paths = list(hashes)
    rows: list[list[str | int]] = [["File", *[str(path) for path in paths]]]
    for first in paths:
        row: list[str | int] = [str(first)]
        for second in paths:
            if first == second:
                score: int | str = 100
            else:
                value = ssdeep.compare(
                    hashes[first].fuzzy,
                    hashes[second].fuzzy,
                )
                score = value if value >= threshold else ""
            row.append(score)
        rows.append(row)
    matrix_path = output / "similarity_matrix.csv"
    with matrix_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        csv.writer(file).writerows(rows)
    print(f"Threshold-filtered similarity matrix written to {matrix_path}")
    if not display:
        return
    display_rows = [
        [
            row[0],
            *[score_text(value, threshold) for value in row[1:]],
        ]
        for row in rows[1:]
    ]
    if tabulate is not None:
        print(tabulate(display_rows, headers=rows[0], tablefmt="grid"))
        return
    print("|".join(str(value) for value in rows[0]))
    print("-" * len("|".join(str(value) for value in rows[0])))
    for row in display_rows:
        print("|".join(str(value) if value else "." for value in row))


def make_scan_options(
    args: argparse.Namespace,
    *,
    profile: str,
) -> ScanOptions:
    if profile == "fsim":
        include_symlinks = True
        ignored = frozenset()
        min_size = 0
        max_bytes = None
    elif profile == "ssdip":
        include_symlinks = False
        ignored = frozenset()
        min_size = 51
        max_bytes = 1024 * 1024
    else:
        include_symlinks = False
        ignored = DEFAULT_IGNORED_DIRECTORIES
        min_size = 0
        max_bytes = None
    if args.include_symlinks is not None:
        include_symlinks = args.include_symlinks
    if args.ignore is not None:
        ignored = frozenset(args.ignore)
    if args.min_size is not None:
        min_size = args.min_size
    if args.max_bytes is not None:
        max_bytes = args.max_bytes
    return ScanOptions(
        root=Path(args.root),
        include_symlinks=include_symlinks,
        ignored_directories=ignored,
        min_size=min_size,
        max_bytes=max_bytes,
    )


def run_scan(args: argparse.Namespace) -> int:
    if not 0 <= args.threshold <= 100:
        msg = "Threshold must be an integer from 0 through 100."
        raise SystemExit(msg)
    options = make_scan_options(args, profile=args.profile)
    paths = list(iter_files(options))
    print(f"Found {len(paths)} files. Computing hashes...")
    hashes = hash_files(paths, max_bytes=options.max_bytes)
    exact_groups = exact_duplicate_groups(hashes)
    if args.print_exact:
        print_exact_duplicates(exact_groups)
    groups = fuzzy_groups(
        hashes,
        args.threshold,
        exclude_exact_duplicates=args.exclude_exact,
        mark_on_match=args.mark_on_match,
        minimum_group_size=args.min_group_size_for_group,
    )
    if args.pair_report:
        print_pair_report(
            groups,
            root=options.root.resolve(),
            hashes=hashes,
            threshold=args.threshold,
        )
    if not groups:
        print("No similar files found.")
        return 0
    print(f"Found {len(groups)} groups of similar files.")
    if args.action == "copy":
        copy_groups(groups, Path(args.output))
    elif args.action == "move":
        move_groups(groups, Path(args.output))
    elif args.action == "delete":
        move_groups(
            groups,
            Path(args.output),
            delete_rest=True,
        )
    print(f"Processed {len(groups)} similarity groups.")
    return 0


def run_report(args: argparse.Namespace) -> int:
    if not 0 <= args.threshold <= 100:
        msg = "Threshold must be an integer from 0 through 100."
        raise SystemExit(msg)
    options = ScanOptions(
        root=Path(args.root),
        include_symlinks=args.include_symlinks,
        ignored_directories=frozenset(args.ignore),
        min_size=args.min_size,
        max_bytes=args.max_bytes,
    )
    paths = list(iter_files(options))
    print(f"Found {len(paths)} files. Computing hashes...")
    hashes = hash_files(paths, max_bytes=options.max_bytes)
    groups = fuzzy_groups(
        hashes,
        args.threshold,
        exclude_exact_duplicates=args.exclude_exact,
        minimum_group_size=2,
    )
    if args.format == "matrix":
        write_similarity_matrix(
            hashes,
            args.threshold,
            Path(args.output),
            display=args.display,
        )
        return 0
    if not groups:
        print("No similar files found.")
    write_group_report(groups, Path(args.output), args.format)
    return 0


def run_move(args: argparse.Namespace) -> int:
    if not 0 <= args.threshold <= 100:
        msg = "Threshold must be an integer from 0 through 100."
        raise SystemExit(msg)
    options = ScanOptions(
        root=Path(args.root),
        include_symlinks=False,
        ignored_directories=frozenset(args.ignore),
        min_size=args.min_size,
        max_bytes=args.max_bytes,
    )
    paths = list(iter_files(options))
    print(f"Scanning for fuzzy duplicates in: {options.root.resolve()}")
    hashes = hash_files(paths, max_bytes=options.max_bytes)
    groups = fuzzy_groups(
        hashes,
        args.threshold,
        mark_on_match=False,
        minimum_group_size=args.min_group_size,
    )
    if not groups:
        print("No groups of similar files found that met the criteria.")
        return 0
    move_groups(
        groups,
        Path(args.output),
        prefix="group",
    )
    moved_count = sum(len(group) for group in groups)
    print(f"Moved {moved_count} files into {len(groups)} groups.")
    print(f"Similar files have been moved to: {Path(args.output)}")
    return 0


def add_common_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "threshold",
        type=int,
        nargs="?",
        default=70,
        help="Similarity threshold from 0 through 100.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Directory or file to scan.",
    )
    parser.add_argument(
        "--output",
        default="output",
        help="Output directory.",
    )
    parser.add_argument(
        "--profile",
        choices=("fsim", "ssdip", "ssim"),
        default="ssim",
        help="Compatibility profile for the original scanners.",
    )
    parser.add_argument(
        "--action",
        choices=("copy", "move", "delete"),
        default="copy",
        help="Action for matched groups.",
    )
    parser.add_argument(
        "--include-symlinks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include symbolic links.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=None,
        help="Directory name to ignore. Repeat for multiple names.",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=None,
        help="Minimum file size in bytes.",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="Maximum number of bytes read from each file.",
    )
    parser.add_argument(
        "--exclude-exact",
        action="store_true",
        help="Do not fuzzy-compare byte-identical files.",
    )
    parser.add_argument(
        "--mark-on-match",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prevent already matched files from entering later groups.",
    )
    parser.add_argument(
        "--min-group-size",
        dest="min_size_for_group",
        type=int,
        default=2,
        help="Minimum number of files in a reported group.",
    )
    parser.add_argument(
        "--pair-report",
        action="store_true",
        help="Print pair-wise fuzzy matches.",
    )
    parser.add_argument(
        "--print-exact",
        action="store_true",
        help="Print exact duplicate groups.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find and manage exact and fuzzy duplicate files.")
    subparsers = parser.add_subparsers(dest="command")
    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan, compare, and optionally copy, move, or delete groups.",
    )
    add_common_scan_arguments(scan_parser)
    scan_parser.set_defaults(
        handler=run_scan,
        min_size_for_group=2,
    )
    report_parser = subparsers.add_parser(
        "report",
        help="Generate CSV, JSON, or matrix reports.",
    )
    report_parser.add_argument(
        "threshold",
        type=int,
        nargs="?",
        default=70,
    )
    report_parser.add_argument("--root", default=".")
    report_parser.add_argument("--output", default="output")
    report_parser.add_argument(
        "--format",
        choices=("csv", "json", "matrix"),
        default="csv",
    )
    report_parser.add_argument("--display", action="store_true")
    report_parser.add_argument(
        "--include-symlinks",
        action="store_true",
    )
    report_parser.add_argument(
        "--ignore",
        action="append",
        default=list(DEFAULT_IGNORED_DIRECTORIES),
    )
    report_parser.add_argument("--min-size", type=int, default=0)
    report_parser.add_argument("--max-bytes", type=int, default=None)
    report_parser.add_argument("--exclude-exact", action="store_true")
    report_parser.set_defaults(handler=run_report)
    move_parser = subparsers.add_parser(
        "move",
        help="Move fuzzy groups into numbered output directories.",
    )
    move_parser.add_argument("--root", default=".")
    move_parser.add_argument("--output", default="output")
    move_parser.add_argument("--threshold", type=int, default=60)
    move_parser.add_argument("--min-group-size", type=int, default=2)
    move_parser.add_argument("--min-size", type=int, default=0)
    move_parser.add_argument("--max-bytes", type=int, default=None)
    move_parser.add_argument(
        "--ignore",
        action="append",
        default=list(DEFAULT_IGNORED_DIRECTORIES),
    )
    move_parser.set_defaults(handler=run_move)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    if colorama_init is not None:
        colorama_init()
    if hasattr(args, "min_size_for_group"):
        args.min_group_size_for_group = args.min_size_for_group
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
