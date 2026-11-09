#!/data/data/com.termux/files/usr/bin/env python
"""Convert tar/zip archives to ``.tar.br`` and back.
Repacks the tar, zip, tgz, txz, zst and 7z archives as Brotli-compressed tars (quality 11, or 3 above 10 MB, streamed in 1 MB chunks) with eight parallel jobs, and ``-d`` decompresses the ``.tar.br`` files again.
Usage: archive_convert [paths ...] [-d] Positional arguments: paths ...
Files or directories to process (default: current directory).
Options: -d, --decompress Decompress the given .tar.br file(s) back to .tar.
Examples: $ archive_convert dist/ $ archive_convert bundle.tar.br -d Command-line script: run ``python archive_convert.py`` or the ``archive_convert`` command.
The module is importable as well."""

import argparse
import bz2
from collections.abc import Iterator
import contextlib
import gzip
import lzma
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import TYPE_CHECKING
import zipfile

import brotli
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".tar.xz",
    ".tar.zst",
    ".tar.7z",
    ".tar.lz4",
    ".tar.bz2",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
)
SUFFIXES_BY_LENGTH: tuple[str, ...] = tuple(sorted(ARCHIVE_SUFFIXES, key=len, reverse=True))
CHUNK: int = 1 << 20
BIG_TAR_THRESHOLD: int = 10 * 1024 * 1024
BROTLI_QUALITY_DEFAULT: int = 11
BROTLI_QUALITY_BIG: int = 3
DEFAULT_JOBS: int = 8
ProcResult = tuple[Path, Path | None, int, int, str | None]


def human(n: int) -> str:
    sign = "-" if n < 0 else ""
    v = float(abs(n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024.0 or unit == "TiB":
            return f"{sign}{v:.2f}{unit}"
        v /= 1024.0
    return f"{sign}{v:.2f}PiB"


def relpath(p: Path, base: Path) -> str:
    try:
        return Path(p).relative_to(base)
    except ValueError:
        return str(p)


def find_archives(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            if not name.lower().endswith(ARCHIVE_SUFFIXES):
                continue
            p = base / name
            if p.is_symlink():
                continue
            yield p


def target_path(src: Path) -> Path:
    low = src.name.lower()
    for sfx in SUFFIXES_BY_LENGTH:
        if low.endswith(sfx):
            return src.with_name(src.name[: -len(sfx)] + ".tar.br")
    msg = f"no known archive suffix: {src.name}"
    raise ValueError(msg)


def _zip_to_tar(src: Path, workdir: Path, tar_path: Path) -> None:
    extract_dir = workdir / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(src) as zf:
        zf.extractall(extract_dir)
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for entry in sorted(extract_dir.rglob("*")):
            if entry.is_symlink():
                continue
            tf.add(
                entry,
                arcname=str(entry.relative_to(extract_dir)),
                recursive=False,
            )
    shutil.rmtree(extract_dir)


def _to_tar(src: Path, workdir: Path) -> Path:
    low = src.name.lower()
    tar_path = workdir / "stream.tar"
    if low.endswith(".zip"):
        _zip_to_tar(src, workdir, tar_path)
        return tar_path
    with tar_path.open("wb") as fout:
        if low.endswith((".tar.gz", ".tgz")):
            with gzip.open(src, "rb") as fin:
                shutil.copyfileobj(fin, fout, CHUNK)
        elif low.endswith((".tar.xz", ".txz")):
            with lzma.open(src, "rb") as fin:
                shutil.copyfileobj(fin, fout, CHUNK)
        elif low.endswith((".tar.bz2", ".tbz2")):
            with bz2.open(src, "rb") as fin:
                shutil.copyfileobj(fin, fout, CHUNK)
        elif low.endswith(".tar.zst"):
            subprocess.run(["zstd", "-dc", str(src)], stdout=fout, check=True)
        elif low.endswith(".tar.7z"):
            subprocess.run(
                ["7z", "x", "-so", "-y", "-bd", str(src)],
                stdout=fout,
                check=True,
            )
        elif low.endswith(".tar.lz4"):
            subprocess.run(["lz4", "-dc", str(src)], stdout=fout, check=True)
        else:
            msg = f"unsupported archive: {src.name}"
            raise ValueError(msg)
    return tar_path


def _brotli_stream(src: Path, dst: Path) -> None:
    size = src.stat().st_size
    quality = BROTLI_QUALITY_BIG if size > BIG_TAR_THRESHOLD else BROTLI_QUALITY_DEFAULT
    tmp = dst.with_name(dst.name + ".partial")
    try:
        with src.open("rb") as fin, tmp.open("wb") as fout:
            comp = brotli.Compressor(quality=quality)
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                out = comp.process(chunk)
                if out:
                    fout.write(out)
            tail = comp.finish()
            if tail:
                fout.write(tail)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process(src: Path) -> ProcResult:
    try:
        src_size = src.stat().st_size
    except OSError as exc:
        return src, None, 0, 0, f"stat failed: {exc}"
    if src.is_symlink() or not src.is_file():
        return src, None, 0, 0, "not a regular file"
    try:
        dst = target_path(src)
    except ValueError as exc:
        return src, None, src_size, 0, str(exc)
    if dst.exists():
        return src, dst, src_size, 0, f"target exists: {dst.name}"
    try:
        with tempfile.TemporaryDirectory(prefix="tobr-") as td:
            tar_path = _to_tar(src, Path(td))
            _brotli_stream(tar_path, dst)
        dst_size = dst.stat().st_size
        src.unlink()
        return src, dst, src_size, dst_size, None
    except Exception as exc:
        with contextlib.suppress(OSError):
            dst.unlink(missing_ok=True)
        return src, dst, src_size, 0, f"{type(exc).__name__}: {exc}"


def decompress_one(src: Path) -> tuple[Path, int, int]:
    if not src.name.endswith(".tar.br"):
        msg = f"not a .tar.br file: {src.name}"
        raise ValueError(msg)
    if src.is_symlink() or not src.is_file():
        msg = f"not a regular file: {src.name}"
        raise ValueError(msg)
    dst = src.with_name(src.name[: -len(".br")])
    if dst.exists():
        msg = f"refusing to overwrite: {dst.name}"
        raise FileExistsError(msg)
    src_size = src.stat().st_size
    tmp = dst.with_name(dst.name + ".partial")
    try:
        with src.open("rb") as fin, tmp.open("wb") as fout:
            dec = brotli.Decompressor()
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                out = dec.process(chunk)
                if out:
                    fout.write(out)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst, src_size, dst.stat().st_size


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert tar/zip archives to .tar.br recursively, or decompress .tar.br with -d.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: current directory).",
    )
    parser.add_argument(
        "-d",
        "--decompress",
        action="store_true",
        help="Decompress the given .tar.br file(s) back to .tar.",
    )
    return parser.parse_args(argv)


def collect_targets(paths: list[Path]) -> list[Path]:
    roots = paths or [Path.cwd()]
    found: list[Path] = []
    for root in roots:
        try:
            rp = root.resolve(strict=True)
        except FileNotFoundError:
            msg = f"path not found: {root}"
            raise SystemExit(msg)
        if rp.is_file():
            if not rp.is_symlink() and rp.name.lower().endswith(ARCHIVE_SUFFIXES):
                found.append(rp)
        elif rp.is_dir():
            found.extend(find_archives(rp))
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def run_decompress(paths: list[Path], cwd: Path) -> int:
    if not paths:
        msg = "no archive given for -d"
        raise SystemExit(msg)
    failures = 0
    for raw in paths:
        try:
            rp = raw.resolve(strict=True)
        except FileNotFoundError:
            logger.error(f"fail {relpath(raw, cwd)}: not found")
            failures += 1
            continue
        try:
            dst, src_size, dst_size = decompress_one(rp)
        except Exception as exc:
            logger.error(f"fail {relpath(rp, cwd)}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        saved = src_size - dst_size
        logger.info(
            f"ok {relpath(rp, cwd)} -> {relpath(dst, cwd)}: {human(src_size)} -> {human(dst_size)} ({human(-saved)})"
        )
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cwd = Path.cwd()
    if args.decompress:
        return run_decompress(args.paths, cwd)
    targets = collect_targets(args.paths)
    logger.info(f"targets: {len(targets)}")
    if not targets:
        return 0
    ok = 0
    failures = 0
    total_in = 0
    total_out = 0
    with mp.Pool(MAX_WORKERS) as pool:
        for src, dst, ssize, dsize, err in pool.imap_unordered(process, targets, chunksize=1):
            rp = relpath(src, cwd)
            if err is None and dst is not None:
                saved = ssize - dsize
                total_in += ssize
                total_out += dsize
                ok += 1
                pct = (saved / ssize * 100.0) if ssize else 0.0
                logger.info(
                    f"ok {rp} -> {relpath(dst, cwd)}: "
                    f"{human(ssize)} -> {human(dsize)} "
                    f"({pct:+.1f}%, saved {human(saved)})"
                )
            else:
                failures += 1
                logger.error(f"fail {rp}: {err}")
    if ok:
        total_saved = total_in - total_out
        total_pct = (total_saved / total_in * 100.0) if total_in else 0.0
        logger.info(
            f"totals: ok={ok} fail={failures} "
            f"in={human(total_in)} out={human(total_out)} "
            f"saved={human(total_saved)} ({total_pct:+.1f}%)"
        )
    else:
        logger.info(f"totals: ok=0 fail={failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
