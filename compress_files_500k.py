#!/data/data/com.termux/files/usr/bin/env python
"""Compress the files above 500 KB below the current directory.
Streams each large file through xz (or the ``-b`` algorithm: gz, bz2, br, zst, lz4, 7z) in 1 MB chunks and replaces it with the archive; ``-d`` decompresses the recognised archives and ``--list``/``--show-reduction`` only the report the candidates.
Usage: compress_files_500k [-b ALGO] [-d] [--list] [--show-reduction] Options: -b Compression algorithm (default: xz) -d Decompress supported archive files instead of compressing --list Print matching filenames without compressing or deleting files --show-reduction With --list, test-compress files and show their size reduction Examples: $ compress_files_500k -b zst --show-reduction Command-line script: run ``python compress_files_500k.py`` or the ``compress_files_500k`` command.
The module is importable as well."""

import argparse
import bz2
import gzip
import lzma
import os
from pathlib import Path
import shutil
import sys
import tempfile

THRESHOLD: int = 500 * 1024
CHUNK_SIZE: int = 1024 * 1024
ARCHIVE_SUFFIXES: dict[str, str] = {
    ".xz": "",
    ".gz": "",
    ".bz2": "",
    ".br": "",
    ".zst": "",
    ".lz4": "",
    ".7z": "",
}


def list_matches(root: Path, algorithm: str, show_reduction: bool) -> None:
    suffixes = (".xz", ".gz", ".bz2", ".br", ".zst", ".lz4", ".7z")
    for source in root.rglob("*"):
        if not source.is_file() or source.stat().st_size <= THRESHOLD:
            continue
        if source.name.lower().endswith(suffixes):
            continue
        if not show_reduction:
            print(source)
            continue
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".compress-test-", delete=False) as temp:
                temp_path = Path(temp.name)
            compress_stream(source, temp_path, algorithm)
            original_size = source.stat().st_size
            compressed_size = temp_path.stat().st_size
            reduction = (original_size - compressed_size) / original_size * 100
            print(f"{source}  ({reduction:.1f}% reduction)")
        except Exception as exc:
            print(f"ERROR testing {source}: {exc}", file=sys.stderr)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


def compress_stream(source: Path, destination: Path, algorithm: str) -> None:
    if algorithm == "xz":
        with source.open("rb") as src, lzma.open(destination, "wb", preset=6) as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "gz":
        with source.open("rb") as src, gzip.open(destination, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "bz2":
        with source.open("rb") as src, bz2.open(destination, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "zstd":
        import zstandard

        compressor = zstandard.ZstdCompressor(level=3)
        with source.open("rb") as src, destination.open("wb") as dst:
            compressor.copy_stream(src, dst)
    elif algorithm == "br":
        import brotli

        compressor = brotli.Compressor()
        with source.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(CHUNK_SIZE):
                dst.write(compressor.process(chunk))
            dst.write(compressor.finish())
    elif algorithm == "lz4":
        import lz4.frame

        with source.open("rb") as src, lz4.frame.open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "7z":
        import py7zr

        with py7zr.SevenZipFile(destination, "w") as archive:
            archive.write(source, arcname=source.name)


def decompress_stream(source: Path, destination: Path, algorithm: str) -> None:
    if algorithm == "xz":
        opener = lzma.open
        with opener(source, "rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "gz":
        with gzip.open(source, "rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "bz2":
        with bz2.open(source, "rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
    elif algorithm == "zstd":
        import zstandard

        with source.open("rb") as src, destination.open("wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)
    elif algorithm == "br":
        import brotli

        decompressor = brotli.Decompressor()
        with source.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(CHUNK_SIZE):
                dst.write(decompressor.process(chunk))
    elif algorithm == "lz4":
        import lz4.frame

        with lz4.frame.open(source, "rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)


def algorithm_for(path: Path):
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return None, None
    for suffix, algorithm in (
        (".xz", "xz"),
        (".gz", "gz"),
        (".bz2", "bz2"),
        (".br", "br"),
        (".zst", "zstd"),
        (".lz4", "lz4"),
        (".7z", "7z"),
    ):
        if name.endswith(suffix):
            return algorithm, path.with_name(path.name[: -len(suffix)])
    return None, None


def compress_tree(root: Path, algorithm: str) -> None:
    freed = 0
    processed = 0
    files = list(root.rglob("*"))
    for source in files:
        if not source.is_file() or source.stat().st_size <= THRESHOLD:
            continue
        if source.name.lower().endswith(tuple(ARCHIVE_SUFFIXES)):
            continue
        suffix = {"xz": ".xz", "gz": ".gz", "bz2": ".bz2", "zstd": ".zst", "br": ".br", "lz4": ".lz4", "7z": ".7z"}[
            algorithm
        ]
        destination = source.with_name(source.name + suffix)
        if destination.exists():
            print(f"SKIP {source}: output already exists: {destination.name}")
            continue
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".compress-", delete=False) as temp:
                temp_path = Path(temp.name)
            compress_stream(source, temp_path, algorithm)
            compressed_size = temp_path.stat().st_size
            original_size = source.stat().st_size
            os.replace(temp_path, destination)
            temp_path = None
            source.unlink()
            ratio = compressed_size / original_size * 100 if original_size else 0
            saved = original_size - compressed_size
            freed += saved
            processed += 1
            print(
                f"{source}: {original_size:,} -> {compressed_size:,} bytes "
                f"({ratio:.1f}% of original; saved {saved:,} bytes)"
            )
        except Exception as exc:
            print(f"ERROR compressing {source}: {exc}", file=sys.stderr)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
    print(f"\nCompressed {processed} file(s); net space freed: {freed:,} bytes")


def decompress_tree(root: Path) -> None:
    archives = list(root.rglob("*"))
    freed = 0
    processed = 0
    for source in archives:
        if not source.is_file():
            continue
        algorithm, destination = algorithm_for(source)
        if not algorithm:
            continue
        if algorithm == "7z":
            print(f"SKIP {source}: 7z extraction is not supported by this single-file script")
            continue
        if destination.exists():
            print(f"SKIP {source}: output already exists: {destination.name}")
            continue
        archive_size = source.stat().st_size
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".decompress-", delete=False) as temp:
                temp_path = Path(temp.name)
            decompress_stream(source, temp_path, algorithm)
            output_size = temp_path.stat().st_size
            os.replace(temp_path, destination)
            temp_path = None
            source.unlink()
            net_freed = archive_size - output_size
            freed += net_freed
            processed += 1
            ratio = archive_size / output_size * 100 if output_size else 0
            print(
                f"{source}: {archive_size:,} -> {output_size:,} bytes "
                f"(archive is {ratio:.1f}% of output; net space freed {net_freed:,} bytes)"
            )
        except Exception as exc:
            print(f"ERROR decompressing {source}: {exc}", file=sys.stderr)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
    print(f"\nDecompressed {processed} file(s); net space freed: {freed:,} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recursively compress or decompress files under the current folder.")
    parser.add_argument(
        "-b",
        choices=("xz", "zstd", "gz", "bz2", "br", "lz4", "7z"),
        default="xz",
        help="compression algorithm (default: xz)",
    )
    parser.add_argument(
        "-d",
        action="store_true",
        help="decompress supported archive files instead of compressing",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print matching filenames without compressing or deleting files",
    )
    parser.add_argument(
        "--show-reduction",
        action="store_true",
        help="with --list, test-compress files and show their size reduction",
    )
    args = parser.parse_args()
    if args.list:
        list_matches(Path(), args.b, args.show_reduction)
    elif args.d:
        decompress_tree(Path())
    else:
        compress_tree(Path(), args.b)


if __name__ == "__main__":
    main()
