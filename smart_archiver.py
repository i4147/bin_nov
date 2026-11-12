#!/data/data/com.termux/files/usr/bin/env python
"""Archive files by automatically choosing the best compression algorithm."""

import argparse
import bz2
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import gzip
from io import BytesIO
import json
import lzma
from pathlib import Path
import sys
import tarfile
import tempfile
import time
from typing import Any

import brotli
from dh import MAX_WORKERS
from loguru import logger
import lz4.frame
import zstandard as zstd

EXTENSION_MAP: dict[str, dict[str, str | int]] = {
    ".txt": {"algo": "brotli", "level": 11},
    ".log": {"algo": "brotli", "level": 11},
    ".csv": {"algo": "brotli", "level": 11},
    ".json": {"algo": "brotli", "level": 11},
    ".xml": {"algo": "brotli", "level": 11},
    ".html": {"algo": "brotli", "level": 11},
    ".css": {"algo": "brotli", "level": 11},
    ".js": {"algo": "brotli", "level": 11},
    ".md": {"algo": "brotli", "level": 11},
    ".yaml": {"algo": "brotli", "level": 11},
    ".yml": {"algo": "brotli", "level": 11},
    ".sql": {"algo": "brotli", "level": 11},
    ".py": {"algo": "zstd", "level": 19},
    ".c": {"algo": "zstd", "level": 19},
    ".cpp": {"algo": "zstd", "level": 19},
    ".h": {"algo": "zstd", "level": 19},
    ".java": {"algo": "zstd", "level": 19},
    ".go": {"algo": "zstd", "level": 19},
    ".rs": {"algo": "zstd", "level": 19},
    ".rb": {"algo": "zstd", "level": 19},
    ".php": {"algo": "zstd", "level": 19},
    ".swift": {"algo": "zstd", "level": 19},
    ".kt": {"algo": "zstd", "level": 19},
    ".zip": {"algo": "lz4", "level": 1},
    ".gz": {"algo": "lz4", "level": 1},
    ".bz2": {"algo": "lz4", "level": 1},
    ".xz": {"algo": "lz4", "level": 1},
    ".7z": {"algo": "lz4", "level": 1},
    ".rar": {"algo": "lz4", "level": 1},
    ".zst": {"algo": "lz4", "level": 1},
    ".br": {"algo": "lz4", "level": 1},
    ".jpg": {"algo": "lz4", "level": 1},
    ".jpeg": {"algo": "lz4", "level": 1},
    ".png": {"algo": "lz4", "level": 1},
    ".gif": {"algo": "lz4", "level": 1},
    ".bmp": {"algo": "zstd", "level": 19},
    ".webp": {"algo": "lz4", "level": 1},
    ".mp4": {"algo": "lz4", "level": 1},
    ".avi": {"algo": "lz4", "level": 1},
    ".mkv": {"algo": "lz4", "level": 1},
    ".mov": {"algo": "lz4", "level": 1},
    ".wmv": {"algo": "lz4", "level": 1},
    ".flv": {"algo": "lz4", "level": 1},
    ".mp3": {"algo": "lz4", "level": 1},
    ".flac": {"algo": "lz4", "level": 1},
    ".wav": {"algo": "zstd", "level": 19},
    ".aac": {"algo": "lz4", "level": 1},
    ".ogg": {"algo": "lz4", "level": 1},
    ".exe": {"algo": "zstd", "level": 19},
    ".dll": {"algo": "zstd", "level": 19},
    ".so": {"algo": "zstd", "level": 19},
    ".bin": {"algo": "zstd", "level": 19},
    ".pdf": {"algo": "zstd", "level": 19},
    ".docx": {"algo": "lz4", "level": 1},
    ".xlsx": {"algo": "lz4", "level": 1},
    ".pptx": {"algo": "lz4", "level": 1},
    ".epub": {"algo": "lz4", "level": 1},
    ".tar": {"algo": "zstd", "level": 19},
}
DEFAULT_SETTINGS: dict[str, dict[str, str | int]] = {
    "small_text": {"algo": "brotli", "level": 11},
    "large_text": {"algo": "zstd", "level": 19},
    "small_binary": {"algo": "zstd", "level": 19},
    "large_binary": {"algo": "zstd", "level": 21},
    "already_compressed": {"algo": "lz4", "level": 1},
}


def compress_zstd(data: bytes, level: int) -> bytes:
    compressor = zstd.ZstdCompressor(level=level)
    return compressor.compress(data)


def compress_brotli_standard(data: bytes, level: int) -> bytes:
    return brotli.compress(data, quality=level)


def compress_brotli_streaming(data: bytes, level: int, chunk_size: int = 512 * 1024) -> bytes:
    compressor = brotli.Compressor(quality=level)
    result_parts: list[bytes] = []
    for i in range(0, len(data), chunk_size):
        chunk = data[i : i + chunk_size]
        result_parts.append(compressor.process(chunk))
    result_parts.append(compressor.finish())
    return b"".join(result_parts)


def compress_lzma(data: bytes, level: int) -> bytes:
    return lzma.compress(data, preset=level)


def compress_gzip(data: bytes, level: int) -> bytes:
    out = BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=level) as gz:
        gz.write(data)
    return out.getvalue()


def compress_bz2(data: bytes, level: int) -> bytes:
    return bz2.compress(data, compresslevel=level)


def compress_lz4(data: bytes, level: int) -> bytes:
    return lz4.frame.compress(data, compression_level=level)


def compress_data(data: bytes, algo: str, level: int, is_large: bool = False) -> bytes:
    if algo == "zstd":
        return compress_zstd(data, level)
    elif algo == "brotli":
        if is_large:
            return compress_brotli_streaming(data, level)
        else:
            return compress_brotli_standard(data, level)
    elif algo == "lzma":
        return compress_lzma(data, level)
    elif algo == "gzip":
        return compress_gzip(data, level)
    elif algo == "bz2":
        return compress_bz2(data, level)
    elif algo == "lz4":
        return compress_lz4(data, level)
    else:
        msg = f"Unknown algorithm: {algo}"
        raise ValueError(msg)


def is_already_compressed(data: bytes, sample_size: int = 4096) -> bool:
    if len(data) < 4:
        return False
    magic_bytes: list[tuple[bytes, str]] = [
        (b"\x1f\x8b", "gzip"),
        (b"BZh", "bzip2"),
        (b"\xfd7zXZ", "xz"),
        (b"PK\x03\x04", "zip"),
        (b"(\xb5/\xfd", "zstd"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpeg"),
        (b"Rar!", "rar"),
        (b"7z\xbc\xaf'\x1c", "7z"),
    ]
    return any(data.startswith(magic) for magic, _ in magic_bytes)


def choose_algorithm(
    path: str | Path,
    data: bytes | None = None,
    file_size: int | None = None,
) -> dict[str, str | int]:
    ext = Path(path).suffix.lower()
    if ext in EXTENSION_MAP:
        return EXTENSION_MAP[ext]
    if file_size is None and data is not None:
        file_size = len(data)
    elif file_size is None:
        file_size = Path(path).stat().st_size
    if data is not None and is_already_compressed(data):
        return DEFAULT_SETTINGS["already_compressed"]
    is_text = False
    if data is not None:
        sample = data[: min(8192, len(data))]
        if b"\x00" not in sample:
            printable = sum(32 <= b <= 126 or b in (9, 10, 13) for b in sample)
            is_text = printable / len(sample) > 0.8
    if is_text:
        if file_size < 10 * 1024 * 1024:
            return DEFAULT_SETTINGS["small_text"]
        else:
            return DEFAULT_SETTINGS["large_text"]
    elif file_size < 10 * 1024 * 1024:
        return DEFAULT_SETTINGS["small_binary"]
    elif file_size > 100 * 1024 * 1024:
        return DEFAULT_SETTINGS["large_binary"]
    else:
        return DEFAULT_SETTINGS["small_binary"]


def compress_single_file(
    path: str | Path,
    output_path: str | Path | None = None,
    remove_original: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    start_time = time.time()
    try:
        with open(path, "rb") as f:
            data = f.read()
        settings = choose_algorithm(path, data)
        algo = str(settings["algo"])
        level = int(settings["level"])
        is_large = len(data) > 50 * 1024 * 1024
        compressed_data = compress_data(data, algo, level, is_large)
        if output_path is None:
            output_path = str(path) + f".{algo}"
        elif str(output_path).endswith(("/", "\\")):
            output_path = Path(output_path) / (Path(path).name + f".{algo}")
        else:
            output_path = Path(output_path)
        with open(output_path, "wb") as f:
            f.write(compressed_data)
        if remove_original:
            Path(path).unlink()
        elapsed = time.time() - start_time
        original_size = len(data)
        compressed_size = len(compressed_data)
        ratio = compressed_size / original_size * 100
        if verbose:
            print(
                f"✓ {Path(path).name}: {algo.upper()}:{level} "
                f"{original_size:,d} → {compressed_size:,d} bytes ({ratio:.1f}%) "
                f"in {elapsed:.2f}s"
            )
        return {
            "file": str(path),
            "output": str(output_path),
            "algorithm": algo,
            "level": level,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "ratio": ratio,
            "time": elapsed,
            "success": True,
        }
    except Exception as e:
        if verbose:
            logger.error(f"✗ Failed to compress {path}: {e}")
        return {"file": str(path), "success": False, "error": str(e)}


def compress_multiple_files(
    paths: list[str | Path],
    output_dir: str | Path | None = None,
    max_workers: int | None = None,
    remove_original: bool = False,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    if max_workers is None:
        max_workers = 8
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[Any, str | Path] = {}
        for path in paths:
            output_path = Path(output_dir) / (Path(path).name + ".compressed") if output_dir else None
            future = executor.submit(compress_single_file, path, output_path, remove_original, verbose)
            futures[future] = path
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if verbose and result["success"]:
                file_name = Path(result["file"]).name
                logger.debug(f"  Completed: {file_name} ({result['algorithm']})")
    return results


def create_tar_archive(
    source_dir: str | Path,
    output_path: str | Path | None = None,
    compression: str = "auto",
    level: int | None = None,
    parallel: bool = False,
    max_workers: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    start_time = time.time()
    source_dir = Path(source_dir)
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"{source_dir.name}_{timestamp}.tar"
    tar_path = Path(str(output_path).replace(".gz", "").replace(".xz", "").replace(".zst", "").replace(".br", ""))
    if compression not in {"none", "auto"}:
        tar_path = tar_path.with_suffix("")
    print(f"Creating archive from {source_dir}...")
    file_count = 0
    total_size = 0
    with tarfile.open(tar_path, "w") as tar:
        for item in source_dir.rglob("*"):
            if item.is_file() and not item.is_symlink():
                tar.add(item, arcname=item.relative_to(source_dir))
                file_count += 1
                total_size += item.stat().st_size
                if file_count % 1000 == 0:
                    logger.debug(f"  Added {file_count} files...")
    print(f"\nArchived {file_count} files ({total_size / 1024 / 1024:.2f} MB)")
    if compression != "none":
        with open(tar_path, "rb") as f:
            tar_data = f.read()
        if compression == "auto":
            settings = choose_algorithm(tar_path, tar_data, total_size)
            algo = str(settings["algo"])
            level = int(settings["level"])
        elif compression in ["zstd", "brotli", "lz4", "lzma", "gzip", "bz2"]:
            algo = compression
            level = level or (11 if algo == "brotli" else 19 if algo == "zstd" else 9)
        else:
            msg = f"Unsupported compression: {compression}"
            raise ValueError(msg)
        print(f"Compressing with {algo.upper()} (level {level})...")
        is_large = len(tar_data) > 50 * 1024 * 1024
        compressed_data = compress_data(tar_data, algo, level, is_large)
        compressed_path = Path(f"{tar_path}.{algo}")
        with open(compressed_path, "wb") as f:
            f.write(compressed_data)
        Path(tar_path).unlink()
        final_path = compressed_path
        elapsed = time.time() - start_time
        compressed_size = len(compressed_data)
        ratio = compressed_size / total_size * 100
        print(f"\n✓ Archive created: {final_path}")
        print(f"  Size: {compressed_size / 1024 / 1024:.2f} MB ({ratio:.1f}% of original)")
        print(f"  Time: {elapsed:.2f}s")
        print(f"  Algorithm: {algo.upper()} level {level}")
        return final_path, {
            "file_count": file_count,
            "original_size": total_size,
            "compressed_size": compressed_size,
            "ratio": ratio,
            "time": elapsed,
            "algorithm": algo,
            "level": level,
        }
    else:
        elapsed = time.time() - start_time
        print(f"\n✓ Archive created: {tar_path}")
        print(f"  Size: {total_size / 1024 / 1024:.2f} MB")
        print(f"  Time: {elapsed:.2f}s")
        return tar_path, {
            "file_count": file_count,
            "original_size": total_size,
            "time": elapsed,
        }


def decompress_file(
    compressed_path: str | Path,
    output_dir: str | Path | None = None,
    verbose: bool = False,
) -> Path | str:
    compressed_path = Path(compressed_path)
    ext = compressed_path.suffix.lower()
    algo_map = {
        ".zstd": "zstd",
        ".zst": "zstd",
        ".br": "brotli",
        ".lz4": "lz4",
        ".xz": "lzma",
        ".gz": "gzip",
        ".bz2": "bz2",
    }
    algo = algo_map.get(ext)
    if not algo:
        msg = f"Unknown compression format: {ext}"
        raise ValueError(msg)
    with open(compressed_path, "rb") as f:
        compressed_data = f.read()
    if algo == "zstd":
        decompressor = zstd.ZstdDecompressor()
        data = decompressor.decompress(compressed_data)
    elif algo == "brotli":
        data = brotli.decompress(compressed_data)
    elif algo == "lz4":
        data = lz4.frame.decompress(compressed_data)
    elif algo == "lzma":
        data = lzma.decompress(compressed_data)
    elif algo == "gzip":
        with gzip.GzipFile(fileobj=BytesIO(compressed_data)) as gz:
            data = gz.read()
    elif algo == "bz2":
        data = bz2.decompress(compressed_data)
    else:
        msg = f"Decompression not implemented for {algo}"
        raise ValueError(msg)
    if output_dir:
        output_path = Path(output_dir) / compressed_path.stem
    else:
        output_path = compressed_path.with_suffix("")
        if output_path.suffix == ".tar":
            output_path = output_path.with_suffix("")
    with open(output_path, "wb") as f:
        f.write(data)
    if output_path.suffix == ".tar":
        extract_dir = output_dir or output_path.parent / output_path.stem
        extract_dir.mkdir(exist_ok=True)
        with tarfile.open(output_path, "r") as tar:
            tar.extractall(extract_dir)
        Path(output_path).unlink()
        if verbose:
            print(f"✓ Extracted to: {extract_dir}")
        return extract_dir
    if verbose:
        print(f"✓ Decompressed to: {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart Archiver - Automatically chooses best compression algorithm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python smart_archiver.py compress document.txt
  python smart_archiver.py compress *.log --parallel --output-dir compressed/
  python smart_archiver.py archive myfolder/ --compression auto
  python smart_archiver.py archive myfolder/ --compression zstd --level 22
  python smart_archiver.py decompress document.txt.zstd
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    compress_parser = subparsers.add_parser("compress", help="Compress files")
    compress_parser.add_argument("files", nargs="+", help="Files to compress")
    compress_parser.add_argument("-o", "--output-dir", help="Output directory")
    compress_parser.add_argument("-p", "--parallel", action="store_true", help="Enable parallel compression")
    compress_parser.add_argument("--remove", action="store_true", help="Remove original files after compression")
    compress_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    archive_parser = subparsers.add_parser("archive", help="Create compressed archive")
    archive_parser.add_argument("directory", help="Directory to archive")
    archive_parser.add_argument("-o", "--output", help="Output file path")
    archive_parser.add_argument(
        "-c",
        "--compression",
        default="auto",
        choices=["auto", "zstd", "brotli", "lz4", "lzma", "gzip", "bz2", "none"],
        help="Compression algorithm (default: auto)",
    )
    archive_parser.add_argument("-l", "--level", type=int, help="Compression level (algorithm-specific)")
    archive_parser.add_argument(
        "--parallel",
        action="store_true",
        help="Use parallel processing for file addition",
    )
    archive_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    decompress_parser = subparsers.add_parser("decompress", help="Decompress files")
    decompress_parser.add_argument("files", nargs="+", help="Files to decompress")
    decompress_parser.add_argument("-o", "--output-dir", help="Output directory")
    decompress_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    benchmark_parser = subparsers.add_parser("benchmark", help="Benchmark different algorithms")
    benchmark_parser.add_argument("input", help="Input file or directory")
    benchmark_parser.add_argument("-o", "--output", help="Output JSON file for results")
    benchmark_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    if args.command == "compress":
        files: list[Path] = []
        for pattern in args.files:
            files.extend(Path().glob(pattern))
        if not files:
            logger.error(f"No files found matching {args.files}")
            sys.exit(1)
        if args.parallel and len(files) > 1:
            results = compress_multiple_files(
                files,
                output_dir=args.output_dir,
                max_workers=MAX_WORKERS,
                remove_original=args.remove,
                verbose=args.verbose,
            )
            successful = sum(1 for r in results if r["success"])
            failed = len(results) - successful
            total_original = sum(r.get("original_size", 0) for r in results if r["success"])
            total_compressed = sum(r.get("compressed_size", 0) for r in results if r["success"])
            print(f"\n{'=' * 40}")
            print("COMPRESSION SUMMARY")
            print(f"  Successful: {successful}/{len(results)} files")
            if failed:
                logger.warning(f"  Failed: {failed} files")
            if successful:
                print(f"  Total size: {total_original / 1024 / 1024:.2f} MB → {total_compressed / 1024 / 1024:.2f} MB")
                print(f"  Overall ratio: {total_compressed / total_original * 100:.1f}%")
        else:
            for path in files:
                compress_single_file(path, args.output_dir, args.remove, args.verbose)
    elif args.command == "archive":
        create_tar_archive(
            args.directory,
            output_path=args.output,
            compression=args.compression,
            level=args.level,
            parallel=args.parallel,
            max_workers=MAX_WORKERS,
        )
    elif args.command == "decompress":
        for path in args.files:
            decompress_file(path, args.output_dir, args.verbose)
    elif args.command == "benchmark":
        from hybrid_compression_benchmark import benchmark_hybrid

        input_path = Path(args.input)
        if input_path.is_dir():
            with tempfile.TemporaryDirectory() as tmpdir:
                tar_path = Path(tmpdir) / "data.tar"
                with tarfile.open(tar_path, "w") as tar:
                    for item in input_path.rglob("*"):
                        if item.is_file():
                            tar.add(item, arcname=item.relative_to(input_path))
                data = tar_path.read_bytes()
                results = benchmark_hybrid(data, len(data))
                if args.output:
                    serializable = {}
                    for name, info in results.items():
                        serializable[name] = {k: v for k, v in info.items() if isinstance(v, (int, float, str))}
                    with open(args.output, "w") as f:
                        json.dump(serializable, f, indent=2)
                    print(f"\nResults saved to {args.output}")
        else:
            with open(input_path, "rb") as f:
                data = f.read()
            results = benchmark_hybrid(data, len(data))


if __name__ == "__main__":
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )
    raise SystemExit(main())
