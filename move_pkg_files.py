#!/data/data/com.termux/files/usr/bin/env python
"""Move files belonging to installed packages into ~/tmp/pkgs/<package>.

Run from inside site-packages; accepts package names, wildcards or `all`, preserves the site-packages layout, and can move or delete emptied source directories. --dry-run previews.
"""

from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass
from difflib import get_close_matches
import fnmatch
from pathlib import Path
import shutil
import sys
from typing import TYPE_CHECKING, Final

from loguru import logger


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

DEFAULT_OUTPUT_ROOT: Final[Path] = Path.home() / "tmp" / "pkgs"
RECORD_FILENAME: Final[str] = "RECORD"
ENTRY_POINTS_FILENAME: Final[str] = "entry_points.txt"
DIST_INFO_SUFFIX: Final[str] = ".dist-info"
PYC_SUFFIXES: Final[tuple[str, ...]] = (".pyc", ".pyo")
IGNORED_RECORD_NAMES: Final[frozenset[str]] = frozenset({"RECORD.jws"})
DIST_INFO_ALLOWED_FILES: Final[frozenset[str]] = frozenset(
    {
        "METADATA",
        "RECORD",
        "top_level.txt",
        "entry_points.txt",
        "WHEEL",
    }
)
MISSING_FILES_REPORT: Final[Path] = Path("missing-files.txt")
ALL_KEYWORD: Final[str] = "all"


@dataclass(frozen=True, slots=True)
class PackageInfo:
    requested_name: str
    canonical_name: str
    display_name: str
    version: str
    dist_info: Path
    record_file: Path
    has_entry_points: bool = False


@dataclass(slots=True)
class MoveResult:
    package: PackageInfo
    moved: int = 0
    skipped_pyc: int = 0
    skipped_dist_info: int = 0
    missing: int = 0
    failed: int = 0
    removed_dirs: int = 0


def normalize_name(name: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in name).lower()


def discover_packages(site_packages: Path) -> list[PackageInfo]:
    packages: list[PackageInfo] = []
    for dist_info in site_packages.iterdir():
        if not dist_info.is_dir() or not dist_info.name.endswith(DIST_INFO_SUFFIX):
            continue
        record_file = dist_info / RECORD_FILENAME
        if not record_file.is_file():
            logger.warning("Missing RECORD file: {}", record_file)
            continue
        stem = dist_info.name[: -len(DIST_INFO_SUFFIX)]
        if "-" not in stem:
            logger.warning("Unexpected dist-info directory name: {}", dist_info.name)
            continue
        display_name, version = stem.split("-", 1)
        packages.append(
            PackageInfo(
                requested_name=display_name,
                canonical_name=normalize_name(display_name),
                display_name=display_name,
                version=version,
                dist_info=dist_info,
                record_file=record_file,
                has_entry_points=(dist_info / ENTRY_POINTS_FILENAME).is_file(),
            )
        )
    return packages


def select_packages(
    patterns: Sequence[str], packages: Sequence[PackageInfo]
) -> tuple[list[PackageInfo], dict[str, list[str]]]:
    selected: dict[str, PackageInfo] = {}
    suggestions: dict[str, list[str]] = {}
    normalized_names = [package.canonical_name for package in packages]
    display_names = [package.display_name for package in packages]

    for pattern in patterns:
        normalized_pattern = normalize_name(pattern)
        if normalized_pattern == ALL_KEYWORD:
            for package in packages:
                selected[package.canonical_name] = package
            continue

        has_wildcard = any(char in pattern for char in "*?[]")
        if has_wildcard:
            matches = [
                package
                for package in packages
                if fnmatch.fnmatchcase(package.canonical_name, normalized_pattern)
            ]
            if matches:
                for package in matches:
                    selected[package.canonical_name] = package
                continue
            logger.warning("No packages match wildcard pattern: {}", pattern)
            suggestions[pattern] = []
            continue

        exact = [
            package
            for package in packages
            if package.canonical_name == normalized_pattern
        ]
        if exact:
            selected[exact[0].canonical_name] = exact[0]
            continue

        close_matches = get_close_matches(
            normalized_pattern,
            normalized_names,
            n=8,
            cutoff=0.45,
        )
        matched_names = [
            display_name
            for display_name, normalized in zip(display_names, normalized_names)
            if normalized in close_matches
        ]
        suggestions[pattern] = matched_names

    return list(selected.values()), suggestions


def read_record(record_file: Path, site_packages: Path) -> Iterator[tuple[Path, Path]]:
    with record_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row_number, row in enumerate(reader, start=1):
            if not row:
                continue
            raw_path = row[0]
            source = Path(raw_path)
            if not source.is_absolute():
                source = site_packages / source
            try:
                relative_path = source.resolve(strict=False).relative_to(
                    site_packages.resolve(strict=False)
                )
            except ValueError:
                logger.warning(
                    "RECORD path is outside site-packages; preserving basename only: {}",
                    source,
                )
                relative_path = Path(source.name)
            yield source, relative_path


def should_skip(relative_path: Path) -> bool:
    if relative_path.suffix.lower() in PYC_SUFFIXES:
        return True
    return bool(
        relative_path.name in IGNORED_RECORD_NAMES
        and relative_path.parent.suffix == DIST_INFO_SUFFIX
    )


def is_unlisted_dist_info_file(relative_path: Path) -> bool:
    return (
        relative_path.parent.suffix == DIST_INFO_SUFFIX
        and relative_path.name not in DIST_INFO_ALLOWED_FILES
    )


def report_missing_file(source: Path) -> None:
    try:
        with MISSING_FILES_REPORT.open("a", encoding="utf-8") as handle:
            handle.write(f"{source}\n")
    except OSError as error:
        logger.error("Could not append to {}: {}", MISSING_FILES_REPORT, error)


def unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    counter = 1
    while True:
        candidate = destination.with_name(
            f"{destination.stem}_{counter}{destination.suffix}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def remove_empty_directories(directories: Iterable[Path], *, dry_run: bool) -> int:
    removed = 0
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            if not directory.is_dir():
                continue
            if any(directory.iterdir()):
                continue
            if dry_run:
                logger.info("Would remove empty directory: {}", directory)
                removed += 1
                continue
            directory.rmdir()
            removed += 1
            logger.debug("Removed empty directory: {}", directory)
        except OSError as error:
            logger.error("Could not remove empty directory {}: {}", directory, error)
    return removed


def move_package(
    package: PackageInfo,
    site_packages: Path,
    output_root: Path,
    *,
    dry_run: bool,
    include_dist_info: bool,
    overwrite: bool,
    cleanup_empty_dirs: bool,
) -> MoveResult:
    result = MoveResult(package=package)
    destination_root = output_root / package.canonical_name

    try:
        if not dry_run:
            destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.error(
            "Could not create destination directory {}: {}",
            destination_root,
            error,
        )
        result.failed += 1
        return result

    logger.info(
        "Processing {} {} -> {}",
        package.display_name,
        package.version,
        destination_root,
    )

    try:
        entries = list(read_record(package.record_file, site_packages))
    except (OSError, csv.Error) as error:
        logger.error("Could not read {}: {}", package.record_file, error)
        result.failed += 1
        return result

    candidate_dirs: set[Path] = set()

    for source, relative_path in entries:
        destination = destination_root / relative_path
        try:
            if should_skip(relative_path):
                result.skipped_pyc += 1
                logger.debug("Skipped generated/cache file: {}", source)
                continue

            if is_unlisted_dist_info_file(relative_path):
                result.skipped_dist_info += 1
                logger.debug("Skipped non-allowlisted dist-info file: {}", source)
                continue

            if relative_path.name in IGNORED_RECORD_NAMES and not include_dist_info:
                logger.debug("Skipped dist-info metadata file: {}", source)
                continue

            if not source.exists():
                result.missing += 1
                logger.warning("Missing file, skipping: {}", source)
                report_missing_file(source)
                continue

            if not source.is_file():
                result.failed += 1
                logger.error("RECORD entry is not a regular file: {}", source)
                continue

            if destination.exists():
                if not overwrite:
                    result.failed += 1
                    logger.error(
                        "Destination already exists and --overwrite was not supplied: {}",
                        destination,
                    )
                    continue
                logger.warning("Overwriting existing file: {}", destination)
            elif not overwrite:
                destination = unique_destination(destination)

            if dry_run:
                logger.info("Would move: {} -> {}", source, destination)
                result.moved += 1
                if cleanup_empty_dirs:
                    candidate_dirs.add(source.parent)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            result.moved += 1
            logger.debug("Moved: {} -> {}", source, destination)
            if cleanup_empty_dirs:
                candidate_dirs.add(source.parent)

        except (OSError, shutil.Error) as error:
            result.failed += 1
            logger.error("Failed to move {}: {}", source, error)

    if cleanup_empty_dirs:
        result.removed_dirs = remove_empty_directories(
            candidate_dirs,
            dry_run=dry_run,
        )

    logger.info(
        "Finished {}: moved={}, skipped .pyc/.pyo={}, "
        "skipped dist-info={}, missing={}, failed={}, "
        "empty dirs removed={}",
        package.display_name,
        result.moved,
        result.skipped_pyc,
        result.skipped_dist_info,
        result.missing,
        result.failed,
        result.removed_dirs,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move files belonging to installed Python packages into "
            "~/tmp/pkgs/<package>, preserving the original site-packages tree. "
            "Run from inside site-packages."
        )
    )
    parser.add_argument(
        "packages",
        nargs="*",
        metavar="PACKAGE_OR_PATTERN",
        help=(
            "One or more package names, wildcard patterns, or `all`. "
            "Examples: requests, 'req*', '*pip*', all. "
            "Optional if -a/--all is supplied."
        ),
    )
    parser.add_argument(
        "-a",
        "--all",
        dest="all_packages",
        action="store_true",
        help="Process every installed package (equivalent to passing `all`).",
    )
    parser.add_argument(
        "-e",
        "--entry-points",
        action="store_true",
        help=(
            "Only process packages that declare entry points "
            "(i.e. have an entry_points.txt in their dist-info directory)."
        ),
    )
    parser.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Destination root directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be moved and removed without changing the filesystem.",
    )
    parser.add_argument(
        "--include-dist-info",
        action="store_true",
        help=(
            "Also move metadata files such as METADATA and WHEEL. "
            "RECORD itself is always skipped to avoid breaking package metadata."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files that already exist in the destination.",
    )
    parser.add_argument(
        "--keep-empty-dirs",
        action="store_true",
        help="Do not remove source directories that become empty after moving files.",
    )
    parser.add_argument(
        "--site-packages",
        type=Path,
        default=Path.cwd(),
        help=(
            "Site-packages directory containing the packages. Defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Optional log file path, for example ~/tmp/pkgs/move_pkg.log.",
    )
    return parser


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
        ),
    )
    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file),
            level="DEBUG",
            rotation="10 MB",
            retention=5,
            enqueue=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose, args.log_file)

    patterns: list[str] = list(args.packages)
    if args.all_packages:
        patterns.append(ALL_KEYWORD)
    if not patterns:
        parser.error("Provide at least one PACKAGE_OR_PATTERN or pass -a/--all.")

    site_packages = args.site_packages.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()

    if not site_packages.is_dir():
        logger.error("Site-packages directory does not exist: {}", site_packages)
        return 2

    try:
        packages = discover_packages(site_packages)
    except OSError as error:
        logger.error("Could not scan {}: {}", site_packages, error)
        return 2

    if not packages:
        logger.error("No installed distributions with RECORD files were found.")
        return 2

    selected_packages, suggestions = select_packages(patterns, packages)

    for pattern, matches in suggestions.items():
        logger.error("Package or pattern not found: {}", pattern)
        if matches:
            logger.info("Did you mean one of these? {}", ", ".join(matches))
        else:
            installed = ", ".join(sorted(package.display_name for package in packages))
            logger.info("Installed packages: {}", installed)

    if not selected_packages:
        return 1

    if args.entry_points:
        filtered: list[PackageInfo] = [
            package for package in selected_packages if package.has_entry_points
        ]
        excluded = len(selected_packages) - len(filtered)
        if excluded:
            logger.info("Excluded {} package(s) without entry points.", excluded)
        if not filtered:
            logger.error("None of the selected packages declare entry points.")
            return 1
        selected_packages = filtered

    exit_code = 0
    for package in selected_packages:
        result = move_package(
            package,
            site_packages,
            output_root,
            dry_run=args.dry_run,
            include_dist_info=args.include_dist_info,
            overwrite=args.overwrite,
            cleanup_empty_dirs=not args.keep_empty_dirs,
        )
        if result.failed:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
