#!/data/data/com.termux/files/usr/bin/env python
"""Compress or decompress the files of a tree with bzip2 or gzip.
Subcommands: ``bz2`` handles the bzip2 files, ``gz`` the gzip and tar files and ``gzip-files`` recompresses the plain files recursively (skipping the already compressed extensions and the small files); ``-l`` sets the level and the chunks are streamed in 512 KB blocks.
Usage: gzbz {bz2,gz,gzip-files} [-d] [-l LEVEL] [paths ...] Positional arguments: paths ...
Files or directories to process (default: current working directory) directories ...
Directories to walk recursively (default: '.') Subcommands: bz2 Bzip2 compression / decompression (bzr.py) gz Gzip compression / decompression with tar support (gzr.py) gzip-files Recursive gzip on files only (gziper.py) Options: -d, --decompress Decompress instead of compress -c, --compress Explicitly select compression (default) -l, --level --chunk-size --mem-threshold --min-file-size -e, --exclude Additional file extensions to exclude (e.g.
.pdf .jpg) Examples: $ gzbz gz -d logs/ $ gzbz gzip-files -l 9 .
Command-line script: run ``python gzbz.py`` or the ``gzbz`` command.
The module is importable as well."""

import argparse
import bz2
from concurrent.futures import ProcessPoolExecutor
import contextlib
from dataclasses import dataclass
from datetime import timedelta
import gzip
import mmap
from pathlib import Path
import shutil
import tarfile
import time
from typing import Any, Iterable, Optional, Self, Sequence

from dh import MAX_WORKERS, workers
from loguru import logger


DEFAULT_CHUNK_SIZE: int = 524_288
DEFAULT_GZIPER_CHUNK: int = 262_144
DEFAULT_MEM_THRESHOLD: int = 32_768
DEFAULT_MIN_FILE_SIZE: int = 1_024
DEFAULT_BZ2_LEVEL: int = 9
DEFAULT_GZ_LEVEL: int = 9
DEFAULT_GZIPER_LEVEL: int = 9
EXCLUDE_BZ2: frozenset[str] = frozenset({".bz2", ".xz", ".gz", ".br", ".zst", ".7z", ".zip", ".rar"})
EXCLUDE_GZ: frozenset[str] = frozenset({".gz", ".bz2", ".xz", ".br", ".zst", ".7z", ".zip", ".rar"})
EXCLUDE_GZIPER: frozenset[str] = frozenset({".gz", ".zip", ".bz2", ".xz", ".7z", ".rar", ".tar"})


def human_size(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(n)
    for u in units:
        if v < 1024.0 or u == units[-1]:
            return f"{int(v)} B" if u == "B" else f"{v:.1f} {u}"
        v /= 1024.0
    return f"{v:.1f} TB"


def human_time(seconds: int) -> str:
    return str(timedelta(seconds=seconds))


def compress_bytes(data: bytes, algo: str, level: int) -> bytes:
    if algo == "bz2":
        return bz2.compress(data, compresslevel=level)
    if algo == "gz":
        return gzip.compress(data, compresslevel=level)
    msg = f"unknown algorithm: {algo!r}"
    raise ValueError(msg)


def decompress_bytes(data: bytes, algo: str) -> bytes:
    if algo == "bz2":
        return bz2.decompress(data)
    if algo == "gz":
        return gzip.decompress(data)
    msg = f"unknown algorithm: {algo!r}"
    raise ValueError(msg)


def _worker_compress_chunk(args: tuple[int, bytes, str, int]) -> tuple[int, bytes]:
    idx, data, algo, level = args
    return idx, compress_bytes(data, algo, level)


def _worker_gzip_file(args: tuple[str, int, int]) -> tuple[str, bool, int, int, str]:
    path_str, chunk_size, level = args
    src = Path(path_str)
    dst = Path(str(src) + ".gz")
    try:
        size = src.stat().st_size
        with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=level) as fo:
            shutil.copyfileobj(fi, fo, chunk_size)
        csize = dst.stat().st_size
        src.unlink()
        return (path_str, True, size, csize, "")
    except Exception as exc:
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return (path_str, False, 0, 0, str(exc))


@dataclass
class Config:
    algo: str
    level: int
    chunk_size: int = DEFAULT_CHUNK_SIZE
    mem_threshold: int = DEFAULT_MEM_THRESHOLD
    min_file_size: int = DEFAULT_MIN_FILE_SIZE

    @property
    def file_ext(self) -> str:
        return ".bz2" if self.algo == "bz2" else ".gz"

    @property
    def tar_ext(self) -> str:
        return ".tar.bz2" if self.algo == "bz2" else ".tar.gz"

    @property
    def exclude_ext(self) -> frozenset[str]:
        return EXCLUDE_BZ2 if self.algo == "bz2" else EXCLUDE_GZ


class Tool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pool: Optional[ProcessPoolExecutor] = None

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.cfg.workers)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _compress_small(self, src: Path, dst: Path) -> bool:
        try:
            data = src.read_bytes()
            if not data:
                return False
            dst.write_bytes(compress_bytes(data, self.cfg.algo, self.cfg.level))
            return True
        except (OSError, MemoryError) as exc:
            logger.error(f"Memory compression failed for {src.name}: {exc}")
            return False

    def _compress_chunked(self, src: Path, dst: Path, size: int) -> bool:
        chunk = self.cfg.chunk_size
        try:
            n = (size + chunk - 1) // chunk
            with (
                dst.open("wb", buffering=1 << 20) as out,
                src.open("rb") as fh,
                mmap.mmap(fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm,
            ):
                slices = [(i, mm[i * chunk : min((i + 1) * chunk, size)]) for i in range(n)]
            pool = self._get_pool()
            futures = [
                pool.submit(_worker_compress_chunk, (idx, data, self.cfg.algo, self.cfg.level)) for idx, data in slices
            ]
            outputs: list[Optional[bytes]] = [None] * n
            for fut in futures:
                try:
                    idx, blob = fut.result()
                    outputs[idx] = blob
                except Exception as exc:
                    logger.error(f"Chunk compression failed: {exc}")
                    return False
            with dst.open("wb", buffering=1 << 20) as out:
                for blob in outputs:
                    if blob is None:
                        return False
                    out.write(blob)
            return True
        except (OSError, MemoryError) as exc:
            logger.error(f"Chunked compression failed for {src.name}: {exc}")
            return False

    def _compress_file(self, src: Path, dst: Path, size: int) -> bool:
        if size < self.cfg.mem_threshold:
            return self._compress_small(src, dst)
        return self._compress_chunked(src, dst, size)

    @staticmethod
    def _create_tar(src_dir: Path, tar_path: Path) -> bool:
        try:
            with tarfile.open(tar_path, "w") as tf:
                for p in src_dir.rglob("*"):
                    if p.is_file():
                        arcname = p.relative_to(src_dir.parent)
                        tf.add(p, arcname=str(arcname))
            return True
        except (OSError, tarfile.TarError) as exc:
            logger.error(f"  Failed to create tar archive: {exc}")
            return False

    @staticmethod
    def _extract_tar(tar_path: Path, dest: Path) -> bool:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path, "r") as tf:
                tf.extractall(dest)
            return True
        except (OSError, tarfile.TarError) as exc:
            logger.error(f"  Failed to extract tar archive: {exc}")
            return False

    def compress_dir(self, src_dir: Path) -> bool:
        tar_path = src_dir.with_name(src_dir.name + ".tar")
        out_path = src_dir.with_name(src_dir.name + self.cfg.tar_ext)
        try:
            print("  Creating tar archive...")
            if not self._create_tar(src_dir, tar_path) or not tar_path.exists():
                logger.error("  Failed to create tar archive")
                return False
            print(f"  Compressing tar archive with {self.cfg.algo} (level {self.cfg.level})...")
            size = tar_path.stat().st_size
            if not self._compress_file(tar_path, out_path, size):
                return False
            if not out_path.exists():
                return False
            csize = out_path.stat().st_size
            if csize == 0:
                logger.warning(f"Compressed archive empty for {src_dir.name}")
                out_path.unlink()
                return False
            if csize < size:
                tar_path.unlink()
                saved_pct = (size - csize) / size * 100
                print(f"  ✓ Compressed archive: {saved_pct:.1f}% saved ({human_size(size)} → {human_size(csize)})")
                shutil.rmtree(src_dir)
                return True
            print("  ✗ Archive compression didn't save space, keeping .tar")
            out_path.unlink()
            return False
        except (OSError, MemoryError, tarfile.TarError) as exc:
            logger.error(f"  ✗ Failed to compress tar archive: {exc}")
            if tar_path.exists():
                tar_path.unlink()
            if out_path.exists():
                out_path.unlink()
            return False

    def compress_single_file(self, src: Path) -> tuple[bool, int, int]:
        dst = Path(str(src) + self.cfg.file_ext)
        if dst.exists():
            print(f"Skipping {src.name} - output already exists")
            return (False, 0, 0)
        try:
            size = src.stat().st_size
            if not size:
                return (False, 0, 0)
            if not self._compress_file(src, dst, size) or not dst.exists():
                return (False, 0, 0)
            csize = dst.stat().st_size
            if csize == 0:
                logger.warning(f"Compressed file empty for {src.name}")
                dst.unlink()
                return (False, 0, 0)
            if csize < size:
                src.unlink()
                saved_pct = (size - csize) / size * 100
                print(f"  ✓ {src.name}: {saved_pct:.1f}% saved ({human_size(size)} → {human_size(csize)})")
                return (True, size, csize)
            print(f"  ✗ {src.name}: No space saved, removing compressed file")
            dst.unlink()
            return (False, 0, 0)
        except (OSError, PermissionError, MemoryError) as exc:
            logger.error(f"  ✗ Failed to compress {src.name}: {exc}")
            return (False, 0, 0)

    def decompress_single_file(self, src: Path) -> bool:
        if src.suffix != self.cfg.file_ext:
            return False
        dst = src.with_suffix("")
        try:
            data = src.read_bytes()
            if not data:
                return False
            dst.write_bytes(decompress_bytes(data, self.cfg.algo))
            print(f"  ✓ Decompressed {src.name}: {human_size(src.stat().st_size)} → {human_size(dst.stat().st_size)}")
            src.unlink()
            return True
        except (OSError, EOFError, ValueError) as exc:
            logger.error(f"  ✗ Failed to decompress {src.name}: {exc}")
            return False

    def decompress_archive(self, archive: Path) -> bool:
        tar_path = archive.with_suffix("")
        dest = archive.parent
        try:
            print(f"\n  Decompressing {archive.name}...")
            data = archive.read_bytes()
            tar_path.write_bytes(decompress_bytes(data, self.cfg.algo))
            print(f"    Extracting tar to {dest}/...")
            if self._extract_tar(tar_path, dest):
                tar_path.unlink()
                archive.unlink()
                print(f"  ✓ Extracted {archive.name} to {dest}/")
                return True
            logger.error(f"  ✗ Failed to extract {archive.name}")
            return False
        except (OSError, EOFError, ValueError, tarfile.TarError) as exc:
            logger.error(f"  ✗ Failed to decompress {archive.name}: {exc}")
            if tar_path.exists():
                tar_path.unlink()
            return False

    def collect_dirs(self, root: Path) -> list[Path]:
        return [p for p in root.glob("*") if p.is_dir() and not p.is_symlink()]

    def collect_files(self, root: Path) -> list[Path]:
        out: list[Path] = []
        for p in root.glob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            if p.suffix in self.cfg.exclude_ext:
                continue
            try:
                if p.stat().st_size >= self.cfg.min_file_size:
                    out.append(p)
            except (OSError, PermissionError):
                pass
        return out

    def collect_archives(self, root: Path) -> list[Path]:
        return [p for p in root.glob(f"*{self.cfg.tar_ext}") if p.is_file() and not p.is_symlink()]

    def collect_compressed(self, root: Path) -> list[Path]:
        ext = self.cfg.file_ext
        return [
            p
            for p in root.glob(f"*{ext}")
            if p.is_file() and not p.is_symlink() and not p.name.endswith(self.cfg.tar_ext)
        ]

    def compress(self, paths: Sequence[Path]) -> None:
        cwd = Path.cwd()
        if paths:
            dirs = [p for p in paths if p.is_dir()]
            files = [p for p in paths if p.is_file()]
        else:
            dirs = self.collect_dirs(cwd)
            files = self.collect_files(cwd)
        if dirs:
            print(f"\n📁 Compressing {len(dirs)} directories...")
            for d in sorted(dirs):
                try:
                    rel = d.relative_to(cwd)
                except ValueError:
                    rel = d
                print(f"\n  Processing {rel}...")
                if self.compress_dir(d):
                    print(f"  ✓ Successfully compressed {rel} to {d.name}{self.cfg.tar_ext}")
                else:
                    logger.error(f"  ✗ Failed to compress {rel}")
        if not files:
            print("\n📄 No files to compress")
            return
        print(f"\n📄 Compressing {len(files)} files with {self.cfg.algo} max compression...")
        orig = comp = ok = 0
        for i, f in enumerate(sorted(files), 1):
            print(f"\n[{i}/{len(files)}] {f.name}")
            success, o, c = self.compress_single_file(f)
            if success:
                ok += 1
                orig += o
                comp += c
        if ok > 0:
            saved = orig - comp
            pct = saved / orig * 100 if orig else 0.0
            print(f"\n{'=' * 40}")
            print(f"✅ Compressed {ok}/{len(files)} files")
            print(f"📊 Original size: {human_size(orig)}")
            print(f"📦 Compressed size: {human_size(comp)}")
            print(f"💾 Space saved: {human_size(saved)} ({pct:.1f}%)")
            print(f"{'=' * 40}")
        elif files:
            logger.error("\n❌ No files were successfully compressed")

    def decompress(self, paths: Sequence[Path]) -> None:
        cwd = Path.cwd()
        if paths:
            archives = [p for p in paths if p.is_file() and p.name.endswith(self.cfg.tar_ext)]
        else:
            archives = self.collect_archives(cwd)
        if archives:
            print(f"\n📦 Decompressing {len(archives)} archives...")
            for a in sorted(archives):
                self.decompress_archive(a)
        if paths:
            singles = [
                p
                for p in paths
                if p.is_file() and p.suffix == self.cfg.file_ext and not p.name.endswith(self.cfg.tar_ext)
            ]
        else:
            singles = self.collect_compressed(cwd)
        if not singles:
            print(f"\n📄 No {self.cfg.file_ext} files to decompress")
            return
        print(f"\n📄 Decompressing {len(singles)} {self.cfg.algo} files...")
        total_in = total_out = ok = 0
        for i, f in enumerate(sorted(singles), 1):
            print(f"\n[{i}/{len(singles)}] {f.name}")
            with contextlib.suppress(OSError):
                total_in += f.stat().st_size
            if self.decompress_single_file(f):
                ok += 1
                dst = f.with_suffix("")
                if dst.exists():
                    total_out += dst.stat().st_size
        if ok > 0:
            print(f"\n{'=' * 40}")
            print(f"✅ Decompressed {ok}/{len(singles)} files")
            print(f"📦 Compressed size: {human_size(total_in)}")
            print(f"📊 Decompressed size: {human_size(total_out)}")
            print(f"{'=' * 40}")
        elif singles:
            logger.error("\n❌ No files were successfully decompressed")


def run_gzip_files(
    directories: Sequence[Path],
    excludes: Sequence[str],
    workers: int,
    chunk_size: int,
    level: int,
) -> int:
    exclude_set: set[str] = set(EXCLUDE_GZIPER)
    for e in excludes:
        if not e.startswith("."):
            e = "." + e
        exclude_set.add(e)
    resolved_dirs = [d.resolve() for d in directories]
    print("=" * 40)
    print("🔍 GZIP Compression Tool (Maximum Compression - Level 9)".center(70))
    print("-" * 40)
    print("📂 Processing directories:")
    for d in resolved_dirs:
        print("   •", d)
    print("🚫 Excluding extensions:", ",".join(sorted(exclude_set)))
    files: list[Path] = []
    for d in resolved_dirs:
        if not d.exists():
            logger.warning(f"Directory '{d}' does not exist, skipping...")
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix not in exclude_set:
                files.append(p)
    if not files:
        logger.success("✅ No files found to compress!")
        return 0
    print("📊 Found", len(files), "file(s) to compress")
    print("-" * 40)
    print(f"{'File':<50} {'Original':>10} {'Compressed':>10} {'Ratio':>8} {'Status':>10}")
    print("-" * 40)
    total_files = ok = fail = 0
    total_orig = total_comp = 0
    start = time.time()
    cwd = Path.cwd()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker_gzip_file, (str(f), chunk_size, level)) for f in files]
        for fut in futures:
            path_str, success, o, c, err = fut.result()
            total_files += 1
            try:
                rel = str(Path(path_str).relative_to(cwd))
            except ValueError:
                rel = path_str
            display = ("..." + rel[-44:]) if len(rel) > 47 else rel
            if success:
                ok += 1
                total_orig += o
                total_comp += c
                ratio = f"{(1 - c / o) * 100:.1f}%" if o else "N/A"
                print(f"{display:<50} {human_size(o):>10} {human_size(c):>10} {ratio:>8} {'✅':>10}")
            else:
                fail += 1
                print(f"{display:<50} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'❌':>10}")
                if err:
                    logger.warning(f"   ⚠ Error: {err}")
    elapsed = time.time() - start
    print("=" * 40)
    print("📊 COMPRESSION SUMMARY".center(70))
    print("-" * 40)
    print(f"  Total files processed: {total_files}")
    print(f"  Successfully compressed: {ok} ✅")
    print(f"  Failed compressions: {fail} ❌")
    print(f"  Original total size: {human_size(total_orig)}")
    print(f"  Compressed total size: {human_size(total_comp)}")
    if total_orig > 0:
        ratio = (1 - total_comp / total_orig) * 100
        saved = total_orig - total_comp
        print(f"  Overall compression ratio: {ratio:.1f}%")
        print(f"  Space saved: {human_size(saved)}")
    print(f"  Time elapsed: {human_time(int(elapsed))}")
    print("-" * 40)
    return 0


def _add_pool_options(sp: argparse.ArgumentParser, algo_default_level: int, chunk_default: int) -> None:
    sp.add_argument("-d", "--decompress", action="store_true", help="Decompress instead of compress")
    sp.add_argument(
        "-c",
        "--compress",
        action="store_true",
        help="Explicitly select compression (default)",
    )
    sp.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: current working directory)",
    )
    sp.add_argument(
        "-l",
        "--level",
        type=int,
        default=algo_default_level,
        help=f"Compression level (default: {algo_default_level})",
    )
    sp.add_argument(
        "--chunk-size",
        type=int,
        default=chunk_default,
        help=f"Chunk size for parallel compression in bytes (default: {chunk_default})",
    )
    sp.add_argument(
        "--mem-threshold",
        type=int,
        default=DEFAULT_MEM_THRESHOLD,
        help=f"Files smaller than this are compressed in memory (default: {DEFAULT_MEM_THRESHOLD})",
    )
    sp.add_argument(
        "--min-file-size",
        type=int,
        default=DEFAULT_MIN_FILE_SIZE,
        help=f"Ignore files smaller than this many bytes (default: {DEFAULT_MIN_FILE_SIZE})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compress_tool",
        description="Unified bz2/gz compression & decompression tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Mapping from the original scripts:\n"
            "  bzr.py     -> compress_tool.py bz2         [paths...]\n"
            "  bzr.py -d  -> compress_tool.py bz2 -d      [paths...]\n"
            "  gzr.py     -> compress_tool.py gz          [paths...]\n"
            "  gzr.py -d  -> compress_tool.py gz  -d      [paths...]\n"
            "  gziper.py  -> compress_tool.py gzip-files  [dirs...]\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)
    bz2_p = sub.add_parser("bz2", help="bzip2 compression / decompression (bzr.py)")
    _add_pool_options(bz2_p, DEFAULT_BZ2_LEVEL, DEFAULT_CHUNK_SIZE)
    gz_p = sub.add_parser("gz", help="gzip compression / decompression with tar support (gzr.py)")
    _add_pool_options(gz_p, DEFAULT_GZ_LEVEL, DEFAULT_CHUNK_SIZE)
    gf_p = sub.add_parser("gzip-files", help="recursive gzip on files only (gziper.py)")
    gf_p.add_argument(
        "directories",
        nargs="*",
        type=Path,
        help="Directories to walk recursively (default: '.')",
    )
    gf_p.add_argument(
        "-e",
        "--exclude",
        nargs="+",
        default=[],
        help="Additional file extensions to exclude (e.g. .pdf .jpg)",
    )
    gf_p.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_GZIPER_CHUNK,
        help=f"Streaming copy buffer size in bytes (default: {DEFAULT_GZIPER_CHUNK})",
    )
    gf_p.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_GZIPER_LEVEL,
        help=f"gzip compression level (default: {DEFAULT_GZIPER_LEVEL})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "gzip-files":
            return run_gzip_files(
                directories=args.directories or [Path()],
                excludes=args.exclude,
                workers=MAX_WORKERS,
                chunk_size=args.chunk_size,
                level=args.level,
            )
        algo = "bz2" if args.command == "bz2" else "gz"
        cfg = Config(
            algo=algo,
            level=args.level,
            workers=MAX_WORKERS,
            chunk_size=args.chunk_size,
            mem_threshold=args.mem_threshold,
            min_file_size=args.min_file_size,
        )
        paths = [p.resolve() for p in (args.paths or [])]
        with Tool(cfg) as tool:
            if args.decompress:
                tool.decompress(paths)
            else:
                tool.compress(paths)
        return 0
    except KeyboardInterrupt:
        logger.warning("\n\n⚠️  Interrupted by user")
        return 1
    except Exception as exc:
        logger.error(f"\n❌ Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
