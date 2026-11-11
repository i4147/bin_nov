#!/data/data/com.termux/files/usr/bin/env python
"""Unified multi-codec compression toolkit."""

import argparse
import bz2
from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass
import gzip
import hashlib
import lzma
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import time
from typing import TYPE_CHECKING, Optional
import zipfile
import zlib

from dh import MAX_WORKERS
from loguru import logger


try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import brotli
except ImportError:
    brotli = None
try:
    import lz4.frame as lz4frame
except ImportError:
    lz4frame = None
try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import blosc2
except ImportError:
    blosc2 = None
try:
    import cramjam as cj
except ImportError:
    cj = None
log = logger


@dataclass(frozen=True, slots=True)
class Codec:
    name: str
    ext: str
    compress: Callable[[bytes, int], bytes]
    decompress: Optional[Callable[[bytes], bytes]]
    min_level: int = 1
    max_level: int = 9
    default_level: int = 9


def _lzma_c(d: bytes, l: int) -> bytes:
    preset = l | lzma.PRESET_EXTREME if l >= 9 else l
    return lzma.compress(d, preset=preset, format=lzma.FORMAT_XZ)


def _sevenz_c(d: bytes, l: int, name: str = "data") -> bytes:
    if py7zr is None:
        msg = "py7zr not installed"
        raise RuntimeError(msg)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / name
        src.write_bytes(d)
        out = Path(td) / "a.7z"
        with py7zr.SevenZipFile(out, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": l}]) as z:
            z.write(src, name)
        return out.read_bytes()


def _zip_c(d: bytes, l: int, name: str = "data") -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=l) as zf:
        zf.writestr(name, d)
    return buf.getvalue()


def _zip_d(d: bytes) -> bytes:
    import io

    with zipfile.ZipFile(io.BytesIO(d), "r") as zf:
        return zf.read(zf.namelist()[0])


def _build_codecs() -> dict[str, Codec]:
    cs: dict[str, Codec] = {
        "gzip": Codec(
            "gzip",
            ".gz",
            lambda d, l: gzip.compress(d, compresslevel=l),
            gzip.decompress,
            1,
            9,
            9,
        ),
        "bz2": Codec(
            "bz2",
            ".bz2",
            lambda d, l: bz2.compress(d, compresslevel=l),
            bz2.decompress,
            1,
            9,
            9,
        ),
        "xz": Codec("xz", ".xz", _lzma_c, lzma.decompress, 1, 9, 9),
        "zlib": Codec("zlib", ".zlib", zlib.compress, zlib.decompress, 1, 9, 9),
        "zip": Codec("zip", ".zip", _zip_c, _zip_d, 1, 9, 9),
    }
    if zstd:
        cs["zstd"] = Codec(
            "zstd",
            ".zst",
            lambda d, l: zstd.ZstdCompressor(level=l).compress(d),
            lambda d: zstd.ZstdDecompressor().decompress(d),
            1,
            22,
            21,
        )
    if brotli:
        cs["brotli"] = Codec(
            "brotli",
            ".br",
            lambda d, l: brotli.compress(d, quality=l),
            brotli.decompress,
            0,
            11,
            11,
        )
    if lz4frame:
        cs["lz4"] = Codec(
            "lz4",
            ".lz4",
            lambda d, l: lz4frame.compress(d, compression_level=l),
            lz4frame.decompress,
            0,
            16,
            16,
        )
    if blosc2:
        cs["blosc2"] = Codec(
            "blosc2",
            ".blosc2",
            lambda d, l: blosc2.compress(d, codec=blosc2.Codec.zstd, clevel=l),
            blosc2.decompress,
            1,
            9,
            9,
        )
    if py7zr:
        cs["7z"] = Codec("7z", ".7z", _sevenz_c, None, 1, 9, 9)
    if cj:
        cs["snappy"] = Codec(
            "snappy",
            ".sz",
            lambda d, l: bytes(cj.snappy.compress(d)),
            lambda d: bytes(cj.snappy.decompress(d)),
            0,
            0,
            0,
        )
        cs["deflate"] = Codec(
            "deflate",
            ".deflate",
            lambda d, l: bytes(cj.deflate.compress(d, level=l)),
            lambda d: bytes(cj.deflate.decompress(d)),
            1,
            9,
            9,
        )
    return cs


CODECS: dict[str, Codec] = _build_codecs()
ARCHIVE_SUFFIXES: frozenset[str] = frozenset(
    [".tar"]
    + [c.ext for c in CODECS.values()]
    + [
        ".tar.gz",
        ".tar.bz2",
        ".tar.xz",
        ".tar.zst",
        ".tar.br",
        ".tar.7z",
        ".tar.lz4",
        ".zip",
        ".rar",
        ".zstd",
        ".lzma",
    ]
)


def human(n: int) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def is_archive(p: Path) -> bool:
    n = p.name.lower()
    return any(n.endswith(s) for s in ARCHIVE_SUFFIXES)


def tar_bytes(src: Path) -> tuple[bytes, str]:
    arcname = f"{src.name}.tar"
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tpath = Path(tmp.name)
    try:
        with tarfile.open(tpath, "w") as tf:
            tf.add(src, arcname=src.name)
        return tpath.read_bytes(), arcname
    finally:
        tpath.unlink(missing_ok=True)


def safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if dest != target and dest not in target.parents:
            continue
        tf.extract(m, path=str(dest), filter="data")


def prepare_input(path: Path) -> tuple[bytes, str]:
    if path.is_file():
        return path.read_bytes(), path.name
    if path.is_dir():
        return tar_bytes(path)
    msg = f"Not a file or directory: {path}"
    raise ValueError(msg)


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def _mp_chunk_worker(args: tuple[str, bytes]) -> bytes:
    name, chunk = args
    return CODECS[name].compress(chunk, CODECS[name].default_level)


def cmd_bench(args: argparse.Namespace) -> int:
    src = Path(args.path).expanduser()
    if not src.exists():
        log.error("Path does not exist: {}", src)
        return 1
    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload, base = prepare_input(src)
    total = len(payload)
    print(f"Input: {src}  ({human(total)})")
    if src.is_file():
        with contextlib.suppress(Exception):
            print(f"SHA256(input) = {sha256_file(src)}")
    print()
    names: Sequence[str] = args.algos or list(CODECS.keys())
    level_override: Optional[int] = args.level
    results: list[tuple[str, int, float, float, Path]] = []
    print(f"{'codec':<10} {'size':>14} {'ratio':>8} {'time(s)':>9}")
    print("-" * 45)
    for name in names:
        codec = CODECS.get(name)
        if codec is None:
            print(f"✗ {name:<8} | not available")
            continue
        level = level_override if level_override is not None else codec.default_level
        try:
            t0 = time.perf_counter()
            if args.mp_chunks and name in CODECS:
                chunk_size = args.chunk_size * 1024 * 1024
                chunks = [payload[i : i + chunk_size] for i in range(0, total, chunk_size)]
                with mp.Pool(processes=MAX_WORKERS or mp.cpu_count()) as pool:
                    parts = pool.map(_mp_chunk_worker, [(name, c) for c in chunks])
                out = b"".join(parts)
            else:
                out = codec.compress(payload, level)
            dt = time.perf_counter() - t0
            out_path = out_dir / f"{base}{codec.ext}"
            out_path.write_bytes(out)
            results.append((name, len(out), len(out) / total if total else 0.0, dt, out_path))
            print(f"✓ {name:<8} | {len(out):>12,} | {len(out) / total if total else 0:.4f} | {dt:>7.3f}")
        except Exception as e:
            print(f"✗ {name:<8} | Error: {e}")
    if not results:
        print("No successful compressions.")
        return 1
    results.sort(key=lambda r: r[1])
    print()
    print("Top results:")
    for i, (n, sz, ratio, dt, p) in enumerate(results[:3], 1):
        print(
            f"  {i}. {n:<10} size={sz:>12,} ratio={ratio:.4f} "
            f"saved={total - sz:>12,} B ({100 * (1 - ratio):.1f}%) time={dt:.3f}s"
        )
    best = results[0]
    print(f"\nKeeping best: {best[0]} -> {best[4]}")
    if not args.keep_all:
        for n, _, _, _, p in results[1:]:
            with contextlib.suppress(OSError):
                p.unlink()
            print(f"  deleted: {p.name}")
    return 0


def _archive_one_file(job: tuple[str, str, list[str], bool]) -> Optional[str]:
    fpath, out_dir, codecs, _keep_all = job
    p = Path(fpath)
    try:
        data = p.read_bytes()
    except OSError as e:
        log.error("Cannot read {}: {}", p, e)
        return None
    total = len(data)
    best: Optional[tuple[str, str, int, bytes]] = None
    for name in codecs:
        codec = CODECS.get(name)
        if codec is None:
            continue
        for level in range(codec.min_level, codec.max_level + 1):
            try:
                out = codec.compress(data, level)
            except Exception:
                continue
            if best is None or len(out) < len(best[3]):
                best = (name, codec.ext, level, out)
    if best is None:
        return None
    name, ext, level, blob = best
    dst = Path(out_dir) / f"{p.name}{ext}"
    dst.write_bytes(blob)
    log.info("  {} → {} ({}, level={}, {})", p.name, dst.name, name, level, human(len(blob)))
    return str(dst)


def cmd_archive(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if not target.exists():
        log.error("Path does not exist: {}", target)
        return 1
    codecs = args.algos or list(CODECS.keys())
    workers = MAX_WORKERS or (mp.cpu_count() or 1)
    if target.is_file():
        out_dir = Path(args.output).expanduser() if args.output else target.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        _archive_one_file((str(target), str(out_dir), codecs, args.keep_all))
        return 0
    if target.is_dir():
        files = [p for p in target.rglob("*") if p.is_file() and not is_archive(p)]
        if not files:
            log.info("No compressible files in {}", target)
            return 0
        out_dir = Path(args.output).expanduser() if args.output else target
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = [(str(f), str(out_dir), codecs, args.keep_all) for f in files]
        log.info("Found {} file(s); using {} worker(s)", len(files), min(workers, len(files)))
        with mp.Pool(processes=min(workers, len(files))) as pool:
            outcomes = pool.map(_archive_one_file, jobs)
        ok = sum(1 for o in outcomes if o)
        log.info("Done: {}/{} compressed", ok, len(files))
        return 0
    log.error("Not a file or directory: {}", target)
    return 1


def _compress_one_file(
    src: Path,
    codec: Codec,
    level: int,
    keep: bool,
    verify: bool,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    try:
        data = src.read_bytes()
    except OSError as e:
        log.error("read {}: {}", src, e)
        return None
    try:
        blob = codec.compress(data, level)
    except Exception as e:
        log.error("compress {} with {}: {}", src, codec.name, e)
        return None
    if verify and codec.decompress is not None:
        try:
            if codec.decompress(blob) != data:
                log.error("round-trip mismatch for {} — refusing to write", src)
                return None
        except Exception as e:
            log.error("verify {}: {}", src, e)
            return None
    dst = (out_dir or src.parent) / f"{src.name}{codec.ext}"
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, dst)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        log.error("write {}: {}", dst, e)
        return None
    log.info("✓ {} -> {}  [{} -> {}]", src, dst.name, human(len(data)), human(len(blob)))
    if not keep:
        with contextlib.suppress(OSError):
            src.unlink()
    return dst


def _compress_dir_as_tar(
    src: Path,
    codec: Codec,
    level: int,
    keep: bool,
    verify: bool,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    data, _arc = tar_bytes(src)
    try:
        blob = codec.compress(data, level)
    except Exception as e:
        log.error("compress {}: {}", src, e)
        return None
    dst = (out_dir or src.parent) / f"{src.name}{codec.ext}"
    dst.write_bytes(blob)
    log.info("✓ {} -> {}  [{} -> {}]", src, dst.name, human(len(data)), human(len(blob)))
    if not keep:
        shutil.rmtree(src, ignore_errors=True)
    return dst


def cmd_compress(args: argparse.Namespace) -> int:
    codec = CODECS.get(args.algo)
    if codec is None:
        log.error(
            "Unknown or unavailable codec: {} (available: {})",
            args.algo,
            ", ".join(sorted(CODECS)),
        )
        return 1
    level = args.level if args.level is not None else codec.default_level
    src = Path(args.path).expanduser()
    if not src.exists():
        log.error("Path does not exist: {}", src)
        return 1
    out_dir = Path(args.output).expanduser() if args.output else None
    if src.is_file():
        return 0 if _compress_one_file(src, codec, level, args.keep, args.verify, out_dir) else 1
    if args.recursive:
        targets = [p for p in sorted(src.rglob("*")) if p.is_file() and not is_archive(p)]
        if not targets:
            log.info("No compressible files in {}", src)
            return 0
        jobs = [
            (
                str(t),
                codec.name,
                level,
                args.keep,
                args.verify,
                str(out_dir) if out_dir else None,
            )
            for t in targets
        ]
        workers = MAX_WORKERS or (mp.cpu_count() or 1)
        with mp.Pool(processes=min(workers, len(targets))) as pool:
            pool.map(_compress_worker, jobs)
        return 0
    return 0 if _compress_dir_as_tar(src, codec, level, args.keep, args.verify, out_dir) else 1


def _compress_worker(job: tuple[str, str, int, bool, bool, Optional[str]]):
    p, cname, lvl, keep, verify, out = job
    return _compress_one_file(Path(p), CODECS[cname], lvl, keep, verify, Path(out) if out else None)


def _guess_codec_from_name(name: str) -> Optional[Codec]:
    n = name.lower()
    for c in sorted(CODECS.values(), key=lambda c: len(c.ext), reverse=True):
        if n.endswith(c.ext):
            return c
    return None


def _decompress_one_file(src: Path, codec: Codec, keep: bool, out_dir: Optional[Path] = None) -> Optional[Path]:
    if codec.decompress is None:
        log.error("No decompressor implemented for codec {}", codec.name)
        return None
    base = src.name.removesuffix(codec.ext)
    dst = (out_dir or src.parent) / base
    try:
        blob = src.read_bytes()
    except OSError as e:
        log.error("read {}: {}", src, e)
        return None
    if codec.name == "7z" and py7zr is not None:
        try:
            with tempfile.TemporaryDirectory() as td:
                with py7zr.SevenZipFile(src, "r") as z:
                    z.extractall(path=td)
                entries = list(Path(td).iterdir())
                if len(entries) == 1:
                    shutil.move(str(entries[0]), str(dst))
                else:
                    dst.mkdir(parents=True, exist_ok=True)
                    for e in entries:
                        shutil.move(str(e), str(dst / e.name))
        except Exception as e:
            log.error("7z extract {}: {}", src, e)
            return None
    else:
        try:
            out = codec.decompress(blob)
        except Exception as e:
            log.error("decompress {}: {}", src, e)
            return None
        dst.write_bytes(out)
        if dst.name.endswith(".tar"):
            target = dst.with_suffix("")
            try:
                with tarfile.open(dst, "r:") as tf:
                    target.mkdir(parents=True, exist_ok=True)
                    safe_extract(tf, target)
                dst.unlink()
                dst = target
            except tarfile.TarError:
                pass
    log.info("✓ {} -> {}", src, dst)
    if not keep:
        with contextlib.suppress(OSError):
            src.unlink()
    return dst


def _decompress_worker(job: tuple[str, str, bool, Optional[str]]):
    p, cname, keep, out = job
    return _decompress_one_file(Path(p), CODECS[cname], keep, Path(out) if out else None)


def cmd_decompress(args: argparse.Namespace) -> int:
    src = Path(args.path).expanduser()
    if not src.exists():
        log.error("Path does not exist: {}", src)
        return 1
    out_dir = Path(args.output).expanduser() if args.output else None
    explicit = CODECS.get(args.algo) if args.algo else None
    if args.algo and explicit is None:
        log.error("Unknown codec: {}", args.algo)
        return 1
    if src.is_file():
        if args.algo == "zlib":
            dst = (out_dir or src.parent) / (src.name + ".decompressed")
            try:
                with src.open("rb") as fin, dst.open("wb") as fout:
                    d = zlib.decompressobj()
                    for chunk in iter(lambda: fin.read(16384), b""):
                        fout.write(d.decompress(chunk))
                    fout.write(d.flush())
            except zlib.error as e:
                log.error("zlib stream error: {}", e)
                return 2
            log.info("✓ {} -> {}", src, dst)
            if not args.keep:
                src.unlink()
            return 0
        codec = explicit or _guess_codec_from_name(src.name)
        if codec is None:
            log.error("Cannot infer codec from {} — pass --algo", src.name)
            return 1
        return 0 if _decompress_one_file(src, codec, args.keep, out_dir) else 1
    if args.recursive:
        candidates = [p for p in sorted(src.rglob("*")) if p.is_file() and is_archive(p)]
    else:
        candidates = [p for p in sorted(src.iterdir()) if p.is_file() and is_archive(p)]
    if not candidates:
        log.info("No archives found under {}", src)
        return 0
    jobs = []
    for c in candidates:
        codec = explicit or _guess_codec_from_name(c.name)
        if codec is None:
            log.warning("skip (unknown suffix): {}", c)
            continue
        jobs.append((str(c), codec.name, args.keep, str(out_dir) if out_dir else None))
    if not jobs:
        return 0
    workers = MAX_WORKERS or (mp.cpu_count() or 1)
    with mp.Pool(processes=min(workers, len(jobs))) as pool:
        pool.map(_decompress_worker, jobs)
    return 0


def _subdir_compress(job: tuple[str, str, int]) -> bool:
    dpath, cname, level = job
    src = Path(dpath)
    codec = CODECS[cname]
    try:
        data, _arcname = tar_bytes(src)
        blob = codec.compress(data, level)
    except Exception as e:
        log.error("subdir compress {}: {}", src, e)
        return False
    dst = src.parent / f"{src.name}.tar{codec.ext}"
    dst.write_bytes(blob)
    log.info("✓ {} -> {}  [{} -> {}]", src, dst.name, human(len(data)), human(len(blob)))
    shutil.rmtree(src, ignore_errors=True)
    return True


def _subdir_decompress(job: tuple[str, bool]) -> bool:
    p, keep = job
    src = Path(p)
    codec = _guess_codec_from_name(src.name)
    if codec is None:
        return False
    name = src.name
    if name.endswith(f".tar{codec.ext}"):
        base = name[: -len(f".tar{codec.ext}")]
    else:
        base = name[: -len(codec.ext)]
    try:
        data = src.read_bytes()
        out = codec.decompress(data) if codec.decompress else None
        if out is None:
            return False
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
            tpath = Path(tf.name)
        tpath.write_bytes(out)
        try:
            with tarfile.open(tpath, "r:") as tar:
                safe_extract(tar, src.parent)
        finally:
            tpath.unlink(missing_ok=True)
    except Exception as e:
        log.error("subdir decompress {}: {}", src, e)
        return False
    if not keep:
        src.unlink(missing_ok=True)
    log.info("✓ {} -> {}", src, base)
    return True


def cmd_subdirs(args: argparse.Namespace) -> int:
    codec = CODECS.get(args.algo)
    if codec is None:
        log.error("Unknown codec: {}", args.algo)
        return 1
    level = args.level if args.level is not None else codec.default_level
    paths = [Path(p).expanduser() for p in (args.paths or ["."])]
    workers = MAX_WORKERS or 8
    if args.compress:
        dirs = [d for p in paths if p.is_dir() for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not dirs:
            print("No subdirectories found to compress.")
            return 0
        print(f"Found {len(dirs)} directories — codec={codec.name} level={level}")
        jobs = [(str(d), codec.name, level) for d in dirs]
        with mp.Pool(processes=min(workers, len(dirs))) as pool:
            results = pool.map(_subdir_compress, jobs)
        print(f"Compressed {sum(results)}/{len(dirs)}")
        return 0
    archives = [
        a for p in paths if p.is_dir() for a in p.iterdir() if a.is_file() and a.name.endswith(f".tar{codec.ext}")
    ]
    if not archives:
        print("No matching archives found.")
        return 0
    print(f"Found {len(archives)} archives — codec={codec.name}")
    jobs = [(str(a), args.keep) for a in archives]
    with mp.Pool(processes=min(workers, len(archives))) as pool:
        results = pool.map(_subdir_decompress, jobs)
    print(f"Decompressed {sum(results)}/{len(archives)}")
    return 0


def _common_workers(p: argparse.ArgumentParser) -> None:
    pass


def _common_output(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="output directory (default: alongside input)",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="compressor.py",
        description="Unified multi-codec compression toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    ap.add_argument("-q", "--quiet", action="store_true", help="quiet logging")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bench", help="try every codec at its best level; keep best")
    p.add_argument("path")
    _common_output(p)
    p.add_argument("--algos", nargs="+", default=None, help="restrict to these codec names")
    p.add_argument("--level", type=int, default=None, help="override the codec's default level")
    p.add_argument("--keep-all", action="store_true", help="do not delete losing outputs")
    p.add_argument(
        "--mp-chunks",
        action="store_true",
        help="parallel chunked compression (best_compression.py mode)",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=4,
        help="MiB per chunk for --mp-chunks (default: 4)",
    )
    _common_workers(p)
    p.set_defaults(func=cmd_bench)
    p = sub.add_parser("archive", help="recursively best-codec every non-archive file")
    p.add_argument("path")
    _common_output(p)
    p.add_argument("--algos", nargs="+", default=None, help="restrict to these codec names")
    p.add_argument("--keep-all", action="store_true", help="(reserved for parity)")
    _common_workers(p)
    p.set_defaults(func=cmd_archive)
    p = sub.add_parser("compress", help="compress a file or directory with ONE codec")
    p.add_argument("path")
    p.add_argument("--algo", "-a", required=True, choices=sorted(CODECS), help="codec to use")
    p.add_argument(
        "--level",
        "-l",
        type=int,
        default=None,
        help="compression level (codec-specific; default = codec's best)",
    )
    p.add_argument("--keep", action="store_true", help="keep the original after compressing")
    p.add_argument(
        "--recursive",
        action="store_true",
        help="compress each file individually (cramer.py mode); "
        "otherwise the directory is tarred as one unit (xxr.py mode)",
    )
    p.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="skip round-trip verification",
    )
    p.set_defaults(verify=True)
    _common_output(p)
    _common_workers(p)
    p.set_defaults(func=cmd_compress)
    p = sub.add_parser("decompress", help="decompress a file or all archives under a directory")
    p.add_argument("path")
    p.add_argument(
        "--algo",
        "-a",
        default=None,
        choices=sorted(CODECS) + ["zlib"],
        help="codec (default: inferred from filename)",
    )
    p.add_argument("--keep", action="store_true", help="keep the archive after decompressing")
    p.add_argument("--recursive", action="store_true", help="recurse into subdirectories")
    _common_output(p)
    _common_workers(p)
    p.set_defaults(func=cmd_decompress)
    p = sub.add_parser("subdirs", help="compress/decompress each direct subdirectory")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("-c", "--compress", action="store_true", help="compress subdirectories")
    mode.add_argument("-d", "--decompress", action="store_true", help="decompress .tar.<ext> archives")
    p.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="parent directories (default: current dir)",
    )
    p.add_argument(
        "--algo",
        "-a",
        default="zstd",
        choices=sorted(CODECS),
        help="codec (default: zstd)",
    )
    p.add_argument("--level", "-l", type=int, default=None, help="compression level")
    p.add_argument(
        "--keep",
        action="store_true",
        help="(decompress only) keep archive after extraction",
    )
    _common_workers(p)
    p.set_defaults(func=cmd_subdirs)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)
    level = "DEBUG" if ns.verbose else ("WARNING" if ns.quiet else "INFO")
    try:
        return int(ns.func(ns) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
