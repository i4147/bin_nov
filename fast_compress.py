#!/data/data/com.termux/files/usr/bin/env python
"""Compress or decompress the files of a directory with zstd or xz.
The ``zstd`` subcommand compresses with 4 threads at level 3 (``--level`` 1-22) and the ``xz`` one through ``lzma_mt``; both work in a process pool, skip the excluded and package directories, keep the originals with ``--keep`` and ``--simple`` switches to a plain recursive glob.
Usage: fast_compress {zstd,xz} [--dir DIR] [-d] [--level N] Positional arguments: directory Directory to process Subcommands: zstd Compress/decompress using zstandard xz Compress/decompress using lzma_mt Options: --dir Directory to process -c, --compress Compress files (default) -d, --decompress Decompress .zst files --level Compression level 1-22 (default: 3) --threads Threads per zstd call (default: 4) --pool-workers Multiprocessing pool size (default: 8) --sequential Disable multiprocessing and run in-process --keep, --keep-original Keep original files --chunk-size Streaming chunk size in bytes --scan-order File scan order ...
plus 16 more; run ``fast_compress --help`` for the full list.
Examples: $ fast_compress zstd --dir logs --level 19 Command-line script: run ``python fast_compress.py`` or the ``fast_compress`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
import contextlib
from dataclasses import dataclass
import fnmatch
import json
from multiprocessing import Pool
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from dh import DEFAULT_XZ_MAX_WORKERS, DEFAULT_ZSTD_MAX_WORKERS
from loguru import logger
import zstandard as zstd


try:
    import lzma_mt
except ImportError:
    lzma_mt = None
try:
    from dh import fsz
except ImportError:

    def fsz(num: float) -> str:
        value = float(num)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(value) < 1024.0:
                return f"{value:3.1f} {unit}"
            value /= 1024.0
        return f"{value:.1f} PB"


DEFAULT_ZSTD_LEVEL = 3
DEFAULT_ZSTD_THREADS = 4
DEFAULT_ZSTD_CHUNK_SIZE = 131_072
DEFAULT_XZ_PRESET = 9
DEFAULT_XZ_THREADS = 4
ZSTD_EXTENSIONS = frozenset({".zst"})
ZSTD_SKIP_EXTENSIONS = frozenset({
    ".xz",
    ".gz",
    ".7z",
    ".zip",
    ".whl",
    ".lz4",
    ".zst",
    ".br",
    ".bz2",
    ".lzma",
    ".z",
    ".rar",
    ".tar",
    ".tgz",
    ".tbz2",
    ".bz3",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".svg",
    ".ico",
    ".heic",
    ".heif",
    ".avif",
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
    ".ts",
    ".m2ts",
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
    ".m4a",
    ".opus",
    ".mid",
    ".midi",
    ".aiff",
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
    ".mobi",
    ".azw",
    ".azw3",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".iso",
    ".img",
    ".deb",
    ".rpm",
    ".pkg",
    ".msi",
})
ZSTD_MEDIA_EXTENSIONS = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".svg",
    ".ico",
    ".heic",
    ".heif",
    ".avif",
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
    ".ts",
    ".m2ts",
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
    ".m4a",
    ".opus",
    ".mid",
    ".midi",
    ".aiff",
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
    ".mobi",
    ".azw",
    ".azw3",
})
ZSTD_EXCLUDED_DIR_NAMES = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".bin",
    "bin",
    "dh",
    "print_persian",
    ".dist-info",
    ".egg-info",
    "zstandard",
})
ZSTD_EXCLUDED_DIR_PATTERNS = ["*.egg-info", "*.dist-info"]
XZ_SKIP_EXTENSIONS = frozenset({
    ".zip",
    ".br",
    ".xz",
    ".gz",
    ".bz2",
    ".bz3",
    ".zst",
    ".7z",
    ".lz4",
    ".rar",
    ".tar",
    ".tgz",
    ".tbz",
    ".tbz2",
    ".z",
    ".lz",
    ".lzma",
    ".xza",
})
XZ_EXCLUDED_DIR_NAMES = frozenset({".git", "__pycache__", ".venv", "venv", ".env", "node_modules"})


@dataclass
class Stats:
    original_size: int = 0
    compressed_size: int = 0

    def add(self, original: int, compressed: int) -> None:
        self.original_size += original
        self.compressed_size += compressed

    def savings(self, scale: float = 100.0) -> tuple[int, float, float]:
        if self.original_size == 0:
            return (0, 0.0, 0.0)
        saved = self.original_size - self.compressed_size
        compressed_pct = self.compressed_size / self.original_size * scale
        saved_pct = saved / self.original_size * scale
        return (saved, compressed_pct, saved_pct)


@dataclass
class ScanStats:
    dirs: int = 0
    files: int = 0
    skipped_symlinks: int = 0
    skipped_extensions: int = 0
    skipped_editable: int = 0
    skipped_media: int = 0
    skipped_existing: int = 0


def resolve_directory(positional: str | None, flag: str | None) -> Path | None:
    if positional is not None and flag is not None and positional != flag:
        logger.error("Specify directory either positionally or with --dir, not both differently")
        return None
    raw = flag if flag is not None else positional
    if raw is None:
        raw = "."
    root = Path(raw).resolve()
    if not root.exists():
        logger.error(f"Directory '{root}' does not exist")
        return None
    if not root.is_dir():
        logger.error(f"'{root}' is not a directory")
        return None
    return root


def is_excluded_name(
    name: str,
    excluded_names: set[str] | frozenset[str],
    excluded_patterns: Sequence[str],
) -> bool:
    if name in excluded_names:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in excluded_patterns)


def is_editable_package_dir(path: Path) -> bool:
    try:
        for child in path.iterdir():
            if child.is_dir() and child.name.endswith(".egg-info"):
                if (child / "SOURCES.txt").exists():
                    return True
                direct_url = child / "direct_url.json"
                if direct_url.exists():
                    try:
                        data = json.loads(direct_url.read_text())
                        dir_info = data.get("dir_info", {})
                        if isinstance(dir_info, dict) and dir_info.get("editable", False):
                            return True
                    except (OSError, json.JSONDecodeError):
                        pass
        return False
    except (PermissionError, OSError):
        return False


def collect_zstd_tasks(
    root: Path,
    compress: bool,
    args: argparse.Namespace,
) -> tuple[list[tuple[Path, Path]], ScanStats]:
    stats = ScanStats()
    tasks: list[tuple[Path, Path]] = []
    skip_exts = set(ZSTD_SKIP_EXTENSIONS)
    if args.no_skip_so:
        skip_exts.discard(".so")
    if args.legacy_extra_skips:
        skip_exts.update({".dat", ".npz", ".onnx"})
    excluded_names = set(args.exclude_dir_names) if args.exclude_dir_names is not None else set(ZSTD_EXCLUDED_DIR_NAMES)
    excluded_patterns = (
        list(args.exclude_dir_patterns) if args.exclude_dir_patterns is not None else list(ZSTD_EXCLUDED_DIR_PATTERNS)
    )
    if args.simple:
        pattern = args.pattern
        if not compress:
            pattern = f"*{pattern}*.zst"
        for path in root.rglob(pattern):
            if not path.is_file():
                continue
            if compress:
                if path.suffix == ".zst":
                    stats.skipped_extensions += 1
                    continue
                out = path.with_suffix(path.suffix + ".zst")
            else:
                if path.suffix != ".zst":
                    stats.skipped_extensions += 1
                    continue
                if args.simple_legacy_zst_skip:
                    stats.skipped_extensions += 1
                    continue
                out = path.with_suffix("")
            if out.exists():
                stats.skipped_existing += 1
                continue
            stats.files += 1
            tasks.append((path, out))
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            if ".git" in current.parts:
                continue
            kept_dirs: list[str] = []
            for dirname in dirnames:
                if is_excluded_name(dirname, excluded_names, excluded_patterns):
                    stats.skipped_dirs += 1
                else:
                    kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            if is_editable_package_dir(current):
                dirnames[:] = []
                stats.skipped_editable += 1
                continue
            stats.dirs += 1
            for filename in filenames:
                path = current / filename
                if path.is_symlink():
                    stats.skipped_symlinks += 1
                    continue
                path_str = str(path)
                if ".egg-info" in path_str or ".dist-info" in path_str:
                    stats.skipped_extensions += 1
                    continue
                if compress:
                    suffix = path.suffix.lower()
                    if suffix in skip_exts:
                        stats.skipped_extensions += 1
                        if suffix in ZSTD_MEDIA_EXTENSIONS:
                            stats.skipped_media += 1
                        continue
                else:
                    if args.tar_zst_skip_decompress and path.name.endswith(".tar.zst"):
                        stats.skipped_extensions += 1
                        continue
                    if path.suffix not in ZSTD_EXTENSIONS:
                        stats.skipped_extensions += 1
                        continue
                out = path.with_suffix(path.suffix + ".zst") if compress else path.with_suffix("")
                if out.exists():
                    stats.skipped_existing += 1
                    continue
                stats.files += 1
                tasks.append((path, out))
    if args.scan_order == "largest":

        def size_key(item: tuple[Path, Path]) -> int:
            try:
                return item[0].stat().st_size
            except OSError:
                return 0

        tasks.sort(key=size_key, reverse=True)
    return tasks, stats


def zstd_compress_file(
    path: Path,
    out: Path,
    level: int,
    threads: int,
    chunk_size: int,
    remove_original: bool,
    use_writer: bool,
) -> tuple[bool, Path, Path, int, int, str | None]:
    try:
        original_size = path.stat().st_size
        compressor = zstd.ZstdCompressor(level=level, threads=threads)
        if use_writer:
            with path.open("rb") as fin, out.open("wb") as fout, compressor.stream_writer(fout) as writer:
                while True:
                    chunk = fin.read(chunk_size)
                    if not chunk:
                        break
                    writer.write(chunk)
        else:
            with path.open("rb") as fin, out.open("wb") as fout:
                reader = compressor.stream_reader(fin)
                while True:
                    chunk = reader.read(chunk_size)
                    if not chunk:
                        break
                    fout.write(chunk)
        compressed_size = out.stat().st_size
        if remove_original:
            path.unlink()
        return (True, path, out, original_size, compressed_size, None)
    except Exception as exc:
        with contextlib.suppress(OSError):
            if out.exists():
                out.unlink()
        return (False, path, out, 0, 0, str(exc))


def zstd_decompress_file(
    path: Path,
    out: Path,
    chunk_size: int,
    remove_original: bool,
) -> tuple[bool, Path, Path, int, int, str | None]:
    try:
        compressed_size = path.stat().st_size
        decompressor = zstd.ZstdDecompressor()
        with path.open("rb") as fin, out.open("wb") as fout:
            reader = decompressor.stream_reader(fin)
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                fout.write(chunk)
        original_size = out.stat().st_size
        if remove_original:
            path.unlink()
        return (True, path, out, original_size, compressed_size, None)
    except Exception as exc:
        with contextlib.suppress(OSError):
            if out.exists():
                out.unlink()
        return (False, path, out, 0, 0, str(exc))


def _handle_zstd_result(
    result: tuple[bool, Path, Path, int, int, str | None],
    stats: Stats,
    errors: list[tuple[Path, str]],
    compress: bool,
    args: argparse.Namespace,
    index: int,
    total: int,
) -> None:
    ok, path, out, original_size, compressed_size, error = result
    if ok:
        stats.add(original_size, compressed_size)
        if args.progress == "verbose":
            if compress:
                ratio = compressed_size / original_size * args.stats_scale if original_size else 0.0
                action = "Compressed & removed" if not args.keep else "Compressed"
                print(f"✓ {action}: {path} -> {out}")
                print(f"  Size: {original_size:,} -> {compressed_size:,} bytes ({ratio:.1f}%)")
            else:
                action = "Decompressed & removed" if not args.keep else "Decompressed"
                print(f"✓ {action}: {path} -> {out}")
        elif args.progress == "bar":
            bar_len = 50
            filled = int(index / total * bar_len) if total else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"\rProgress: [{bar}] {index}/{total} files", end="", flush=True)
    else:
        errors.append((path, error or "unknown error"))


def run_zstd(args: argparse.Namespace) -> int:
    compress = args.compress or not args.decompress
    root = resolve_directory(args.directory, args.dir_flag)
    if root is None:
        return 1
    print(f"Working directory: {root}")
    print(f"Mode: {'Compression' if compress else 'Decompression'}")
    if not args.simple:
        print(f"Pool workers: {args.pool_workers}")
    print(f"Threads per job: {args.threads}")
    if compress:
        print(f"Compression level: {args.level}")
    print(f"Keep original files: {'Yes' if args.keep else 'No'}")
    print("Scanning directory tree...")
    tasks, scan = collect_zstd_tasks(root, compress, args)
    if scan.skipped_symlinks:
        logger.warning(f"Skipped {scan.skipped_symlinks} symlinks")
    if scan.skipped_media:
        print(f"Skipped {scan.skipped_media} media/binary files (already compressed)")
    if scan.skipped_extensions:
        print(f"Skipped {scan.skipped_extensions} files with unwanted extensions")
    if scan.skipped_editable:
        print(f"Skipped {scan.skipped_editable} editable package directories")
    if scan.skipped_dirs:
        print(f"Skipped {scan.skipped_dirs} excluded directories")
    if scan.skipped_existing:
        logger.warning(f"Skipped {scan.skipped_existing} files (output already exists)")
    total = len(tasks)
    if total == 0:
        print("No files to process.")
        return 0
    print(f"Found {total} files to process.")
    if args.dry_run:
        for path, out in tasks:
            action = "compress" if compress else "decompress"
            keep = "keep" if args.keep else "remove"
            print(f"[DRY RUN] Would {action} & {keep}: {path} -> {out}")
        return 0
    stats = Stats()
    errors: list[tuple[Path, str]] = []
    remove_original = not args.keep
    if args.sequential or args.pool_workers <= 1:
        for index, (path, out) in enumerate(tasks, 1):
            if compress:
                result = zstd_compress_file(
                    path,
                    out,
                    args.level,
                    args.threads,
                    args.chunk_size,
                    remove_original,
                    args.zstd_writer,
                )
            else:
                result = zstd_decompress_file(
                    path,
                    out,
                    args.chunk_size,
                    remove_original,
                )
            _handle_zstd_result(result, stats, errors, compress, args, index, total)
    else:
        with Pool(processes=args.pool_workers) as pool:
            async_results = []
            for path, out in tasks:
                if compress:
                    ar = pool.apply_async(
                        zstd_compress_file,
                        (
                            path,
                            out,
                            args.level,
                            args.threads,
                            args.chunk_size,
                            remove_original,
                            args.zstd_writer,
                        ),
                    )
                else:
                    ar = pool.apply_async(
                        zstd_decompress_file,
                        (path, out, args.chunk_size, remove_original),
                    )
                async_results.append(ar)
            for index, ar in enumerate(async_results, 1):
                result = ar.get()
                _handle_zstd_result(result, stats, errors, compress, args, index, total)
    print()
    print("-" * 40)
    if compress and stats.original_size > 0:
        saved, compressed_pct, saved_pct = stats.savings(args.stats_scale)
        print("📊 Compression Statistics:")
        print(f"   Original size:   {fsz(stats.original_size)}")
        print(f"   Compressed size: {fsz(stats.compressed_size)}")
        print(f"   Space saved:     {fsz(saved)} ({saved_pct:.1f}%)")
        print(f"   Compression ratio: {compressed_pct:.1f}%")
    if errors:
        logger.error(f"❌ Failed to process {len(errors)} files:")
        for path, error in errors[:200]:
            logger.error(f"  - {path}: {error}")
        if len(errors) > 200:
            logger.error(f"  ... and {len(errors) - 200} more")
        return 1
    if total > 0:
        action = "compressed" if compress else "decompressed"
        logger.success(f"✅ Successfully {action} {total} files!")
        if remove_original:
            print("   Original files have been removed.")
    else:
        logger.warning("No files were processed.")
    return 0


def collect_xz_tasks(
    root: Path,
    compress: bool,
    skip_extensions: set[str] | frozenset[str],
    excluded_dirs: set[str] | frozenset[str],
) -> list[Path]:
    tasks: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in excluded_dirs for part in path.parts):
            continue
        suffix = path.suffix.lower()
        if compress:
            if suffix in skip_extensions:
                continue
        elif suffix != ".xz":
            continue
        tasks.append(path)
    return sorted(tasks)


def xz_compress_file(
    path: Path,
    preset: int,
    threads: int,
    remove_original: bool,
) -> tuple[bool, Path, Path, int, int, str | None]:
    try:
        data = path.read_bytes()
        compressed = lzma_mt.compress(data, preset=preset, threads=threads)
        out = path.parent / (path.name + ".xz")
        out.write_bytes(compressed)
        if remove_original:
            path.unlink()
        return (True, path, out, len(data), len(compressed), None)
    except Exception as exc:
        return (False, path, path.parent / (path.name + ".xz"), 0, 0, str(exc))


def xz_decompress_file(
    path: Path,
    remove_original: bool,
) -> tuple[bool, Path, Path, int, int, str | None]:
    try:
        compressed = path.read_bytes()
        data = lzma_mt.decompress(compressed)
        out = path.parent / path.stem
        out.write_bytes(data)
        if remove_original:
            path.unlink()
        return (True, path, out, len(data), len(compressed), None)
    except Exception as exc:
        return (False, path, path.parent / path.stem, 0, 0, str(exc))


def run_xz(args: argparse.Namespace) -> int:
    if lzma_mt is None:
        logger.error("lzma_mt is not installed; xz subcommand is unavailable")
        return 1
    compress = args.compress or not args.decompress
    root = resolve_directory(args.directory, args.dir_flag)
    if root is None:
        return 1
    skip_extensions = set(args.skip_extensions) if args.skip_extensions is not None else set(XZ_SKIP_EXTENSIONS)
    excluded_dirs = set(args.exclude_dirs) if args.exclude_dirs is not None else set(XZ_EXCLUDED_DIR_NAMES)
    tasks = collect_xz_tasks(root, compress, skip_extensions, excluded_dirs)
    print(f"Working directory: {root}")
    print(f"Mode: {'Compression' if compress else 'Decompression'}")
    print(f"Pool workers: {args.pool_workers}")
    if compress:
        print(f"Preset: {args.preset}, Threads: {args.threads}")
    print(f"Keep original files: {'Yes' if args.keep else 'No'}")
    if not tasks:
        print("No files found to process")
        return 0
    if args.dry_run:
        for path in tasks:
            action = "compress" if compress else "decompress"
            keep = "keep" if args.keep else "remove"
            print(f"[DRY RUN] Would {action} & {keep}: {path}")
        return 0
    remove_original = not args.keep
    success = 0
    errors: list[tuple[Path, str]] = []
    total = len(tasks)
    if args.pool_workers <= 1:
        for index, path in enumerate(tasks, 1):
            if compress:
                result = xz_compress_file(path, args.preset, args.threads, remove_original)
            else:
                result = xz_decompress_file(path, remove_original)
            ok, in_path, out_path, _orig, _comp, error = result
            print(f"[{index / total * 100:5.1f}%] {index}/{total}")
            if ok:
                success += 1
                verb = "Compressed to" if compress else "Decompressed to"
                print(f"✓ {in_path.relative_to(root)}: {verb} {out_path.name}")
            else:
                errors.append((in_path, error or "unknown error"))
                print(f"✗ {in_path.relative_to(root)}: {error}")
    else:
        with Pool(processes=args.pool_workers) as pool:
            async_results = []
            for path in tasks:
                if compress:
                    ar = pool.apply_async(
                        xz_compress_file,
                        (path, args.preset, args.threads, remove_original),
                    )
                else:
                    ar = pool.apply_async(xz_decompress_file, (path, remove_original))
                async_results.append(ar)
            for index, ar in enumerate(async_results, 1):
                result = ar.get()
                ok, in_path, out_path, _orig, _comp, error = result
                print(f"[{index / total * 100:5.1f}%] {index}/{total}")
                if ok:
                    success += 1
                    verb = "Compressed to" if compress else "Decompressed to"
                    print(f"✓ {in_path.relative_to(root)}: {verb} {out_path.name}")
                else:
                    errors.append((in_path, error or "unknown error"))
                    print(f"✗ {in_path.relative_to(root)}: {error}")
    print("─" * 40)
    print(f"Total successful: {success}")
    print(f"Total failed: {len(errors)}")
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively compress/decompress files with zstd or xz.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python unified_compress.py zstd -c --dir . --level 3\n"
            "  python unified_compress.py zstd -d --dir .\n"
            "  python unified_compress.py xz -c --preset 9 --threads 4\n"
            "  python unified_compress.py xz -d --dir . --keep-orig"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    zstd_parser = subparsers.add_parser(
        "zstd",
        help="Compress/decompress using zstandard",
        description="Recursively compress/decompress using zstandard.",
    )
    zstd_parser.add_argument("directory", nargs="?", default=None, help="Directory to process")
    zstd_parser.add_argument("--dir", dest="dir_flag", default=None, help="Directory to process")
    zstd_group = zstd_parser.add_mutually_exclusive_group()
    zstd_group.add_argument("-c", "--compress", action="store_true", help="Compress files (default)")
    zstd_group.add_argument("-d", "--decompress", action="store_true", help="Decompress .zst files")
    zstd_parser.add_argument(
        "--level",
        type=int,
        default=DEFAULT_ZSTD_LEVEL,
        choices=range(1, 23),
        help="Compression level 1-22 (default: 3)",
    )
    zstd_parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_ZSTD_THREADS,
        help="Threads per zstd call (default: 4)",
    )
    zstd_parser.add_argument(
        "--pool-workers",
        type=int,
        default=DEFAULT_ZSTD_MAX_WORKERS,
        help="Multiprocessing pool size (default: 8)",
    )
    zstd_parser.add_argument(
        "--sequential",
        action="store_true",
        help="Disable multiprocessing and run in-process",
    )
    zstd_parser.add_argument(
        "--keep",
        "--keep-original",
        action="store_true",
        dest="keep",
        help="Keep original files",
    )
    zstd_parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_ZSTD_CHUNK_SIZE,
        help="Streaming chunk size in bytes",
    )
    zstd_parser.add_argument(
        "--scan-order",
        choices=("unsorted", "largest"),
        default="unsorted",
        help="File scan order",
    )
    zstd_parser.add_argument(
        "--simple",
        action="store_true",
        help="Use simple rglob(pattern) mode and disable exclusions",
    )
    zstd_parser.add_argument("--pattern", default="*", help="Glob pattern for --simple mode")
    zstd_parser.add_argument("--dry-run", action="store_true", help="Show actions without executing")
    zstd_parser.add_argument(
        "--progress",
        choices=("simple", "bar", "verbose", "none"),
        default="simple",
        help="Progress output style",
    )
    zstd_parser.add_argument(
        "--tar-zst-skip-decompress",
        action="store_true",
        help="Skip .tar.zst during decompression",
    )
    zstd_parser.add_argument(
        "--stats-scale",
        type=float,
        default=100.0,
        help="Scale used for reported percentages (100 or legacy 40)",
    )
    zstd_parser.add_argument(
        "--no-skip-so",
        action="store_true",
        help="Do not skip .so files during compression",
    )
    zstd_parser.add_argument(
        "--legacy-extra-skips",
        action="store_true",
        help="Also skip .dat, .npz, .onnx during compression",
    )
    zstd_parser.add_argument(
        "--zstd-writer",
        action="store_true",
        help="Use ZstdCompressor.stream_writer instead of stream_reader",
    )
    zstd_parser.add_argument(
        "--simple-legacy-zst-skip",
        action="store_true",
        help="Reproduce fast_compress4 decompression .zst skip bug",
    )
    zstd_parser.add_argument(
        "--exclude-dir-names",
        nargs="*",
        default=None,
        help="Override excluded directory names",
    )
    zstd_parser.add_argument(
        "--exclude-dir-patterns",
        nargs="*",
        default=None,
        help="Override excluded directory glob patterns",
    )
    xz_parser = subparsers.add_parser(
        "xz",
        help="Compress/decompress using lzma_mt",
        description="Recursively compress/decompress using lzma_mt.",
    )
    xz_parser.add_argument("directory", nargs="?", default=None, help="Directory to process")
    xz_parser.add_argument("--dir", dest="dir_flag", default=None, help="Directory to process")
    xz_group = xz_parser.add_mutually_exclusive_group()
    xz_group.add_argument("-c", "--compress", action="store_true", help="Compress files (default)")
    xz_group.add_argument("-d", "--decompress", action="store_true", help="Decompress .xz files")
    xz_parser.add_argument(
        "--preset",
        type=int,
        default=DEFAULT_XZ_PRESET,
        choices=range(10),
        help="Compression preset 0-9 (default: 9)",
    )
    xz_parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_XZ_THREADS,
        help="Threads per compression job (default: 4)",
    )
    xz_parser.add_argument(
        "--pool-workers",
        type=int,
        default=DEFAULT_XZ_MAX_WORKERS,
        help="Multiprocessing pool size (default: 8)",
    )
    xz_parser.add_argument(
        "--keep",
        "--keep-orig",
        action="store_true",
        dest="keep",
        help="Keep original files",
    )
    xz_parser.add_argument(
        "--exclude-dirs",
        nargs="*",
        default=None,
        help="Override excluded directory names",
    )
    xz_parser.add_argument(
        "--skip-extensions",
        nargs="*",
        default=None,
        help="Override skipped extensions during compression",
    )
    xz_parser.add_argument("--dry-run", action="store_true", help="Show actions without executing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        parser.print_help()
        return 0
    args = parser.parse_args(args_list)
    if args.command == "zstd":
        return run_zstd(args)
    if args.command == "xz":
        return run_xz(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
