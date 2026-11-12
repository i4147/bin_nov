#!/data/data/com.termux/files/usr/bin/env python
"""Compress or decompress the top-level directories of a folder in parallel.
Packs each subdirectory into a tar with xz, gz, bz2, zstd, lz4 or snappy (``-c``), or extracts the archives back (``-d``), with ``--min``/``--max`` size filters, ``--include`` globs, ``--keep``/``--remove-source`` handling and a choice of the multiprocessing or threading pool.
Usage: compress_subdirs -c xz [-C DIR] [--min SIZE] [--max SIZE] Options: -C, --directory Target root directory -c, --compressor Compression algorithm -d, --decompress Decompress recognized archives -f, --force Replace existing archives when compressing; never overwrite extracted directories -k, --keep Keep source directories or archives --remove-source Remove sources after successful completion --min Skip directories smaller than this recursive regular-file size --max, -max Skip directories larger than this recursive regular-file size --include Include matching top-level names; repeatable --exclude Exclude matching top-level names; repeatable ...
plus 4 more; run ``compress_subdirs --help`` for the full list.
Examples: $ compress_subdirs -c zstd -C ~/projects --remove-source Command-line script: run ``python compress_subdirs.py`` or the ``compress_subdirs`` command.
The module is importable as well."""

import argparse
import bz2
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal
import fnmatch
import gzip
import lzma
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
from typing import BinaryIO, Callable, Final, Literal, TypeVar, cast

from dh import MAX_WORKERS

Compressor = Literal["xz", "gz", "bz2", "zstd", "lz4", "snappy"]
Method = Literal["mp-map", "mp-imap", "process-pool", "thread-pool"]
Status = Literal["ok", "skipped", "error"]
Result = tuple[Path, Status, str]
CompressTask = tuple[Path, Compressor, bool, bool, int | None, bool]
DecompressTask = tuple[Path, bool]

T = TypeVar("T")
R = TypeVar("R")

COMPRESSORS: Final[tuple[Compressor, ...]] = ("xz", "gz", "bz2", "zstd", "lz4", "snappy")
EXTENSIONS: Final[dict[Compressor, str]] = {
    "xz": ".tar.xz",
    "gz": ".tar.gz",
    "bz2": ".tar.bz2",
    "zstd": ".tar.zst",
    "lz4": ".tar.lz4",
    "snappy": ".tar.snappy",
}
LEVEL_RANGES: Final[dict[Compressor, tuple[int, int]]] = {
    "xz": (0, 9),
    "gz": (0, 9),
    "bz2": (1, 9),
    "zstd": (-5, 22),
    "lz4": (0, 16),
}
DEFAULT_LEVELS: Final[dict[Compressor, int]] = {
    "xz": 9,
    "gz": 9,
    "bz2": 9,
    "zstd": 20,
    "lz4": 16,
}
SNAPPY_MAGIC: Final[bytes] = b"TARDIRS-SNAPPY\x01"
SNAPPY_CHUNK_SIZE: Final[int] = 1024 * 1024
SNAPPY_MAX_BLOCK_SIZE: Final[int] = 2 * SNAPPY_CHUNK_SIZE


def parse_size(value: str) -> int:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        msg = "Expected a size such as 1024, 500KB, 1.5MB, or 2GiB"
        raise argparse.ArgumentTypeError(msg)

    units: dict[str, int] = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return int(Decimal(match.group(1)) * units[(match.group(2) or "B").upper()])


def format_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    msg = "Unreachable"
    raise AssertionError(msg)


def directory_size(directory: Path) -> int:
    total = 0
    pending: list[Path] = [directory]

    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size

    return total


def archive_format(archive: Path) -> tuple[Compressor, str]:
    for compressor, extension in EXTENSIONS.items():
        if archive.name.endswith(extension):
            name = archive.name[: -len(extension)]
            if name in ("", ".", ".."):
                msg = f"Invalid archive name: {archive.name}"
                raise ValueError(msg)
            return compressor, name
    msg = f"Unsupported archive format: {archive}"
    raise ValueError(msg)


def check_dependencies(compressor: Compressor) -> None:
    if compressor == "zstd":
        try:
            import zstandard as zstd
        except ImportError:
            import zstandard as zstd
    elif compressor == "lz4":
        import lz4.frame
    elif compressor == "snappy":
        from snapy import compress, decompress


def _open_writer(
    path: Path,
    compressor: Compressor,
    level: int | None,
    extreme: bool,
) -> BinaryIO:
    selected_level = DEFAULT_LEVELS.get(compressor, 0) if level is None else level

    if compressor == "xz":
        preset = selected_level | (lzma.PRESET_EXTREME if extreme else 0)
        return cast("BinaryIO", lzma.open(path, "wb", preset=preset))
    if compressor == "gz":
        return cast("BinaryIO", gzip.open(path, "wb", compresslevel=selected_level))
    if compressor == "bz2":
        return cast("BinaryIO", bz2.open(path, "wb", compresslevel=selected_level))
    if compressor == "zstd":
        try:
            from compression import zstd
        except ImportError:
            import zstandard

            return cast(
                "BinaryIO",
                zstandard.open(str(path), "wb", level=selected_level),
            )
        return cast("BinaryIO", zstd.open(path, "wb", level=selected_level))
    if compressor == "lz4":
        import lz4.frame

        return cast(
            "BinaryIO",
            lz4.frame.open(
                path,
                "wb",
                compression_level=selected_level,
                block_size=lz4.frame.BLOCKSIZE_MAX4MB,
                block_linked=True,
                content_checksum=True,
            ),
        )
    msg = f"Unsupported streaming compressor: {compressor}"
    raise ValueError(msg)


def _open_reader(archive: Path, compressor: Compressor) -> BinaryIO:
    if compressor == "xz":
        return cast("BinaryIO", lzma.open(archive, "rb"))
    if compressor == "gz":
        return cast("BinaryIO", gzip.open(archive, "rb"))
    if compressor == "bz2":
        return cast("BinaryIO", bz2.open(archive, "rb"))
    if compressor == "zstd":
        try:
            from compression import zstd
        except ImportError:
            import zstandard

            return cast("BinaryIO", zstandard.open(str(archive), "rb"))
        return cast("BinaryIO", zstd.open(archive, "rb"))
    if compressor == "lz4":
        import lz4.frame

        return cast("BinaryIO", lz4.frame.open(archive, "rb"))
    msg = f"Unsupported streaming compressor: {compressor}"
    raise ValueError(msg)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.read(size - len(output))
        if not chunk:
            msg = "Truncated Snappy archive"
            raise ValueError(msg)
        output.extend(chunk)
    return bytes(output)


def _write_snappy(source: BinaryIO, destination: BinaryIO) -> None:
    from snapy import compress

    destination.write(SNAPPY_MAGIC)
    while block := source.read(SNAPPY_CHUNK_SIZE):
        encoded = bytes(compress(block))
        if not encoded or len(encoded) > SNAPPY_MAX_BLOCK_SIZE:
            msg = "Invalid compressed Snappy block size"
            raise ValueError(msg)
        destination.write(struct.pack(">II", len(encoded), len(block)))
        destination.write(encoded)
    destination.write(struct.pack(">II", 0, 0))


def _read_snappy(source: BinaryIO, destination: BinaryIO) -> None:
    from snapy import decompress

    if _read_exact(source, len(SNAPPY_MAGIC)) != SNAPPY_MAGIC:
        msg = "Not a tardirs chunked Snappy archive"
        raise ValueError(msg)

    while True:
        compressed_size, original_size = struct.unpack(">II", _read_exact(source, 8))
        if compressed_size == 0 and original_size == 0:
            if source.read(1):
                msg = "Unexpected trailing Snappy data"
                raise ValueError(msg)
            return
        if not 0 < compressed_size <= SNAPPY_MAX_BLOCK_SIZE:
            msg = "Invalid compressed Snappy block size"
            raise ValueError(msg)
        if not 0 < original_size <= SNAPPY_CHUNK_SIZE:
            msg = "Invalid uncompressed Snappy block size"
            raise ValueError(msg)

        block = bytes(decompress(_read_exact(source, compressed_size)))
        if len(block) != original_size:
            msg = "Snappy block length mismatch"
            raise ValueError(msg)
        destination.write(block)


def _compress_one(task: CompressTask) -> Result:
    directory, compressor, force, keep, level, extreme = task
    archive = directory.with_name(directory.name + EXTENSIONS[compressor])

    try:
        if Path(archive).exists() or (Path(archive).is_symlink() and not force):
            return archive, "skipped", "Archive already exists"

        with tempfile.TemporaryDirectory(prefix=".tardirs-", dir=directory.parent) as temporary:
            temporary_root = Path(temporary)
            output = temporary_root / archive.name

            if compressor == "snappy":
                with tempfile.TemporaryFile(mode="w+b", dir=temporary_root) as raw:
                    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
                        tar.add(directory, arcname=directory.name)
                    raw.seek(0)
                    with output.open("wb") as destination:
                        _write_snappy(raw, destination)
            else:
                with _open_writer(output, compressor, level, extreme) as writer:
                    with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                        tar.add(directory, arcname=directory.name)

            if force:
                os.replace(output, archive)
            else:
                try:
                    os.link(output, archive)
                except FileExistsError:
                    return archive, "skipped", "Archive created by another task"

        if not keep:
            shutil.rmtree(directory)
        return archive, "ok", "Kept source" if keep else "Removed source"
    except Exception as exc:
        return archive, "error", str(exc)


def _extract_tar(reader: BinaryIO, staging: Path, expected_name: str) -> None:
    def safe_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
        filtered = tarfile.data_filter(member, destination)
        if filtered is None:
            return None

        parts = Path(filtered.name).parts
        if not parts or parts[0] != expected_name or ".." in parts:
            msg = f"Archive member is outside {expected_name!r}: {filtered.name}"
            raise ValueError(msg)
        if filtered.issym() or filtered.islnk():
            msg = f"Archive links are not allowed: {filtered.name}"
            raise ValueError(msg)
        if not (filtered.isfile() or filtered.isdir()):
            msg = f"Unsupported archive member: {filtered.name}"
            raise ValueError(msg)
        return filtered

    with tarfile.open(fileobj=reader, mode="r|") as tar:
        tar.extractall(path=staging, filter=safe_filter)


def _decompress_one(task: DecompressTask) -> Result:
    archive, keep = task
    try:
        compressor, name = archive_format(archive)
        destination = archive.parent / name
        if Path(destination).exists() or Path(destination).is_symlink():
            return archive, "skipped", f"Destination already exists: {destination}"

        with tempfile.TemporaryDirectory(prefix=".tardirs-", dir=archive.parent) as temporary:
            staging = Path(temporary)
            if compressor == "snappy":
                with tempfile.TemporaryFile(mode="w+b", dir=staging) as raw:
                    with archive.open("rb") as source:
                        _read_snappy(source, raw)
                    raw.seek(0)
                    _extract_tar(raw, staging, name)
            else:
                with _open_reader(archive, compressor) as reader:
                    _extract_tar(reader, staging, name)

            extracted = staging / name
            if not extracted.is_dir() or extracted.is_symlink():
                msg = f"Archive must contain a directory named {name!r}"
                raise ValueError(msg)

            destination.mkdir()
            try:
                os.replace(extracted, destination)
            except BaseException:
                destination.rmdir()
                raise

        if not keep:
            archive.unlink()
        return destination, "ok", "Kept archive" if keep else "Removed archive"
    except Exception as exc:
        return archive, "error", str(exc)


def parallel_map(
    method: Method,
    func: Callable[[T], R],
    items: list[T],
    max_workers: int,
) -> list[R]:
    if method == "thread-pool":
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    if method == "process-pool":
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    if method == "mp-map":
        with mp.Pool(processes=max_workers) as pool:
            return pool.map(func, items)
    if method == "mp-imap":
        with mp.Pool(processes=max_workers) as pool:
            return list(pool.imap_unordered(func, items))
    msg = f"Unknown execution method: {method}"
    raise ValueError(msg)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tardirs",
        description="Compress or decompress top-level directories in parallel.",
        epilog=(
            "Requires Python 3.12+. Sizes use decimal KB/MB or binary KiB/MiB. "
            "Size limits are inclusive and apply only when compressing. "
            "Snappy uses a custom chunked container, not standard Snappy framing; "
            "it uses temporary disk space for the uncompressed tar. "
            "Extraction rejects links and special files. "
            "Do not modify source directories during compression."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-C",
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="Target root directory",
    )
    parser.add_argument(
        "-c",
        "--compressor",
        choices=COMPRESSORS,
        default="lz4",
        help="Compression algorithm",
    )
    parser.add_argument(
        "-d",
        "--decompress",
        action="store_true",
        help="Decompress recognized archives",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Replace existing archives when compressing; never overwrite extracted directories",
    )
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument(
        "-k",
        "--keep",
        dest="keep",
        action="store_true",
        help="Keep source directories or archives",
    )
    retention.add_argument(
        "--remove-source",
        dest="keep",
        action="store_false",
        help="Remove sources after successful completion",
    )
    parser.set_defaults(keep=True)
    parser.add_argument(
        "--min",
        dest="min_size",
        type=parse_size,
        help="Skip directories smaller than this recursive regular-file size",
    )
    parser.add_argument(
        "--max",
        "-max",
        dest="max_size",
        type=parse_size,
        help="Skip directories larger than this recursive regular-file size",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Include matching top-level names; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude matching top-level names; repeatable",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show selected targets without changing files",
    )
    parser.add_argument(
        "--level",
        type=int,
        help="Compression level: xz 0–9, gz 0–9, bz2 1–9, zstd -5–22, lz4 0–16",
    )
    parser.add_argument(
        "--extreme",
        action="store_true",
        help="Enable the slower xz extreme preset",
    )
    parser.add_argument(
        "--mp-method",
        choices=("mp-map", "mp-imap", "process-pool", "thread-pool"),
        default="process-pool",
        help="Parallel execution backend",
    )
    ns = parser.parse_args()

    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required")
    if MAX_WORKERS < 0:
        parser.error("--jobs must be nonnegative")
    if ns.min_size is not None and ns.max_size is not None and ns.min_size > ns.max_size:
        parser.error("--min cannot exceed --max")
    if ns.decompress and (
        ns.min_size is not None or ns.max_size is not None or ns.level is not None or ns.extreme or ns.force
    ):
        parser.error("--min, --max, --level, --extreme, and --force are compression-only")

    compressor = cast("Compressor", ns.compressor)
    if ns.level is not None:
        if compressor not in LEVEL_RANGES:
            parser.error("Snappy does not support --level")
        low, high = LEVEL_RANGES[compressor]
        if not low <= ns.level <= high:
            parser.error(f"--level for {compressor} must be between {low} and {high}")
    if ns.extreme and compressor != "xz":
        parser.error("--extreme requires --compressor xz")

    root = cast("Path", ns.directory).resolve()
    if not root.is_dir():
        parser.error(f"Not a directory: {root}")

    targets: list[Path] = []
    scan_errors = 0
    destinations: set[str] = set()

    try:
        entries = sorted(root.iterdir())
        for path in entries:
            if path.is_symlink():
                continue
            if ns.decompress:
                if not path.is_file() or not path.name.endswith(tuple(EXTENSIONS.values())):
                    continue
            elif not path.is_dir():
                continue

            if ns.include and not any(fnmatch.fnmatchcase(path.name, pattern) for pattern in ns.include):
                continue
            if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in ns.exclude):
                continue

            if ns.decompress:
                _, name = archive_format(path)
                if name in destinations:
                    parser.error(f"Multiple selected archives target {name!r}")
                destinations.add(name)
            elif ns.min_size is not None or ns.max_size is not None:
                try:
                    size = directory_size(path)
                except OSError as exc:
                    print(f"error: {path}: {exc}", file=sys.stderr)
                    scan_errors += 1
                    continue
                if ns.min_size is not None and size < ns.min_size:
                    print(f"skipped: {path}: {format_size(size)} below minimum")
                    continue
                if ns.max_size is not None and size > ns.max_size:
                    print(f"skipped: {path}: {format_size(size)} above maximum")
                    continue

            targets.append(path)
    except OSError as exc:
        parser.error(str(exc))

    if not targets:
        print("No matching targets.")
        raise SystemExit(1 if scan_errors else 0)

    if ns.dry_run:
        for path in targets:
            if ns.decompress:
                _, name = archive_format(path)
                destination = path.parent / name
            else:
                destination = path.with_name(path.name + EXTENSIONS[compressor])
            print(f"{path} -> {destination}")
        raise SystemExit(1 if scan_errors else 0)

    required: set[Compressor] = {archive_format(path)[0] for path in targets} if ns.decompress else {compressor}
    for algorithm in sorted(required):
        try:
            check_dependencies(algorithm)
        except ImportError as exc:
            parser.error(f"Missing dependency for {algorithm}: {exc}")

    workers = min(MAX_WORKERS or (os.cpu_count() or 1), len(targets))
    method = cast("Method", ns.mp_method)
    action = "Decompressing" if ns.decompress else "Compressing"
    print(f"{action} {len(targets)} target(s), {workers} worker(s), {method}")

    results: list[Result]
    if ns.decompress:
        decompress_tasks: list[DecompressTask] = [(path, bool(ns.keep)) for path in targets]
        results = parallel_map(method, _decompress_one, decompress_tasks, workers)
    else:
        compress_tasks: list[CompressTask] = [
            (
                path,
                compressor,
                bool(ns.force),
                bool(ns.keep),
                ns.level,
                bool(ns.extreme),
            )
            for path in targets
        ]
        results = parallel_map(method, _compress_one, compress_tasks, workers)

    counts: dict[Status, int] = {"ok": 0, "skipped": 0, "error": scan_errors}
    for path, status, message in results:
        counts[status] += 1
        stream = sys.stderr if status == "error" else sys.stdout
        print(f"{status}: {path}: {message}", file=stream)

    print(f"Completed: {counts['ok']} succeeded, {counts['skipped']} skipped, {counts['error']} failed")
    raise SystemExit(1 if counts["error"] else 0)


if __name__ == "__main__":
    mp.freeze_support()
    main()
