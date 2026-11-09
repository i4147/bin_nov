#!/data/data/com.termux/files/usr/bin/env python
"""Compress or decompress files and directories with Brotli.
Files are turned into ``.br`` streams and directories into ``.tar.br`` archives, in worker pools.
Chunked mode is used automatically for large inputs and the original is kept when compression does not shrink it (unless ``--force``).
Usage: brer [options] <compress|decompress> [args ...] Subcommands: compress Compress files (.br) and dirs (.tar.br) decompress Decompress .br files and .tar.br archives Options: -q, --quality Brotli quality 0-11.
Default 11.
--lgwin Brotli window bits 10-24.
Default 24.
--buffer-size Stream buffer size (bytes).
Default 65536.
--chunk-size Chunk size for chunked mode (bytes).
Default 32768.
--chunk-threshold Files >= this size use chunked compression.
0 disables chunking.
Default 524288.
--min-size Skip files smaller than this many bytes.
Default 1024.
--force Always replace original even if compression grew the file.
Default: keep original when compression does not save space.
--executor Top-level worker pool type.
Default process.
--skip-ext Comma-separated list of extensions to skip.
Examples: $ brer compress notes.txt $ brer decompress notes.txt.br Command-line script: run ``python brer.py`` or the ``brer`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import mmap
from pathlib import Path
import shutil
import tarfile
from typing import Any, Sequence

import brotli
from dh import MAX_WORKERS
from loguru import logger


try:
    from loguru import logger

    if not hasattr(logger, "success"):
        logger.success = logger.info
except ImportError:
    logger.success = logger.info


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def _compress_bytes(data: bytes, quality: int, lgwin: int) -> bytes:
    return brotli.compress(data, quality=quality, lgwin=lgwin, mode=brotli.MODE_GENERIC)


def _stream_compress(src: Path, dst: Path, buffer_size: int, quality: int, lgwin: int) -> bool:
    try:
        comp = brotli.Compressor(quality=quality, lgwin=lgwin)
        with open(src, "rb") as f_in, open(dst, "wb") as f_out:
            while True:
                chunk = f_in.read(buffer_size)
                if not chunk:
                    break
                f_out.write(comp.process(chunk))
            f_out.write(comp.finish())
        return True
    except Exception as e:
        logger.error(f"Error compressing to {dst.name}: {e}")
        return False


def _stream_decompress(src: Path, dst: Path, buffer_size: int) -> bool:
    try:
        dec = brotli.Decompressor()
        with open(src, "rb") as f_in, open(dst, "wb") as f_out:
            while True:
                chunk = f_in.read(buffer_size)
                if not chunk:
                    break
                f_out.write(dec.process(chunk))
        return True
    except Exception as e:
        logger.error(f"Error decompressing {src.name}: {e}")
        return False


def _chunked_compress(src: Path, dst: Path, size: int, chunk_size: int, quality: int, lgwin: int, workers: int) -> bool:
    try:
        n = (size + chunk_size - 1) // chunk_size
        with (
            ThreadPoolExecutor(max_workers=workers) as ex,
            open(dst, "wb", buffering=1 << 20) as f_out,
            open(src, "rb") as f_in,
            mmap.mmap(f_in.fileno(), length=0, access=mmap.ACCESS_READ) as mm,
        ):
            futs = [
                ex.submit(_compress_bytes, mm[i * chunk_size : min((i + 1) * chunk_size, size)], quality, lgwin)
                for i in range(n)
            ]
            blobs: list[bytes] = []
            for idx, fut in enumerate(futs):
                try:
                    blobs.append(fut.result())
                except Exception as e:
                    logger.error(f"Chunk {idx} compression failed: {e}")
                    return False
            for blob in blobs:
                if not blob:
                    return False
                f_out.write(blob)
        return True
    except Exception as e:
        logger.error(f"Chunked compression failed for {src.name}: {e}")
        return False


def _tar(src_dir: Path, tar_path: Path) -> bool:
    try:
        with tarfile.open(tar_path, "w") as tar:
            for p in src_dir.rglob("*"):
                if p.is_file():
                    tar.add(p, arcname=str(p.relative_to(src_dir.parent)))
        return True
    except Exception as e:
        logger.error(f"Failed to create tar archive: {e}")
        return False


def _untar(tar_path: Path, dest: Path) -> bool:
    try:
        with tarfile.open(tar_path, "r") as tar:
            tar.extractall(dest)
        return True
    except Exception as e:
        logger.error(f"Failed to extract tar archive: {e}")
        return False


def _compress_to(src: Path, dst: Path, args: list[Any]) -> bool:
    size = src.stat().st_size
    if args.chunk_threshold > 0 and size >= args.chunk_threshold:
        return _chunked_compress(src, dst, size, args.chunk_size, args.quality, args.lgwin, MAX_WORKERS)
    return _stream_compress(src, dst, args.buffer_size, args.quality, args.lgwin)


def _finish_compress(src: Path, dst: Path, args: list[Any], label: str) -> bool:
    if not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return False
    src_size = src.stat().st_size
    dst_size = dst.stat().st_size
    if args.force:
        src.unlink()
        logger.success(f"Compressed: {label}")
        return True
    if dst_size < src_size:
        src.unlink()
        saved = (src_size - dst_size) / src_size * 100 if src_size else 0.0
        print(f"  ✓ {label}: {saved:.1f}% saved ({human_size(src_size)} -> {human_size(dst_size)})")
        return True
    print(f"  ✗ {label}: no space saved, keeping original")
    dst.unlink()
    return False


def compress_file(src: Path, args: list[Any]) -> bool:
    if src.stat().st_size < args.min_size:
        return False
    dst = src.with_name(f"{src.name}.br")
    if dst.exists():
        print(f"  Skipping {src.name}: output already exists")
        return False
    if not _compress_to(src, dst, args):
        return False
    return _finish_compress(src, dst, args, src.name)


def compress_dir(src_dir: Path, args: list[Any]) -> bool:
    tmp_tar = src_dir.with_name(f"{src_dir.name}.tar")
    dst = src_dir.with_name(f"{src_dir.name}.tar.br")
    if not _tar(src_dir, tmp_tar):
        return False
    if not _compress_to(tmp_tar, dst, args):
        tmp_tar.unlink(missing_ok=True)
        return False
    src_size = tmp_tar.stat().st_size
    dst_size = dst.stat().st_size if dst.exists() else 0
    if dst_size == 0:
        dst.unlink(missing_ok=True)
        tmp_tar.unlink(missing_ok=True)
        return False
    if not args.force and dst_size >= src_size:
        print(f"  ✗ {src_dir.name}: no space saved, keeping original")
        dst.unlink()
        tmp_tar.unlink()
        return False
    tmp_tar.unlink()
    shutil.rmtree(src_dir)
    saved = (src_size - dst_size) / src_size * 100 if src_size else 0.0
    print(f"  ✓ {src_dir.name}: {saved:.1f}% saved ({human_size(src_size)} -> {human_size(dst_size)})")
    return True


def decompress_br(src: Path, args: list[Any]) -> bool:
    if src.name.endswith(".tar.br"):
        return False
    dst = src.with_suffix("")
    if dst.exists():
        print(f"  Skipping {src.name}: output already exists")
        return False
    if not _stream_decompress(src, dst, args.buffer_size):
        return False
    old = src.stat().st_size
    new = dst.stat().st_size
    print(f"  ✓ Decompressed {src.name}: {human_size(old)} -> {human_size(new)}")
    src.unlink()
    return True


def decompress_tar_br(src: Path, args: list[Any]) -> bool:
    dst_dir = src.with_name(src.name[: -len(".tar.br")])
    tmp_tar = src.with_name(src.name[: -len(".br")])
    if not _stream_decompress(src, tmp_tar, args.buffer_size):
        return False
    if not _untar(tmp_tar, dst_dir.parent):
        tmp_tar.unlink(missing_ok=True)
        return False
    tmp_tar.unlink()
    src.unlink()
    print(f"  ✓ Extracted {src.name} -> {dst_dir.name}/")
    return True


def _iter_dirs(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]


def _iter_files(root: Path, self_name: str, skip_ext: Sequence[str], min_size: int) -> list[Path]:
    out: list[Path] = []
    for p in root.iterdir():
        if not p.is_file() or p.is_symlink():
            continue
        if p.name == self_name:
            continue
        if p.suffix in skip_ext or p.suffix == ".br":
            continue
        try:
            if p.stat().st_size < min_size:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _iter_archives(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and (p.name.endswith(".tar.br") or p.suffix == ".br")
    ]


def cmd_compress(args: list[Any]) -> int:
    root = Path.cwd()
    self_name = Path(__file__).name
    skip_ext = tuple(args.skip_ext.split(",")) if args.skip_ext else ()
    dirs = _iter_dirs(root)
    files = _iter_files(root, self_name, skip_ext, args.min_size)
    if not dirs and not files:
        logger.warning("No files or subdirectories found to compress.")
        return 0
    print(f"Found {len(dirs)} subdirs and {len(files)} files to compress.")
    print(f"Starting parallel compression (quality={args.quality}, lgwin={args.lgwin}, workers={MAX_WORKERS})...")
    Executor = ThreadPoolExecutor if args.executor == "thread" else ProcessPoolExecutor
    with Executor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(compress_dir, d, args) for d in dirs]
        futures += [ex.submit(compress_file, f, args) for f in files]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Task failed: {e}")
    logger.success("Compression completed.")
    return 0


def cmd_decompress(args: list[Any]) -> int:
    root = Path.cwd()
    archives = _iter_archives(root)
    if not archives:
        logger.warning("No .br or .tar.br files found to decompress.")
        return 0
    tar_brs = [p for p in archives if p.name.endswith(".tar.br")]
    brs = [p for p in archives if not p.name.endswith(".tar.br")]
    print(f"Found {len(tar_brs)} .tar.br archives and {len(brs)} .br files to decompress.")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(decompress_tar_br, p, args) for p in tar_brs]
        futures += [ex.submit(decompress_br, p, args) for p in brs]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Task failed: {e}")
    logger.success("Decompression completed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified Brotli compression / decompression tool (br.py + br2.py + brer.py).",
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    c = sub.add_parser("compress", help="Compress files (.br) and dirs (.tar.br)")
    c.add_argument("-q", "--quality", type=int, default=11, help="Brotli quality 0-11. Default 11.")
    c.add_argument("--lgwin", type=int, default=24, help="Brotli window bits 10-24. Default 24.")
    c.add_argument("--buffer-size", type=int, default=65536, help="Stream buffer size (bytes). Default 65536.")
    c.add_argument("--chunk-size", type=int, default=32768, help="Chunk size for chunked mode (bytes). Default 32768.")
    c.add_argument(
        "--chunk-threshold",
        type=int,
        default=524288,
        help="Files >= this size use chunked compression. 0 disables chunking. Default 524288.",
    )
    c.add_argument("--min-size", type=int, default=1024, help="Skip files smaller than this many bytes. Default 1024.")
    c.add_argument(
        "--force",
        action="store_true",
        help="Always replace original even if compression grew the file. "
        "Default: keep original when compression does not save space.",
    )
    c.add_argument(
        "--executor",
        choices=("process", "thread"),
        default="process",
        help="Top-level worker pool type. Default process.",
    )
    c.add_argument(
        "--skip-ext", default=".br,.xz,.gz,.bz2,.7z,.zip,.tar", help="Comma-separated list of extensions to skip."
    )
    d = sub.add_parser("decompress", help="Decompress .br files and .tar.br archives")
    d.add_argument("--buffer-size", type=int, default=65536, help="Stream buffer size (bytes). Default 65536.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compress":
        return cmd_compress(args)
    if args.command == "decompress":
        return cmd_decompress(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
