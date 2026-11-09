#!/data/data/com.termux/files/usr/bin/env python
"""LZ4 compression tool for files and folders.
``compress`` writes ``.lz4`` for the files and ``.tar.lz4`` for the folders (``--mode auto|files|folders``), ``decompress`` reverses it, and the level, minimum size, chunk settings, the skip list and ``--keep`` control the behaviour.
Usage: lz4er {compress,decompress} [options] [paths ...] Positional arguments: paths ...
Paths to process (default: current directory) Subcommands: compress Compress files and/or folders decompress Decompress .lz4 and .tar.lz4 files Options: --mode Auto (lzr): files -> .lz4, folders -> .tar.lz4.
files (lzer): compress files only.
folders (lz5): compress each subfolder as .tar.lz4.
Default: auto --level --min-size --chunk-size --chunk-threshold --recursive Walk directories recursively (mode=files) --keep Keep original files/folders after successful compression --skip-ext File extensions to skip (default: archive formats) --no-checksum Disable LZ4 content checksum Examples: $ lz4er compress dist/ --mode folders $ lz4er decompress dist.tar.lz4 Command-line script: run ``python lz4er.py`` or the ``lz4er`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import contextlib
import io
from pathlib import Path
import shutil
import sys
import tarfile

from dh import MAX_WORKERS
import lz4.frame


DEFAULT_SKIP_EXT = {".gz", ".br", ".xz", ".zst", ".bz2", ".zip", ".whl", ".lz4", ".7z", ".rar"}
DEFAULT_MIN_SIZE = 1024
DEFAULT_CHUNK_SIZE = 32768
DEFAULT_CHUNK_THRESHOLD = 524288
DEFAULT_LEVEL = 3
BLOCK_SIZE_MAX = lz4.frame.BLOCKSIZE_MAX


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.2f} {u}"
        v /= 1024
    return f"{v:.2f} {units[-1]}"


def _compress_bytes(data: bytes, level: int, block_size: int, checksum: bool) -> bytes:
    return lz4.frame.compress(
        data,
        compression_level=level,
        block_size=block_size,
        block_linked=True,
        content_checksum=checksum,
    )


def _chunk_worker(args):
    data, level, block_size, checksum = args
    return _compress_bytes(data, level, block_size, checksum)


def compress_file(
    src: Path,
    dst: Path,
    *,
    level: int,
    workers: int,
    chunk_size: int,
    chunk_threshold: int,
    checksum: bool = True,
    block_size: int = BLOCK_SIZE_MAX,
) -> bool:
    try:
        size = src.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        if size < chunk_threshold or workers <= 1:
            dst.write_bytes(_compress_bytes(src.read_bytes(), level, block_size, checksum))
            return True
        n = (size + chunk_size - 1) // chunk_size
        with src.open("rb") as f:
            chunks = [f.read(chunk_size) for _ in range(n)]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_chunk_worker, [(c, level, block_size, checksum) for c in chunks]))
        with dst.open("wb") as o:
            for r in results:
                o.write(r)
        return True
    except Exception as e:
        print(f"  ! Failed to compress {src}: {e}", file=sys.stderr)
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return False


def tar_compress_folder(folder: Path, *, level: int, checksum: bool, block_size: int, keep: bool) -> tuple[bool, str]:
    dst = folder.parent / (folder.name + ".tar.lz4")
    if dst.exists():
        return False, f"Skipped {folder.name}: {dst.name} already exists"
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(str(folder), arcname=folder.name)
        raw = buf.getvalue()
        comp = _compress_bytes(raw, level, block_size, checksum)
        dst.write_bytes(comp)
        if not keep:
            shutil.rmtree(folder)
        return True, f"{folder.name}: {human_size(len(raw))} -> {human_size(len(comp))}"
    except Exception as e:
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return False, f"Error compressing {folder.name}: {e}"


def tar_decompress(archive: Path) -> tuple[bool, str]:
    name = archive.name
    if not name.endswith(".tar.lz4"):
        return False, f"Skipped {name}: not a .tar.lz4 file"
    stem = name[: -len(".tar.lz4")]
    target = archive.parent / stem
    if target.exists():
        return False, f"Skipped {name}: {target.name}/ already exists"
    try:
        data = lz4.frame.decompress(archive.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
            try:
                tf.extractall(path=str(archive.parent), filter="data")
            except TypeError:
                tf.extractall(path=str(archive.parent))
        archive.unlink()
        return True, f"{name} -> {target.name}/"
    except Exception as e:
        return False, f"Failed to extract {name}: {e}"


def plain_decompress(archive: Path) -> tuple[bool, str]:
    target = archive.with_suffix("")
    try:
        data = archive.read_bytes()
        if not data:
            return False, f"Skipped {archive.name}: empty file"
        out = lz4.frame.decompress(data)
        target.write_bytes(out)
        archive.unlink()
        return True, f"{archive.name}: {human_size(len(data))} -> {human_size(len(out))}"
    except Exception as e:
        return False, f"Failed to decompress {archive.name}: {e}"


def _should_skip_file(p: Path, skip_ext: set, min_size: int) -> bool:
    if p.is_symlink() or not p.is_file():
        return True
    if p.suffix.lower() in skip_ext:
        return True
    if ".tar." in p.name:
        return True
    try:
        return p.stat().st_size < min_size
    except OSError:
        return True


def cmd_compress(args) -> int:
    skip_ext = {e.lower() for e in (args.skip_ext or DEFAULT_SKIP_EXT)}
    checksum = not args.no_checksum
    block_size = BLOCK_SIZE_MAX
    stats = {"ok": 0, "skip": 0, "err": 0, "before": 0, "after": 0}

    def _do_file(f: Path) -> None:
        if _should_skip_file(f, skip_ext, args.min_size):
            return
        dst = f.parent / (f.name + ".lz4")
        if dst.exists():
            print(f"  - Skipped {f.name}: {dst.name} already exists")
            stats["skip"] += 1
            return
        try:
            orig = f.stat().st_size
        except OSError:
            return
        if not compress_file(
            f,
            dst,
            level=args.level,
            workers=MAX_WORKERS,
            chunk_size=args.chunk_size,
            chunk_threshold=args.chunk_threshold,
            checksum=checksum,
            block_size=block_size,
        ):
            stats["err"] += 1
            return
        new = dst.stat().st_size
        if new >= orig:
            dst.unlink()
            print(f"  - Skipped {f.name}: no space saved")
            stats["skip"] += 1
            return
        if not args.keep:
            f.unlink()
        pct = (orig - new) / orig * 100
        print(f"  + {f.name}: {human_size(orig)} -> {human_size(new)} ({pct:.1f}% saved)")
        stats["ok"] += 1
        stats["before"] += orig
        stats["after"] += new

    def _do_folder(d: Path) -> None:
        ok, msg = tar_compress_folder(d, level=args.level, checksum=checksum, block_size=block_size, keep=args.keep)
        if ok:
            print(f"  + {msg}")
            stats["ok"] += 1
        else:
            print(f"  - {msg}")
            stats["skip"] += 1

    for root_str in args.paths:
        root = Path(root_str).resolve()
        if not root.exists():
            print(f"Skipped {root}: not found", file=sys.stderr)
            continue
        if root.is_file():
            if args.mode != "folders":
                _do_file(root)
            continue
        if not root.is_dir():
            continue
        if args.mode == "folders":
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.is_symlink():
                    _do_folder(d)
        elif args.mode == "files":
            it = root.rglob("*") if args.recursive else root.iterdir()
            for f in sorted(p for p in it if p.is_file() and not p.is_symlink()):
                _do_file(f)
        else:
            for p in sorted(root.iterdir()):
                if p.is_symlink():
                    continue
                if p.is_dir():
                    _do_folder(p)
                elif p.is_file():
                    _do_file(p)
    print(f"\n{'=' * 40}")
    print(f"Compressed: {stats['ok']}  skipped: {stats['skip']}  errors: {stats['err']}")
    if stats["before"]:
        saved = stats["before"] - stats["after"]
        print(
            f"Total: {human_size(stats['before'])} -> {human_size(stats['after'])} "
            f"({saved / stats['before'] * 100:.1f}% saved, {human_size(saved)} freed)"
        )
    return 0 if stats["err"] == 0 else 1


def cmd_decompress(args) -> int:
    stats = {"ok": 0, "skip": 0, "err": 0}
    for root_str in args.paths:
        root = Path(root_str).resolve()
        if not root.exists():
            print(f"Skipped {root}: not found", file=sys.stderr)
            continue
        if root.is_file():
            candidates = [root]
        else:
            it = root.rglob("*.lz4") if args.recursive else root.glob("*.lz4")
            candidates = sorted(p for p in it if p.is_file() and not p.is_symlink())
        for a in candidates:
            if not a.name.endswith(".lz4"):
                continue
            if a.name.endswith(".tar.lz4"):
                ok, msg = tar_decompress(a)
            else:
                ok, msg = plain_decompress(a)
            if ok:
                print(f"  + {msg}")
                stats["ok"] += 1
            else:
                print(f"  - {msg}")
                stats["err"] += 1
    print(f"\n{'=' * 40}")
    print(f"Decompressed: {stats['ok']}  errors: {stats['err']}")
    return 0 if stats["err"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified LZ4 compression tool (consolidates lz5.py, lzer.py, lzr.py).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compress", help="Compress files and/or folders")
    c.add_argument("paths", nargs="*", default=["."], help="Paths to process (default: current directory)")
    c.add_argument(
        "--mode",
        choices=["auto", "files", "folders"],
        default="auto",
        help="auto (lzr): files -> .lz4, folders -> .tar.lz4. "
        "files (lzer): compress files only. "
        "folders (lz5): compress each subfolder as .tar.lz4. "
        "Default: auto",
    )
    c.add_argument("--level", type=int, default=DEFAULT_LEVEL, help=f"LZ4 compression level (default: {DEFAULT_LEVEL})")
    c.add_argument(
        "--min-size",
        type=int,
        default=DEFAULT_MIN_SIZE,
        help=f"Minimum file size in bytes to compress (default: {DEFAULT_MIN_SIZE})",
    )
    c.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Chunk size in bytes for parallel compression (default: {DEFAULT_CHUNK_SIZE})",
    )
    c.add_argument(
        "--chunk-threshold",
        type=int,
        default=DEFAULT_CHUNK_THRESHOLD,
        help=f"File size above which chunked parallel compression is used (default: {DEFAULT_CHUNK_THRESHOLD})",
    )
    c.add_argument("--recursive", action="store_true", help="Walk directories recursively (mode=files)")
    c.add_argument("--keep", action="store_true", help="Keep original files/folders after successful compression")
    c.add_argument("--skip-ext", nargs="*", default=None, help="File extensions to skip (default: archive formats)")
    c.add_argument("--no-checksum", action="store_true", help="Disable LZ4 content checksum")
    c.set_defaults(func=cmd_compress)
    d = sub.add_parser("decompress", help="Decompress .lz4 and .tar.lz4 files")
    d.add_argument("paths", nargs="*", default=["."], help="Paths to process (default: current directory)")
    d.add_argument("--recursive", action="store_true", help="Walk directories recursively")
    d.set_defaults(func=cmd_decompress)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
