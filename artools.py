#!/data/data/com.termux/files/usr/bin/env python
"""Archive and compression converter for tar, zip, wheel and brotli files.
Subcommands: ``tar-codec`` re-encodes the ``*.tar.<codec>`` files (gz, xz, bz2, zst, br, lz4, 7z), ``archive-convert`` converts between the tar/zip/whl formats, ``br2zst`` turns ``*.json.br`` into ``*.json.zst`` and ``whl-txz`` converts wheels to ``.tar.xz`` and back.
``--remove-original`` and ``--level`` control the output.
Usage: artools {tar-codec,archive-convert,br2zst,whl-txz} [options] Positional arguments: target Target codec, e.g.
xz, zst, br, 7z.
inputs ...
Files or directories (default: cwd entries).
paths ...
Files or directories (default: current directory).
Subcommands: tar-codec Change the codec of *.tar.<codec> files (recursive).
archive-convert Convert archives between tar/zip/whl formats.
br2zst Recursively convert *.json.br -> *.json.zst.
whl-txz Bidirectional .whl <-> .tar.xz converter.
Options: --root Directory to scan recursively (default: cwd).
--level -t, --to Target format, e.g.
.tar.xz or .zip.
-r, --recursive Search directories recursively.
--remove-original Delete source file after successful conversion.
--to Restrict direction when scanning directories.
-v, --verbose -q, --quiet --legacy-percent Reproduce the original buggy '*40' percent display.
Examples: $ artools tar-codec xz --root dist/ Command-line script: run ``python artools.py`` or the ``artools`` command.
The module is importable as well."""

import argparse
import bz2
from collections.abc import Iterable, Iterator
import contextlib
from datetime import datetime
import gzip
import io
import lzma
from multiprocessing import Pool
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Optional
import zipfile

import brotli
import cramjam
from dh import MAX_WORKERS
from loguru import logger
import lz4.frame
import py7zr
import zstandard as zstd

CHUNK: int = 1024 * 1024
"""Read/write block size for streaming codecs (matches originals)."""
"""Default worker count (matches the originals' `c` / `q4`)."""
DEFAULT_LEVEL: int = 9
"""Default compression level for codecs that accept one."""
TAR_CODECS: frozenset[str] = frozenset({"gz", "zst", "xz", "bz2", "lz4", "br", "7z"})
TAR_FORMATS: frozenset[str] = frozenset({
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tar.br",
    ".tar.lz4",
    ".tar.7z",
    ".tar.sz",
})
ZIP_FORMATS: frozenset[str] = frozenset({".zip", ".whl"})
ARCHIVE_FORMATS: frozenset[str] = TAR_FORMATS | ZIP_FORMATS
_ARCHIVE_SUFFIXES: tuple[str, ...] = tuple(sorted(ARCHIVE_FORMATS, key=len, reverse=True))
WHEEL_SCRIPT_SUFFIXES: tuple[str, ...] = (".sh", ".py", ".exe")
TAR_DEFAULT_MODE: int = 0o644
TAR_SCRIPT_MODE: int = 0o755
TAR_MODE_MASK: int = 0o7777
TAR_UID: int = 0
TAR_GID: int = 0
TAR_UNAME: str = "root"
TAR_GNAME: str = "root"


def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:6.2f} {unit}"
        n /= 1024.0
    return f"{n:6.2f} PiB"


def dir_total_size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def get_files(root: Path, exts: Iterable[str]) -> list[Path]:
    exts = tuple(exts)
    return sorted(p for p in root.iterdir() if p.is_file() and any(p.name.endswith(e) for e in exts))


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf, parent = p.stem, p.suffix, p.parent
    i = 1
    while True:
        cand = parent / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
        i += 1


def parallel_map(func: Callable, items: list[Any], workers: int = MAX_WORKERS) -> list[Any]:
    if not items:
        return []
    if workers <= 1:
        return [func(x) for x in items]
    with Pool(processes=workers) as pool:
        return list(pool.map(func, items))


def parallel_imap(func: Callable, items: list[Any], workers: int = MAX_WORKERS) -> list[Any]:
    if not items:
        return []
    if workers <= 1:
        return [func(x) for x in items]
    with Pool(processes=workers) as pool:
        return list(pool.imap_unordered(func, items))


def _copy_stream(src: BinaryIO, dst: BinaryIO) -> int:
    total = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            break
        dst.write(chunk)
        total += len(chunk)
    return total


@contextlib.contextmanager
def open_decompressed(src: Path, codec: str) -> Iterator[BinaryIO]:
    if codec == "gz":
        with gzip.open(src, "rb") as f:
            yield f
    elif codec == "bz2":
        with bz2.open(src, "rb") as f:
            yield f
    elif codec == "xz":
        with lzma.open(src, "rb", format=lzma.FORMAT_XZ) as f:
            yield f
    elif codec == "zst":
        dec = zstd.ZstdDecompressor()
        with src.open("rb") as raw, dec.stream_reader(raw) as f:
            yield f
    elif codec == "lz4":
        with lz4.frame.open(src, "rb") as f:
            yield f
    elif codec == "br":
        yield io.BytesIO(brotli.decompress(src.read_bytes()))
    elif codec == "sz":
        yield io.BytesIO(bytes(cramjam.snappy.decompress(src.read_bytes())))
    else:
        msg = f"Unsupported source codec: {codec}"
        raise ValueError(msg)


@contextlib.contextmanager
def open_compressor(dst: Path, codec: str, level: int = DEFAULT_LEVEL) -> Iterator[BinaryIO]:
    if codec == "gz":
        with gzip.open(dst, "wb", compresslevel=level) as f:
            yield f
    elif codec == "bz2":
        with bz2.open(dst, "wb", compresslevel=level) as f:
            yield f
    elif codec == "xz":
        with lzma.open(dst, "wb", format=lzma.FORMAT_XZ, preset=level) as f:
            yield f
    elif codec == "zst":
        comp = zstd.ZstdCompressor(level=level)
        with dst.open("wb") as raw, comp.stream_writer(raw) as f:
            yield f
    elif codec == "lz4":
        with lz4.frame.open(dst, "wb") as f:
            yield f
    elif codec == "br":
        buf = io.BytesIO()
        yield buf
        dst.write_bytes(brotli.compress(buf.getvalue(), quality=11))
    elif codec == "sz":
        buf = io.BytesIO()
        yield buf
        dst.write_bytes(bytes(cramjam.snappy.compress(buf.getvalue())))
    else:
        msg = f"Unsupported target codec: {codec}"
        raise ValueError(msg)


def _parse_tar_codec_name(p: Path) -> Optional[tuple[str, str]]:
    parts = p.name.split(".")
    if len(parts) < 3 or parts[-2] != "tar":
        return None
    codec = parts[-1].lower()
    stem = ".".join(parts[:-2]) or "archive"
    return stem, codec


def _extract_7z_to_tar(src: Path, dst: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tar7z_dec_"))
    try:
        with py7zr.SevenZipFile(src, mode="r") as z:
            z.extractall(path=tmp)
        inner = next(tmp.glob("*.tar"), None)
        if inner is None:
            files = [p for p in tmp.rglob("*") if p.is_file()]
            if not files:
                msg = "No files extracted from .tar.7z"
                raise RuntimeError(msg)
            inner = files[0]
        inner.replace(dst)
    finally:
        for p in tmp.rglob("*"):
            with contextlib.suppress(Exception):
                if p.is_file():
                    p.unlink()
        with contextlib.suppress(Exception):
            tmp.rmdir()


def _tar_codec_job(job: tuple[str, str, int]) -> tuple[str, bool, str]:
    src_str, target, level = job
    src = Path(src_str)
    parsed = _parse_tar_codec_name(src)
    if parsed is None:
        return src.name, False, "Not a *.tar.<codec> file"
    stem, src_codec = parsed
    if src_codec == target:
        return src.name, True, "Skipped (already target codec)"
    dst = src.with_name(f"{stem}.tar.{target}")
    if dst.exists():
        return src.name, True, f"Skipped (exists): {dst.name}"
    tmp = src.with_name(f".__tmp_tar_conv_{os.getpid()}_{stem}.tar")
    try:
        if src_codec == "7z":
            _extract_7z_to_tar(src, tmp)
        else:
            with open_decompressed(src, src_codec) as i, tmp.open("wb") as o:
                _copy_stream(i, o)
        if target == "7z":
            with py7zr.SevenZipFile(dst, mode="w") as z:
                z.write(tmp, arcname=tmp.name)
        else:
            with open_compressor(dst, target, level) as o, tmp.open("rb") as i:
                _copy_stream(i, o)
        src.unlink()
        return src.name, True, f"converted -> {dst.name} (removed original)"
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return src.name, False, f"error: {e}"
    finally:
        with contextlib.suppress(Exception):
            if tmp.exists():
                tmp.unlink()


def run_tar_codec(args: argparse.Namespace) -> int:
    root: Path = args.root
    target: str = args.target
    files: list[Path] = []
    for p in root.rglob("*.tar.*"):
        if not p.is_file():
            continue
        parsed = _parse_tar_codec_name(p)
        if parsed and parsed[1] in TAR_CODECS:
            files.append(p)
    files.sort()
    if not files:
        print("No *.tar.<codec> files found recursively in current directory.")
        return 0
    initial = dir_total_size(root)
    results = parallel_imap(_tar_codec_job, [(str(p), target, args.level) for p in files], MAX_WORKERS)
    final = dir_total_size(root)
    delta = final - initial
    ok = sum(1 for _, o, _ in results if o)
    print(f"Converted inputs: {len(files)}; OK: {ok}; Failed/Skipped: {len(files) - ok}")
    for name, o, msg in sorted(results, key=lambda x: x[0]):
        print(f"[{'OK' if o else 'FAIL'}] {name}: {msg}")
    print(f"Disk usage initial: {fmt_size(initial)}")
    print(f"Disk usage final:   {fmt_size(final)}")
    if delta < 0:
        print(f"Saved: {fmt_size(-delta)}")
    elif delta > 0:
        print(f"Extra used: {fmt_size(delta)}")
    else:
        print("No disk usage change (by summed file sizes).")
    return 0


def detect_archive_ext(p: Path) -> Optional[str]:
    name = p.name.lower()
    for ext in _ARCHIVE_SUFFIXES:
        if name.endswith(ext):
            return ext
    return None


def _iter_archive_members(src: Path, ext: str) -> Iterator[tuple[str, bytes]]:
    if ext in ZIP_FORMATS:
        with zipfile.ZipFile(src, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                with zf.open(info) as f:
                    yield info.filename, f.read()
        return
    if ext in TAR_FORMATS:
        codec = ext[5:] if ext != ".tar" else "tar"
        if codec == "tar":
            with src.open("rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tf:
                for m in tf:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    yield m.name, f.read()
        elif codec == "7z":
            with py7zr.SevenZipFile(str(src), mode="r") as z:
                payload = z.readall()
                for bio in payload.values():
                    data = bio.read()
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tf:
                        for m in tf:
                            if not m.isfile():
                                continue
                            f = tf.extractfile(m)
                            if f is None:
                                continue
                            yield m.name, f.read()
                    return
            msg = f"empty 7z archive: {src}"
            raise ValueError(msg)
        else:
            with (
                open_decompressed(src, codec) as stream,
                tarfile.open(fileobj=stream, mode="r|") as tf,
            ):
                for m in tf:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    yield m.name, f.read()
        return
    msg = f"unsupported input extension: {ext}"
    raise ValueError(msg)


def _write_archive(
    members: Iterable[tuple[str, bytes]],
    dst: Path,
    ext: str,
    level: int = DEFAULT_LEVEL,
) -> int:
    if ext in ZIP_FORMATS:
        total = 0
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members:
                zf.writestr(name, data)
                total += len(data)
        return total
    if ext not in TAR_FORMATS:
        msg = f"unsupported output extension: {ext}"
        raise ValueError(msg)
    codec = ext[5:] if ext != ".tar" else "tar"
    if codec == "7z":
        tmp_tar: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                tmp_tar = Path(tmp.name)
            with tmp_tar.open("wb") as raw, tarfile.open(fileobj=raw, mode="w|") as tf:
                total = _tar_add_members(tf, members)
            with py7zr.SevenZipFile(str(dst), mode="w") as z:
                z.write(str(tmp_tar), arcname="archive.tar")
            return total
        finally:
            if tmp_tar is not None:
                tmp_tar.unlink(missing_ok=True)
    if codec == "tar":
        with dst.open("wb") as raw, tarfile.open(fileobj=raw, mode="w|") as tf:
            return _tar_add_members(tf, members)
    with (
        open_compressor(dst, codec, level) as stream,
        tarfile.open(fileobj=stream, mode="w|") as tf,
    ):
        return _tar_add_members(tf, members)


def _tar_add_members(tf: tarfile.TarFile, members: Iterable[tuple[str, bytes]]) -> int:
    total = 0
    for name, data in members:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        total += len(data)
    return total


def _archive_convert_job(job: tuple[str, str, int]) -> tuple[str, int, bool, str]:
    src_str, target_ext, level = job
    src = Path(src_str)
    ext = detect_archive_ext(src)
    if ext is None:
        return src_str, 0, False, f"unsupported input format: {src.name}"
    stem = src.name
    for suf in _ARCHIVE_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    dst = src.with_name(stem + target_ext)
    if dst == src:
        return src_str, 0, True, f"skipped (already {target_ext}): {src.name}"
    if dst.exists():
        return src_str, 0, True, f"skipped (exists): {dst.name}"
    try:
        in_size = src.stat().st_size
        _write_archive(_iter_archive_members(src, ext), dst, target_ext, level)
        out_size = dst.stat().st_size
        src.unlink()
        return (
            src_str,
            out_size - in_size,
            True,
            f"converted -> {dst.name}, removed original",
        )
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return src_str, 0, False, f"error: {e}"


def run_archive_convert(args: argparse.Namespace) -> int:
    inputs: list[Path] = []
    if not args.inputs:
        inputs.extend(Path.cwd().iterdir())
    else:
        for a in args.inputs:
            p = Path(a)
            if p.is_dir():
                inputs.extend(p.iterdir())
            else:
                inputs.append(p)
    files: list[Path] = []
    for p in inputs:
        if not p.is_file():
            continue
        if detect_archive_ext(p) is None:
            logger.warning("Skipping unsupported file: {}", p.name)
            continue
        files.append(p)
    if not files:
        logger.warning("No convertible archives found.")
        return 0
    logger.info("Found {} archive(s); converting to {}", len(files), args.to)
    jobs = [(str(p), args.to, args.level) for p in files]
    results = parallel_imap(_archive_convert_job, jobs, MAX_WORKERS)
    ok = sum(1 for _, _, o, _ in results if o)
    delta = sum(r for _, r, _, _ in results)
    logger.info("Summary: total={} ok={} failed/skipped={}", len(files), ok, len(files) - ok)
    for src, _, o, msg in sorted(results, key=lambda x: x[0]):
        logger.info("[{}] {}: {}", "OK" if o else "FAIL", Path(src).name, msg)
    if delta < 0:
        logger.info("Net space saved: {}", fmt_size(-delta))
    elif delta > 0:
        logger.info("Net extra used: {}", fmt_size(delta))
    else:
        logger.info("Net disk usage change: none")
    return 0


def run_br2zst(args: argparse.Namespace) -> int:
    root: Path = args.root
    files = sorted(p for p in root.rglob("*.json.br") if p.is_file())
    if not files:
        print(f"No .json.br files found under {root}")
        return 0
    comp = zstd.ZstdCompressor(level=args.level)
    total_in = total_out = 0
    print(f"Found {len(files)} file(s) under {root}\n")
    print(f"{'file':<60} {'br':>12} {'zst':>12} {'diff':>12}  {'%':>7}")
    print("-" * 108)
    for src in files:
        dst = src.with_suffix("").with_suffix(".json.zst")
        try:
            raw_bytes = src.read_bytes()
            raw = brotli.decompress(raw_bytes)
            zst_bytes = comp.compress(raw)
            dst.write_bytes(zst_bytes)
            src.unlink()
        except Exception as e:
            print(f"ERROR {src}: {e}")
            continue
        in_size = len(raw_bytes)
        out_size = len(zst_bytes)
        diff = out_size - in_size
        pct = diff / in_size * 100 if in_size else 0.0
        total_in += in_size
        total_out += out_size
        try:
            rel = src.relative_to(root)
        except ValueError:
            rel = src
        label = str(rel)
        if len(label) > 58:
            label = "..." + label[-55:]
        print(f"{label:<60} {fmt_size(in_size):>12} {fmt_size(out_size):>12} {fmt_size(diff):>12}  {pct:>6.2f}%")
    print("-" * 108)
    diff = total_out - total_in
    pct = diff / total_in * 100 if total_in else 0.0
    print(
        f"{'TOTAL (' + str(len(files)) + ' files)':<60} {fmt_size(total_in):>12} "
        f"{fmt_size(total_out):>12} {fmt_size(diff):>12}  {pct:>6.2f}%"
    )
    return 0


def _single_transcode(
    src: Path,
    dst_suffix: str,
    src_codec: str,
    dst_codec: str,
    level: int,
    legacy_percent: bool,
) -> tuple[str, bool, str]:
    dst = src.with_suffix(dst_suffix)
    try:
        with open_decompressed(src, src_codec) as f:
            raw = f.read()
        if dst_codec == "xz":
            out = lzma.compress(raw, format=lzma.FORMAT_XZ, preset=level)
        elif dst_codec == "gz":
            out = gzip.compress(raw, compresslevel=level)
        else:
            msg = f"unsupported dst codec: {dst_codec}"
            raise ValueError(msg)
        dst.write_bytes(out)
        if dst.exists() and dst.stat().st_size > 0:
            src_size = src.stat().st_size
            dst_size = dst.stat().st_size
            src.unlink()
            pct = (dst_size / src_size * (40 if legacy_percent else 100)) if src_size else 0
            return (
                str(src),
                True,
                f"Converted to {dst.name} ({src_size} -> {dst_size} bytes, {pct:.1f}%)",
            )
        return (str(src), False, "Output file is empty or missing")
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return (str(src), False, f"Error: {e!s}")


def _report_single_transcode(results: list[tuple[str, bool, str]]) -> int:
    ok = fail = 0
    total_in = total_out = 0
    print("\n" + "=" * 40)
    print("CONVERSION RESULTS")
    print("-" * 40)
    for src, good, msg in results:
        if good:
            ok += 1
            print(f"✓ {msg}")
            if "bytes" in msg:
                with contextlib.suppress(Exception):
                    nums = msg.split("(")[1].split(")")[0].split("->")
                    total_in += int(nums[0].strip().split()[0])
                    total_out += int(nums[1].strip().split()[0])
        else:
            fail += 1
            print(f"✗ {src}: {msg}", file=sys.stderr)
    print("-" * 40)
    print(f"Summary: {ok} successful, {fail} failed")
    print(f"Total files processed: {len(results)}")
    if ok > 0 and total_in > 0:
        pct = (1 - total_out / total_in) * 100
        print(f"Total space saved: {total_in - total_out:,} bytes ({pct:.1f}%)")
        print(f"Original total: {total_in:,} bytes")
        print(f"New total: {total_out:,} bytes")
    if ok > 0:
        print("\nNote: Original files have been removed.")
    return 0 if fail == 0 else 1


def run_gz2xz(args: argparse.Namespace) -> int:
    files = get_files(args.root, [".gz"])
    if not files:
        print("No .gz files found to convert.")
        return 0
    jobs = [(str(p), ".xz", "gz", "xz", args.level, args.legacy_percent) for p in files]
    results = (
        [_single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]) for j in jobs]
        if MAX_WORKERS <= 1
        else parallel_map(
            lambda j: _single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]),
            jobs,
            MAX_WORKERS,
        )
    )
    return _report_single_transcode(results)


def run_xz2gz(args: argparse.Namespace) -> int:
    files = get_files(args.root, [".xz"])
    if not files:
        print("No .xz files found to convert.")
        return 0
    good_files = []
    for p in files:
        if p.is_symlink():
            print(f"symlink {p}: skipped")
            continue
        good_files.append(p)
    jobs = [(str(p), ".gz", "xz", "gz", args.level, args.legacy_percent) for p in good_files]
    results = (
        [_single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]) for j in jobs]
        if MAX_WORKERS <= 1
        else parallel_map(
            lambda j: _single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]),
            jobs,
            MAX_WORKERS,
        )
    )
    return _report_single_transcode(results)


def _zip_dt_to_ts(date_time: tuple[int, ...]) -> float:
    try:
        return datetime(*date_time).timestamp()
    except (ValueError, TypeError):
        return datetime.now().timestamp()


def _tar_xz_name_for_whl(whl: Path) -> Path:
    return whl.with_suffix(".tar.xz")


def _whl_name_for_tar_xz(src: Path) -> Path:
    name = src.name
    for suf in (".tar.xz", ".txz"):
        if name.lower().endswith(suf):
            return src.with_name(name[: -len(suf)] + ".whl")
    return src.with_suffix(".whl")


def _is_tar_xz(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith((".tar.xz", ".txz"))


def _copy_zipinfo_to_tarinfo(zinfo: zipfile.ZipInfo, tinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    tinfo.size = zinfo.file_size
    if zinfo.date_time:
        tinfo.mtime = _zip_dt_to_ts(zinfo.date_time)
    if zinfo.external_attr:
        mode = (zinfo.external_attr >> 16) & TAR_MODE_MASK
        tinfo.mode = mode or (TAR_SCRIPT_MODE if zinfo.filename.endswith(WHEEL_SCRIPT_SUFFIXES) else TAR_DEFAULT_MODE)
    else:
        tinfo.mode = TAR_DEFAULT_MODE
    tinfo.type = tarfile.REGTYPE
    tinfo.uid = TAR_UID
    tinfo.gid = TAR_GID
    tinfo.uname = TAR_UNAME
    tinfo.gname = TAR_GNAME
    return tinfo


def _copy_tarinfo_to_zipinfo(tinfo: tarfile.TarInfo, zinfo: zipfile.ZipInfo) -> zipfile.ZipInfo:
    if getattr(tinfo, "mtime", None):
        dt = datetime.fromtimestamp(tinfo.mtime)
        zinfo.date_time = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    if getattr(tinfo, "mode", None):
        zinfo.external_attr = (tinfo.mode & 0xFFFF) << 16
    return zinfo


def _whl_to_tar_xz(whl: Path, remove_original: bool) -> tuple[bool, str, Optional[Path]]:
    if not whl.exists() or not whl.is_file():
        return False, f"Invalid file: {whl}", None
    if whl.suffix.lower() != ".whl":
        return False, f"Not a wheel file: {whl.name}", None
    dst = _tar_xz_name_for_whl(whl)
    if dst.exists():
        dst = unique_path(dst)
        print(f"Target exists, using: {dst.name}")
    count = 0
    with zipfile.ZipFile(whl, "r") as zf:
        bad = zf.testzip()
        if bad:
            return False, f"Corrupt ZIP file: {bad}", None
        with tarfile.open(dst, "w:xz") as tf:
            for zinfo in zf.infolist():
                if zinfo.is_dir():
                    continue
                with zf.open(zinfo) as stream:
                    tinfo = tarfile.TarInfo(name=zinfo.filename)
                    tinfo = _copy_zipinfo_to_tarinfo(zinfo, tinfo)
                    tf.addfile(tinfo, stream)
                    count += 1
    if not (dst.exists() and dst.stat().st_size > 0):
        return False, "Output file is empty or missing", None
    if remove_original:
        try:
            whl.unlink()
            print(f"Removed original: {whl.name}")
        except OSError as e:
            return (
                False,
                f"Conversion succeeded but failed to remove original: {e}",
                dst,
            )
    return True, f"Converted {count} files to tar.xz", dst


def _tar_xz_to_whl(src: Path, remove_original: bool) -> tuple[bool, str, Optional[Path]]:
    if not src.exists() or not src.is_file():
        return False, f"Invalid file: {src}", None
    if not _is_tar_xz(src):
        return False, f"Not a tar.xz file: {src.name}", None
    dst = _whl_name_for_tar_xz(src)
    if dst.exists():
        dst = unique_path(dst)
        print(f"Target exists, using: {dst.name}")
    count = 0
    try:
        with (
            tarfile.open(src, "r:xz") as tf,
            zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf,
        ):
            for tinfo in tf.getmembers():
                if tinfo.isdir():
                    continue
                stream = tf.extractfile(tinfo)
                if stream is None:
                    continue
                zinfo = zipfile.ZipInfo(filename=tinfo.name)
                zinfo = _copy_tarinfo_to_zipinfo(tinfo, zinfo)
                zinfo.file_size = tinfo.size
                zf.writestr(zinfo, stream.read())
                stream.close()
                count += 1
    except tarfile.TarError as e:
        return False, f"Tar error: {e}", None
    except Exception as e:
        return False, f"Conversion error: {e}", None
    if not (dst.exists() and dst.stat().st_size > 0):
        return False, "Output file is empty or missing", None
    try:
        with zipfile.ZipFile(dst, "r") as zf:
            bad = zf.testzip()
            if bad:
                return False, f"Created corrupt zip file: {bad}", None
    except Exception as e:
        return False, f"Verification failed: {e}", None
    if remove_original:
        try:
            src.unlink()
            print(f"Removed original: {src.name}")
        except OSError as e:
            return (
                False,
                f"Conversion succeeded but failed to remove original: {e}",
                dst,
            )
    return True, f"Converted {count} files to wheel", dst


def _whl_txz_job(job: tuple[str, bool]) -> tuple[Path, bool, str, Optional[Path]]:
    p = Path(job[0])
    remove_original = job[1]
    if not p.exists():
        return p, False, f"File not found: {p}", None
    suf = p.suffix.lower()
    if suf == ".whl":
        ok, msg, out = _whl_to_tar_xz(p, remove_original)
    elif _is_tar_xz(p):
        ok, msg, out = _tar_xz_to_whl(p, remove_original)
    else:
        return p, False, f"Unsupported file type: {p.suffix}", None
    return p, ok, msg, out


def _setup_logger(verbose: bool, quiet: bool) -> None:
    logger.remove()
    if quiet:
        level = "ERROR"
    elif verbose:
        level = "DEBUG"
    else:
        level = "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> - <level>{level}</level> - <level>{message}</level>",
    )


def _collect_whl_txz_paths(paths: list[str], recursive: bool, to: str) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            logger.error(f"Path does not exist: {p}")
            continue
        if p.is_file():
            if p.suffix.lower() == ".whl" or _is_tar_xz(p):
                out.append(p)
            else:
                logger.warning(f"Skipping unsupported file: {p}")
        elif p.is_dir():
            glob = "**/*" if recursive else "*"
            found: list[Path] = []
            for f in p.glob(glob):
                if not f.is_file():
                    continue
                if f.suffix.lower() == ".whl":
                    if to in ("auto", "tar.xz"):
                        found.append(f)
                elif _is_tar_xz(f) and to in ("auto", "whl"):
                    found.append(f)
            out.extend(found)
            print(f"Found {len(found)} convertible files in {p}")
    return out


def run_whl_txz(args: argparse.Namespace) -> int:
    _setup_logger(args.verbose, args.quiet)
    files = _collect_whl_txz_paths(args.paths, args.recursive, args.to)
    if not files:
        if args.paths == ["."]:
            print("No .whl or .tar.xz files found in current directory")
        else:
            logger.error("No convertible files found")
        return 1
    print(f"Processing {len(files)} file(s)")
    if args.remove_original:
        print("Original files will be removed after successful conversion")
    jobs = [(str(p), args.remove_original) for p in files]
    results = parallel_imap(_whl_txz_job, jobs, MAX_WORKERS) if MAX_WORKERS > 1 else [_whl_txz_job(j) for j in jobs]
    ok = fail = 0
    lines = ["", "=" * 40, "CONVERSION RESULTS", "-" * 40]
    for src, good, msg, out in results:
        if good:
            ok += 1
            src_kind = "whl" if src.suffix.lower() == ".whl" else "tar.xz"
            dst_kind = "tar.xz" if out and out.suffix == ".xz" else "whl"
            size = ""
            if out and out.exists():
                size = f" ({out.stat().st_size / 1024:.1f} KB)"
            lines.append(f"✓ OK {src.name} [{src_kind}] → {out.name if out else 'unknown'} [{dst_kind}]{size}")
            if args.verbose:
                lines.append(f"   {msg}")
        else:
            fail += 1
            lines.append(f"✗ FAIL {src.name}: {msg}")
    lines.append("-" * 40)
    lines.append(f"Summary: {ok} successful, {fail} failed")
    if args.remove_original and ok > 0:
        lines.append("✓ Original files were removed after successful conversion")
    print("\n".join(lines))
    return 0 if fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged_archive_tools",
        description="Unified archive/compression converter (see module docstring for original→merged mapping).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tar-codec", help="Change the codec of *.tar.<codec> files (recursive).")
    p.add_argument("target", choices=sorted(TAR_CODECS), help="Target codec, e.g. xz, zst, br, 7z.")
    p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory to scan recursively (default: cwd).",
    )
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_tar_codec)
    p = sub.add_parser("archive-convert", help="Convert archives between tar/zip/whl formats.")
    p.add_argument(
        "inputs",
        nargs="*",
        default=[],
        help="Files or directories (default: cwd entries).",
    )
    p.add_argument(
        "-t",
        "--to",
        required=True,
        choices=sorted(ARCHIVE_FORMATS),
        help="Target format, e.g. .tar.xz or .zip.",
    )
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_archive_convert)
    p = sub.add_parser("br2zst", help="Recursively convert *.json.br -> *.json.zst.")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_br2zst)
    for name, fn in (("gz2xz", run_gz2xz), ("xz2gz", run_xz2gz)):
        p = sub.add_parser(name, help=f"Convert *.{name[:2]} -> *.{name[3:]} in --root.")
        p.add_argument("--root", type=Path, default=Path.cwd())
        p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
        p.add_argument(
            "--legacy-percent",
            action="store_true",
            help="Reproduce the original buggy '*40' percent display.",
        )
        p.set_defaults(func=fn)
    p = sub.add_parser("whl-txz", help="Bidirectional .whl <-> .tar.xz converter.")
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories (default: current directory).",
    )
    p.add_argument("-r", "--recursive", action="store_true", help="Search directories recursively.")
    p.add_argument(
        "--remove-original",
        action="store_true",
        help="Delete source file after successful conversion.",
    )
    p.add_argument(
        "--to",
        choices=["auto", "whl", "tar.xz"],
        default="auto",
        help="Restrict direction when scanning directories.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=run_whl_txz)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verbose", False) and getattr(args, "quiet", False):
        parser.error("--verbose and --quiet are mutually exclusive")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
