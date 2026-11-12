#!/data/data/com.termux/files/usr/bin/env python
"""Compare every available compression codec on one file or folder.
Runs gzip, bz2, xz, zip, brotli, zstandard, lz4, 7z, lzma, snappy, zopfli, PPMd and deflate64 at several levels (each codec only when its package is installed), prints size/ratio/time for every combination, then copies the smallest result next to the input.
Usage: compression_benchmark FILE_OR_FOLDER Examples: $ compression_benchmark dump.sql Runs as a script (``python compression_benchmark.py``) in the directory it should act on; the module can also be imported.
"""

import bz2
import gzip
import lzma
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import time
from typing import Callable, Optional
import zipfile

try:
    import brotli
except ImportError:
    brotli = None
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import lz4.frame as lz4frame
except ImportError:
    lz4frame = None
try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import pylzma
except ImportError:
    pylzma = None
try:
    import cramjam
except ImportError:
    cramjam = None
try:
    import zopfli.gzip as zopfli_gzip
except ImportError:
    zopfli_gzip = None
try:
    import pyppmd
except ImportError:
    pyppmd = None
try:
    import pyzstd
except ImportError:
    pyzstd = None
try:
    import zipfile_deflate64
except ImportError:
    zipfile_deflate64 = None
try:
    import pyzipper
except ImportError:
    pyzipper = None
Compressor = Callable[[Path, Path, Optional[int]], None]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def make_tar(src_dir: Path, tar_path: Path) -> None:
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src_dir, arcname=src_dir.name)


def _read_all(src: Path) -> bytes:
    with open(src, "rb") as f:
        return f.read()


def _write_all(dst: Path, data: bytes) -> None:
    with open(dst, "wb") as f:
        f.write(data)


def compress_gzip(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_bz2(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with open(src, "rb") as fin, bz2.open(dst, "wb", compresslevel=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_lzma(src: Path, dst: Path, level: Optional[int]) -> None:
    preset = 6 if level is None else level
    with open(src, "rb") as fin, lzma.open(dst, "wb", preset=preset) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_zip(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=lv) as zf:
        zf.write(src, arcname=src.name)


def compress_brotli(src: Path, dst: Path, level: Optional[int]) -> None:
    if brotli is None:
        msg = "brotli not installed"
        raise RuntimeError(msg)
    q = 11 if level is None else level
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        comp = brotli.Compressor(quality=q)
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            fout.write(comp.process(chunk))
        fout.write(comp.finish())


def compress_zstd(src: Path, dst: Path, level: Optional[int]) -> None:
    if zstd is None:
        msg = "zstandard not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    cctx = zstd.ZstdCompressor(level=lv)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        cctx.copy_stream(fin, fout)


def compress_lz4(src: Path, dst: Path, level: Optional[int]) -> None:
    if lz4frame is None:
        msg = "lz4 not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    with open(src, "rb") as fin, lz4frame.open(dst, "wb", compression_level=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_7z(src: Path, dst: Path, level: Optional[int]) -> None:
    if py7zr is None:
        msg = "py7zr not installed"
        raise RuntimeError(msg)
    preset = 7 if level is None else level
    filters = [{"id": py7zr.FILTER_LZMA2, "preset": preset}]
    with py7zr.SevenZipFile(dst, "w", filters=filters) as zf:
        zf.write(src, arcname=src.name)


def compress_pylzma(src: Path, dst: Path, level: Optional[int]) -> None:
    if pylzma is None:
        msg = "pylzma not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    data = _read_all(src)
    out = pylzma.compress(data, filters=[{"id": pylzma.FILTER_LZMA1, "preset": lv}])
    _write_all(dst, out)


def compress_snappy(src: Path, dst: Path, _level: Optional[int]) -> None:
    if cramjam is None:
        msg = "cramjam not installed"
        raise RuntimeError(msg)
    data = _read_all(src)
    out = cramjam.snappy.compress(data)
    _write_all(dst, bytes(out))


def compress_zopfli_gzip(src: Path, dst: Path, level: Optional[int]) -> None:
    if zopfli_gzip is None:
        msg = "zopfli not installed"
        raise RuntimeError(msg)
    iters = 15 if level is None else level
    data = _read_all(src)
    out = zopfli_gzip.compress(data, numiterations=iters)
    _write_all(dst, out)


def compress_ppmd(src: Path, dst: Path, level: Optional[int]) -> None:
    if pyppmd is None:
        msg = "pyppmd not installed"
        raise RuntimeError(msg)
    order = 6 if level is None else level
    data = _read_all(src)
    out = pyppmd.compress(data, max_order=order, mem_size=64 * 1024 * 1024)
    _write_all(dst, out)


def compress_pyzstd(src: Path, dst: Path, level: Optional[int]) -> None:
    if pyzstd is None:
        msg = "pyzstd not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    data = _read_all(src)
    out = pyzstd.compress(data, level_or_option=lv)
    _write_all(dst, out)


def compress_zip_deflate64(src: Path, dst: Path, _level: Optional[int]) -> None:
    if zipfile_deflate64 is None:
        msg = "zipfile-deflate64 not installed"
        raise RuntimeError(msg)
    with zipfile_deflate64.ZipFile(dst, "w") as zf:
        zf.write(src, arcname=src.name)


def compress_zip_lzma(src: Path, dst: Path, _level: Optional[int]) -> None:
    if pyzipper is None:
        msg = "pyzipper not installed"
        raise RuntimeError(msg)
    with pyzipper.AESZipFile(dst, "w", compression=pyzipper.ZIP_LZMA) as zf:
        zf.write(src, arcname=src.name)


def compress_zip_bzip2(src: Path, dst: Path, _level: Optional[int]) -> None:
    if pyzipper is None:
        msg = "pyzipper not installed"
        raise RuntimeError(msg)
    with pyzipper.AESZipFile(dst, "w", compression=pyzipper.ZIP_BZIP2) as zf:
        zf.write(src, arcname=src.name)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file_or_folder>")
        return 1
    input_path = Path(sys.argv[1]).expanduser().resolve()
    if not input_path.exists():
        print(f"Error: path does not exist: {input_path}")
        return 1
    tmpdir = Path(tempfile.mkdtemp(prefix="compress_bench_"))
    print(f"Working dir: {tmpdir}\n")
    try:
        if input_path.is_dir():
            print(f"Input is a folder -> creating tar of {input_path.name} ...")
            src_file = tmpdir / f"{input_path.name}.tar"
            make_tar(input_path, src_file)
            print(f"Tar created: {src_file} ({human(src_file.stat().st_size)})\n")
        else:
            src_file = input_path
        original_size = src_file.stat().st_size
        print(f"Source file  : {src_file}")
        print(f"Original size: {human(original_size)} ({original_size} bytes)\n")
        print("Testing compressors...")
        print("-" * 72)
        jobs: list[tuple[str, str, Compressor, list[Optional[int]]]] = [
            ("gzip", ".gz", compress_gzip, [1, 6, 9]),
            ("bz2", ".bz2", compress_bz2, [1, 5, 9]),
            ("xz", ".xz", compress_lzma, [0, 3, 6, 9]),
            ("zip", ".zip", compress_zip, [1, 6, 9]),
        ]
        if brotli is not None:
            jobs.append(("brotli", ".br", compress_brotli, [1, 4, 7, 9, 11]))
        if zstd is not None:
            jobs.append(("zstandard", ".zst", compress_zstd, [1, 3, 9, 15, 19, 22]))
        if lz4frame is not None:
            jobs.append(("lz4", ".lz4", compress_lz4, [0, 3, 9, 16]))
        if py7zr is not None:
            jobs.append(("7z", ".7z", compress_7z, [1, 5, 9]))
        if pylzma is not None:
            jobs.append(("pylzma", ".lzma", compress_pylzma, [1, 5, 9]))
        if cramjam is not None:
            jobs.append(("snappy", ".snappy", compress_snappy, [None]))
        if zopfli_gzip is not None:
            jobs.append(("zopfli.gz", ".gz", compress_zopfli_gzip, [5, 15, 30]))
        if pyppmd is not None:
            jobs.append(("ppmd", ".ppmd", compress_ppmd, [4, 6, 8, 16]))
        if pyzstd is not None:
            jobs.append(("pyzstd", ".zst", compress_pyzstd, [1, 3, 9, 15, 19, 22]))
        if zipfile_deflate64 is not None:
            jobs.append(("zip-deflate64", ".zip", compress_zip_deflate64, [None]))
        if pyzipper is not None:
            jobs.append(("zip-lzma", ".zip", compress_zip_lzma, [None]))
            jobs.append(("zip-bzip2", ".zip", compress_zip_bzip2, [None]))
        results: list[tuple[str, Optional[int], int, float, float, Path]] = []
        for name, ext, func, levels in jobs:
            for level in levels:
                tag = "" if level is None else f" level={level}"
                dst = tmpdir / f"{name}_{level if level is not None else 'default'}{ext}"
                try:
                    t0 = time.perf_counter()
                    func(src_file, dst, level)
                    dt = time.perf_counter() - t0
                    size = dst.stat().st_size
                    ratio = size / original_size if original_size else 0.0
                    results.append((name, level, size, ratio, dt, dst))
                    print(f"  {name:<14}{tag:<12} -> {human(size):>12}  ratio={ratio:.4f}  time={dt:.2f}s")
                except Exception as e:
                    print(f"  {name:<14}{tag:<12} -> FAILED ({e.__class__.__name__}: {e})")
        if not results:
            print("\nNo compressor succeeded.")
            return 2
        results.sort(key=lambda r: r[2])
        print("\n" + "=" * 72)
        print("BEST 3 METHODS (smallest first)")
        print("=" * 72)
        for rank, (name, level, size, ratio, dt, _) in enumerate(results[:3], 1):
            tag = "" if level is None else f" (level={level})"
            saved = 100 * (1 - ratio)
            print(f"  #{rank}: {name}{tag}")
            print(f"       size  : {human(size)} ({size} bytes)")
            print(f"       ratio : {ratio:.4f}  (saved {saved:.2f}%)")
            print(f"       time  : {dt:.2f}s")
        best = results[0]
        best_path = best[5]
        dest = Path.cwd() / best_path.name
        i = 1
        while dest.exists():
            dest = Path.cwd() / f"{best_path.stem}_{i}{best_path.suffix}"
            i += 1
        shutil.copy2(best_path, dest)
        print(f"\nBest compressed file copied to: {dest}")
        print("Temp directory will now be removed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
