#!/data/data/com.termux/files/usr/bin/env python
"""Compress the subdirectories of the current folder into tar.zst, tar.br or tar.xz.
Packs each directory with tar plus zstd (level 19), brotli (quality 11) or lzma, and ``-d`` safely extracts the archives again with a path-traversal guard; the algorithm, level and recursion depth are configurable.
Usage: compsub {-c|-d} {-z|-b|-x} [paths ...] [--level N] Positional arguments: paths ...
Files/dirs to process (default: .) Options: -c, --compress Compress directories to archives -d, --decompress Decompress archives back to directories -z, --zstd -b, --brotli -x, --lzma --level Override compression level/quality for the selected algorithm --no-recursive Disable recursive scan for inputs Examples: $ compsub -c -z .
$ compsub -d -z dist/ Command-line script: run ``python compsub.py`` or the ``compsub`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
import contextlib
import lzma
from multiprocessing import Pool
from pathlib import Path
import shutil
import sys
import tarfile
from typing import Any, Self

import brotli
from dh import MAX_WORKERS, SKIP_DIRS, fsz
from loguru import logger
import zstandard as zstd


ZSTD_LEVEL: int = 19
BROTLI_QUALITY: int = 11
LZMA_LEVEL: int = 9
ARCHIVE_SUFFIXES: tuple[str, ...] = (".tar.zst", ".tar.br", ".tar.xz")


def iter_target_dirs(paths: Sequence[str], recursive: bool = True) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_dir():
            out.append(p)
            if recursive:
                for root, dirs, _ in p.walk():
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                    for d in dirs:
                        rp = root / d
                        if rp.is_dir():
                            out.append(rp)
        elif p.is_file() and _has_archive_suffix(p):
            continue
    seen: set[str] = set()
    uniq: list[Path] = []
    for d in out:
        key = str(d.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def iter_target_archives(paths: Sequence[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_file() and _has_archive_suffix(p):
            out.append(p)
        elif p.is_dir():
            for suffix in ARCHIVE_SUFFIXES:
                for f in p.rglob(f"*{suffix}"):
                    if f.is_file():
                        out.append(f)
    seen: set[str] = set()
    uniq: list[Path] = []
    for a in out:
        key = str(a.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(a)
    return uniq


def _has_archive_suffix(path: Path) -> bool:
    name = path.name
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def dir_size_bytes(path: Path) -> int:
    total = 0
    for root, dirs, files in path.walk():
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            fp = root / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _open_compressor(algo: str, level: int, f_out: Any) -> Any:
    if algo == "zstd":
        cctx = zstd.ZstdCompressor(level=level, threads=4)
        return cctx.stream_writer(f_out)
    if algo == "brotli":
        return brotli.Compressor(quality=level)
    if algo == "lzma":
        return lzma.LZMAFile(f_out, mode="wb", preset=level)
    msg = f"Unknown compression algorithm: {algo}"
    raise ValueError(msg)


def _archive_suffix(algo: str) -> str:
    if algo == "zstd":
        return ".tar.zst"
    if algo == "brotli":
        return ".tar.br"
    if algo == "lzma":
        return ".tar.xz"
    msg = f"Unknown compression algorithm: {algo}"
    raise ValueError(msg)


def _compress_with_algo(algo: str, level: int, subdir: Path, tar_path: Path) -> None:
    if algo == "brotli":
        with open(tar_path, "wb") as raw_out:
            compressor = brotli.Compressor(quality=level)
            with tarfile.open(fileobj=_BrotliWriter(raw_out, compressor), mode="w|") as tar:
                tar.add(str(subdir), arcname=subdir.name, recursive=True)
            raw_out.write(compressor.finish())
        return
    if algo == "lzma":
        with lzma.open(tar_path, "wb", preset=level) as compressed_out:
            with tarfile.open(fileobj=compressed_out, mode="w|") as tar:
                tar.add(str(subdir), arcname=subdir.name, recursive=True)
        return
    if algo == "zstd":
        cctx = zstd.ZstdCompressor(level=level, threads=4)
        with open(tar_path, "wb") as f_out, cctx.stream_writer(f_out) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tar:
                tar.add(str(subdir), arcname=subdir.name, recursive=True)
        return
    msg = f"Unknown compression algorithm: {algo}"
    raise ValueError(msg)


class _BrotliWriter:
    def __init__(self, raw: Any, compressor: Any) -> None:
        self._raw = raw
        self._compressor = compressor

    def write(self, data: bytes) -> int:
        chunk = self._compressor.process(data)
        if chunk:
            self._raw.write(chunk)
        return len(data)

    def tell(self) -> int:
        return self._raw.tell()


def compress_directory(subdir: Path, algo: str, level: int) -> dict[str, Any]:
    subdir = Path(subdir)
    suffix = _archive_suffix(algo)
    tar_path = subdir.parent / f"{subdir.name}{suffix}"
    try:
        original_size = dir_size_bytes(subdir)
        _compress_with_algo(algo, level, subdir, tar_path)
        if not tar_path.exists() or tar_path.stat().st_size == 0:
            msg = "Archive creation failed or empty"
            raise RuntimeError(msg)
        shutil.rmtree(subdir)
        compressed_size = tar_path.stat().st_size
        return {
            "success": True,
            "name": subdir.name,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "space_freed": original_size - compressed_size,
        }
    except Exception as exc:
        try:
            if tar_path.exists():
                tar_path.unlink()
        except OSError:
            pass
        return {"success": False, "name": subdir.name, "error": str(exc)}


def is_within_directory(directory: Path, target: Path) -> bool:
    directory = Path(directory).resolve()
    target = Path(target).resolve()
    return directory == target or directory in target.parents


def safe_extract_stream(tar: tarfile.TarFile, dest_dir: Path) -> None:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for member in tar:
        if member is None:
            continue
        target_path = dest_dir / member.name
        if not is_within_directory(dest_dir, target_path):
            continue
        tar.extract(member, path=str(dest_dir))


def _detect_algo(archive_path: Path) -> str:
    name = archive_path.name
    if name.endswith(".tar.zst"):
        return "zstd"
    if name.endswith(".tar.br"):
        return "brotli"
    if name.endswith(".tar.xz"):
        return "lzma"
    msg = f"Unknown archive type: {archive_path}"
    raise ValueError(msg)


def _open_decompressed_stream(archive_path: Path) -> Any:
    algo = _detect_algo(archive_path)
    if algo == "zstd":
        dctx = zstd.ZstdDecompressor()
        raw = open(archive_path, "rb")
        return _ClosingReader(dctx.stream_reader(raw), raw)
    if algo == "brotli":
        raw = open(archive_path, "rb")
        return _ClosingReader(brotli.Decompressor(), raw, brotli_raw=raw)
    if algo == "lzma":
        return lzma.open(archive_path, "rb")
    msg = f"Unknown archive type: {archive_path}"
    raise ValueError(msg)


class _ClosingReader:
    def __init__(self, reader: Any, raw: Any, brotli_raw: Any = None) -> None:
        self._reader = reader
        self._raw = raw
        self._brotli_raw = brotli_raw
        self._first = True

    def read(self, size: int = -1) -> bytes:
        if self._brotli_raw is not None:
            if self._first:
                self._first = False
                data = self._raw.decompress(self._brotli_raw.read())
                return data if size < 0 else data[:size]
            return b""
        return self._reader.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        with contextlib.suppress(Exception):
            self._reader.close()
        with contextlib.suppress(Exception):
            self._raw.close()


def decompress_archive(archive_path: Path) -> dict[str, Any]:
    archive_path = Path(archive_path)
    try:
        archive_size = archive_path.stat().st_size
        extracted_size = 0
        with (
            _open_decompressed_stream(archive_path) as stream,
            tarfile.open(fileobj=stream, mode="r|") as tar,
        ):
            for member in tar:
                if member is None:
                    continue
                extracted_size += int(getattr(member, "size", 0) or 0)
        dir_name = archive_path.stem
        if archive_path.name.endswith(".tar.zst"):
            dir_name = archive_path.name[: -len(".tar.zst")]
        elif archive_path.name.endswith(".tar.br"):
            dir_name = archive_path.name[: -len(".tar.br")]
        elif archive_path.name.endswith(".tar.xz"):
            dir_name = archive_path.name[: -len(".tar.xz")]
        target_dir = archive_path.parent / dir_name
        with (
            _open_decompressed_stream(archive_path) as stream,
            tarfile.open(fileobj=stream, mode="r|") as tar,
        ):
            safe_extract_stream(tar, target_dir)
        archive_path.unlink()
        space_used = extracted_size - archive_size
        return {
            "success": True,
            "name": archive_path.name,
            "archive_size": archive_size,
            "extracted_size": extracted_size,
            "space_used": space_used,
        }
    except Exception as exc:
        return {"success": False, "name": archive_path.name, "error": str(exc)}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compress/decompress subdirectories with tar + (zstd|brotli|lzma)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-c", "--compress", action="store_true", help="Compress directories to archives")
    group.add_argument(
        "-d",
        "--decompress",
        action="store_true",
        help="Decompress archives back to directories",
    )
    algo = parser.add_mutually_exclusive_group()
    algo.add_argument(
        "-z",
        "--zstd",
        default=True,
        action="store_true",
        help=f"Use zstandard (level {ZSTD_LEVEL})",
    )
    algo.add_argument(
        "-b",
        "--brotli",
        action="store_true",
        help=f"Use brotli (quality {BROTLI_QUALITY})",
    )
    algo.add_argument(
        "-x",
        "--lzma",
        action="store_true",
        help=f"Use lzma (level {LZMA_LEVEL})",
    )
    parser.add_argument("paths", nargs="*", default=None, help="Files/dirs to process (default: .)")
    parser.add_argument(
        "--level",
        type=int,
        default=None,
        help="Override compression level/quality for the selected algorithm",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Disable recursive scan for inputs")
    return parser


def _resolve_algo(args: argparse.Namespace) -> str:
    if args.brotli:
        return "brotli"
    if args.lzma:
        return "lzma"
    return "zstd"


def _resolve_level(algo: str, level_arg: int | None) -> int:
    if level_arg is not None:
        return level_arg
    if algo == "brotli":
        return BROTLI_QUALITY
    if algo == "lzma":
        return LZMA_LEVEL
    return ZSTD_LEVEL


def _run_compression(args: argparse.Namespace) -> int:
    paths: Sequence[str] = args.paths or ["."]
    recursive = not args.no_recursive
    algo = _resolve_algo(args)
    level = _resolve_level(algo, args.level)
    subdirs = [d for d in iter_target_dirs(paths, recursive=recursive) if d.is_dir()]
    if not subdirs:
        print("No subdirectories found to compress.")
        return 0
    print(f"Found {len(subdirs)} directories to compress.")
    print(f"Starting compression with {algo} (level {level})...")
    total_original = 0
    total_compressed = 0
    successful = 0
    failed = 0
    with Pool(processes=MAX_WORKERS) as pool:
        async_results = [(d, pool.apply_async(compress_directory, (d, algo, level))) for d in subdirs]
        for subdir, ar in async_results:
            try:
                result: dict[str, Any] = ar.get()
            except Exception as exc:
                failed += 1
                logger.error(f"✗ {subdir.name}: Failed - {exc}")
                continue
            if result.get("success"):
                successful += 1
                total_original += int(result["original_size"])
                total_compressed += int(result["compressed_size"])
                print(
                    f"✓ {result['name']}: {fsz(result['original_size'])} -> "
                    f"{fsz(result['compressed_size'])} "
                    f"(freed {fsz(result['space_freed'])})"
                )
            else:
                failed += 1
                logger.error(f"✗ {result.get('name', subdir.name)}: Failed - {result.get('error')}")
    print("=" * 40)
    print(f"Compression complete: {successful} successful, {failed} failed")
    if successful > 0:
        total_freed = total_original - total_compressed
        compression_ratio = (1 - total_compressed / total_original) * 100 if total_original else 0.0
        print(f"Total original size:   {fsz(total_original)}")
        print(f"Total compressed size: {fsz(total_compressed)}")
        print(f"Total space freed:     {fsz(total_freed)}")
        print(f"Compression ratio:     {compression_ratio:.1f}%")
    return 0


def _run_decompression(args: argparse.Namespace) -> int:
    paths: Sequence[str] = args.paths or ["."]
    archives = [a for a in iter_target_archives(paths) if a.is_file()]
    if not archives:
        print("No archives found to decompress.")
        return 0
    print(f"Found {len(archives)} archives to decompress.")
    print("Starting decompression...")
    total_archive = 0
    total_extracted = 0
    successful = 0
    failed = 0
    with Pool(processes=MAX_WORKERS) as pool:
        async_results = [(a, pool.apply_async(decompress_archive, (a,))) for a in archives]
        for archive, ar in async_results:
            try:
                result: dict[str, Any] = ar.get()
            except Exception as exc:
                failed += 1
                logger.error(f"✗ {archive.name}: Failed - {exc}")
                continue
            if result.get("success"):
                successful += 1
                total_archive += int(result["archive_size"])
                total_extracted += int(result["extracted_size"])
                space_change = int(result["space_used"])
                if space_change >= 0:
                    change_str = f"(space used: +{fsz(space_change)})"
                else:
                    change_str = f"(space freed: {fsz(-space_change)})"
                print(
                    f"✓ {result['name']}: {fsz(result['archive_size'])} -> {fsz(result['extracted_size'])} {change_str}"
                )
            else:
                failed += 1
                logger.error(f"✗ {result.get('name', archive.name)}: Failed - {result.get('error')}")
    print("=" * 40)
    print(f"Decompression complete: {successful} successful, {failed} failed")
    if successful > 0:
        total_change = total_extracted - total_archive
        print(f"Total archive size:     {fsz(total_archive)}")
        print(f"Total extracted size:   {fsz(total_extracted)}")
        print(f"Net space change:       {fsz(total_change)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.compress:
        return _run_compression(args)
    return _run_decompression(args)


if __name__ == "__main__":
    try:
        import zstandard
    except ImportError:
        logger.error("zstandard package is required. Install it with: pip install zstandard")
        sys.exit(1)
    raise SystemExit(main())
