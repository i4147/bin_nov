#!/data/data/com.termux/files/usr/bin/env python
"""Unified zstandard compression and decompression toolkit."""

import argparse
import contextlib
from dataclasses import dataclass
from functools import partial
import io
from multiprocessing import Pool
from pathlib import Path
import shutil
import sys
import tarfile
import threading
import time
from typing import Any, Iterable, Optional, Sequence

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger
import zstandard as zstd


with contextlib.suppress(ImportError):
    from loguru import logger
DEFAULT_LEVEL: int = 19
DEFAULT_THREADS: int = 4
DEFAULT_ARCHIVE_LEVEL: int = 3
ZST_EXT: str = ".zst"
TAR_ZST_EXT: str = ".tar.zst"
SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".zst",
    ".zstd",
    ".gz",
    ".bz2",
    ".xz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
    ".lz",
    ".lz4",
    ".lzma",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".heic",
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".webm",
    ".wmv",
    ".flv",
    ".mp3",
    ".flac",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".whl",
    ".egg",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".iso",
    ".img",
    ".dmg",
    ".exe",
    ".dll",
    ".so",
})


def parse_size(value: str) -> int:
    s = value.strip().upper().replace(" ", "")
    units = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "0"
            return int(float(num) * factor)
    return int(float(s))


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += _file_size(p)
    except OSError:
        pass
    return total


def _safe_extractall(tar: tarfile.TarFile, path: Path) -> None:
    try:
        tar.extractall(path, filter="data")
    except TypeError:
        tar.extractall(path, filter="data")


@dataclass(slots=True)
class TaskResult:
    path: Path
    original_size: int = 0
    processed_size: int = 0
    success: bool = False
    operation: str = "compress"
    duration: float = 0.0
    error: Optional[str] = None
    output: Optional[Path] = None
    original_deleted: bool = False
    tar_extracted: bool = False

    @property
    def savings_pct(self) -> float:
        if self.original_size <= 0:
            return 0.0
        if self.operation == "compress":
            return (1 - self.processed_size / self.original_size) * 100
        return (self.processed_size / self.original_size - 1) * 100


def run_parallel(
    tasks: Sequence[Path],
    worker: Any,
    workers: int,
    operation: str,
    dry_run: bool = False,
) -> list[TaskResult]:
    if not tasks:
        return []
    if dry_run:
        for t in tasks:
            print(f"[dry-run] would {operation} {t}")
        return []
    total = len(tasks)
    lock = threading.Lock()
    state = {"done": 0, "orig": 0, "proc": 0, "start": time.time()}

    def render(last: Optional[TaskResult], final: bool = False) -> None:
        done = state["done"]
        pct = done / total * 100 if total else 0.0
        saved = (1 - state["proc"] / state["orig"]) * 100 if state["orig"] else 0.0
        elapsed = time.time() - state["start"]
        speed = state["orig"] / (1024 * 1024) / elapsed if elapsed else 0.0
        bar_len = 30
        filled = max(0, min(int(bar_len * pct / 100), bar_len - 1))
        bar = "=" * filled + ">" + "." * (bar_len - filled - 1)
        name = last.path.name if last else ""
        if len(name) > 30:
            name = name[:27] + "..."
        ending = "\n" if final else ""
        sys.stdout.write(
            f"\r{operation.upper():10} [{bar}] {pct:5.1f}% {done}/{total} files "
            f"({saved:5.1f}% saved, {speed:5.1f} MB/s) - {name:<30}{ending}"
        )
        sys.stdout.flush()

    results: list[TaskResult] = []
    try:
        with Pool(processes=workers) as pool:
            for res in pool.imap_unordered(worker, tasks):
                results.append(res)
                with lock:
                    state["done"] += 1
                    state["orig"] += res.original_size
                    state["proc"] += res.processed_size
                    render(res)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return results
    render(results[-1] if results else None, final=True)
    return results


def compress_file(
    src: Path,
    level: int,
    threads: int,
    keep: bool,
    only_if_smaller: bool = False,
) -> TaskResult:
    t0 = time.perf_counter()
    dst = src.with_suffix(src.suffix + ZST_EXT)
    tmp = src.with_suffix(src.suffix + ZST_EXT + ".tmp")
    if dst.exists():
        return TaskResult(
            path=src,
            success=False,
            error="output exists",
            operation="compress",
            duration=time.perf_counter() - t0,
        )
    try:
        original = _file_size(src)
        if original == 0:
            return TaskResult(
                path=src,
                success=False,
                error="empty file",
                operation="compress",
                duration=time.perf_counter() - t0,
            )
        cctx = zstd.ZstdCompressor(level=level, threads=threads)
        with (
            src.open("rb") as fin,
            tmp.open("wb") as fout,
            cctx.stream_writer(fout) as writer,
        ):
            shutil.copyfileobj(fin, writer, length=1024 * 1024)
        compressed = tmp.stat().st_size
        if only_if_smaller and compressed >= original:
            tmp.unlink(missing_ok=True)
            return TaskResult(
                path=src,
                original_size=original,
                processed_size=original,
                success=False,
                error="compressed not smaller",
                operation="compress",
                duration=time.perf_counter() - t0,
            )
        tmp.rename(dst)
        deleted = False
        if not keep:
            src.unlink()
            deleted = True
        return TaskResult(
            path=src,
            original_size=original,
            processed_size=compressed,
            success=True,
            operation="compress",
            output=dst,
            original_deleted=deleted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return TaskResult(
            path=src,
            success=False,
            error=str(exc),
            operation="compress",
            duration=time.perf_counter() - t0,
        )


def tar_compress_dir(
    src_dir: Path,
    level: int,
    threads: int,
    keep: bool,
) -> TaskResult:
    t0 = time.perf_counter()
    dst = src_dir.with_name(src_dir.name + TAR_ZST_EXT)
    tmp_tar = src_dir.parent / f".tmp_{src_dir.name}.tar"
    if dst.exists():
        return TaskResult(
            path=src_dir,
            success=False,
            error="output exists",
            operation="compress",
            duration=time.perf_counter() - t0,
        )
    try:
        original = dir_size(src_dir)
        with tarfile.open(tmp_tar, "w") as tar:
            tar.add(src_dir, arcname=src_dir.name)
        cctx = zstd.ZstdCompressor(level=level, threads=threads)
        with (
            tmp_tar.open("rb") as fin,
            dst.open("wb") as fout,
            cctx.stream_writer(fout) as writer,
        ):
            shutil.copyfileobj(fin, writer, length=1024 * 1024)
        tmp_tar.unlink(missing_ok=True)
        compressed = dst.stat().st_size
        deleted = False
        if not keep:
            shutil.rmtree(src_dir)
            deleted = True
        return TaskResult(
            path=src_dir,
            original_size=original,
            processed_size=compressed,
            success=True,
            operation="compress",
            output=dst,
            original_deleted=deleted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp_tar.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        return TaskResult(
            path=src_dir,
            success=False,
            error=str(exc),
            operation="compress",
            duration=time.perf_counter() - t0,
        )


def decompress_file(
    src: Path,
    keep: bool = True,
    untar: bool = True,
) -> TaskResult:
    t0 = time.perf_counter()
    suffixes = src.suffixes
    if len(suffixes) >= 2 and suffixes[-2:] == [".tar", ".zst"]:
        dst = src.with_suffix("").with_suffix("")
        is_tar = True
    elif src.suffix == ZST_EXT:
        dst = src.with_suffix("")
        is_tar = False
    else:
        return TaskResult(
            path=src,
            success=False,
            error="not a .zst file",
            operation="decompress",
            duration=time.perf_counter() - t0,
        )
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        original = _file_size(src)
        dctx = zstd.ZstdDecompressor()
        with (
            src.open("rb") as fin,
            tmp.open("wb") as fout,
            dctx.stream_reader(fin) as reader,
        ):
            shutil.copyfileobj(reader, fout, length=1024 * 1024)
        tmp.rename(dst)
        tar_extracted = False
        if untar and is_tar:
            with tarfile.open(dst, "r") as tar:
                _safe_extractall(tar, dst.parent)
            dst.unlink()
            tar_extracted = True
        processed = _file_size(dst) if dst.exists() else original
        deleted = False
        if not keep:
            src.unlink()
            deleted = True
        return TaskResult(
            path=src,
            original_size=original,
            processed_size=processed,
            success=True,
            operation="decompress",
            output=dst,
            original_deleted=deleted,
            tar_extracted=tar_extracted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return TaskResult(
            path=src,
            success=False,
            error=str(exc),
            operation="decompress",
            duration=time.perf_counter() - t0,
        )


def _compress_file_task(path: Path, *, level: int, threads: int, keep: bool, only_if_smaller: bool) -> TaskResult:
    return compress_file(path, level, threads, keep, only_if_smaller)


def _tar_compress_dir_task(path: Path, *, level: int, threads: int, keep: bool) -> TaskResult:
    return tar_compress_dir(path, level, threads, keep)


def _decompress_task(path: Path, *, keep: bool, untar: bool) -> TaskResult:
    return decompress_file(path, keep=keep, untar=untar)


def _should_skip_path(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts)


def discover_targets(root: Path, args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = []
    if args.targets in ("files", "both"):
        it: Iterable[Path] = root.rglob("*") if args.recursive else root.iterdir()
        whitelist = None
        if args.ext:
            whitelist = {e if e.startswith(".") else f".{e}" for e in args.ext}
        for p in it:
            if not p.is_file():
                continue
            if _should_skip_path(p):
                continue
            if args.exclude and any(pat in str(p) for pat in args.exclude):
                continue
            if whitelist is not None:
                if p.suffix.lower() not in whitelist:
                    continue
            elif p.suffix.lower() in SKIP_EXTENSIONS:
                continue
            if args.min_size and _file_size(p) < args.min_size:
                continue
            files.append(p)
    if args.targets in ("dirs", "both"):
        for d in root.iterdir():
            if not d.is_dir():
                continue
            if d.name in SKIP_DIRS:
                continue
            if args.exclude and any(pat in str(d) for pat in args.exclude):
                continue
            if args.min_size and dir_size(d) < args.min_size:
                continue
            dirs.append(d)
    return files, dirs


def print_summary(results: Sequence[TaskResult], operation: str) -> None:
    total = len(results)
    ok = [r for r in results if r.success]
    fail = [r for r in results if not r.success]
    orig = sum(r.original_size for r in ok)
    proc = sum(r.processed_size for r in ok)
    duration = sum(r.duration for r in results)
    print()
    print("=" * 60)
    print(f"{operation.capitalize()} summary")
    print("=" * 60)
    print(f"Total: {total}   Success: {len(ok)}   Failed: {len(fail)}")
    print(f"Original size : {human_size(orig)}")
    print(f"Processed size: {human_size(proc)}")
    if operation == "compress" and orig:
        print(f"Space saved   : {(1 - proc / orig) * 100:.1f}%  ({human_size(orig - proc)})")
    if duration:
        print(f"CPU time      : {duration:.2f}s")
    for r in fail:
        logger.warning(f"FAIL {r.path}: {r.error}")


def cmd_compress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    files, dirs = discover_targets(root, args)
    if not files and not dirs:
        print("Nothing to compress.")
        return 0
    print(
        f"Targets: {len(files)} file(s), {len(dirs)} dir(s) "
        f"(level {args.level}, threads {args.threads}, workers {MAX_WORKERS})"
    )
    results: list[TaskResult] = []
    if dirs:
        worker = partial(
            _tar_compress_dir_task,
            level=args.level,
            threads=args.threads,
            keep=args.keep,
        )
        results += run_parallel(dirs, worker, MAX_WORKERS, "compress", dry_run=args.dry_run)
    if files:
        worker = partial(
            _compress_file_task,
            level=args.level,
            threads=args.threads,
            keep=args.keep,
            only_if_smaller=args.only_if_smaller,
        )
        results += run_parallel(files, worker, MAX_WORKERS, "compress", dry_run=args.dry_run)
    print_summary(results, "compress")
    return 0 if all(r.success or not r.original_size for r in results) else 1


def cmd_decompress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    it = root.rglob("*") if args.recursive else root.iterdir()
    targets = [p for p in it if p.is_file() and p.suffix == ZST_EXT]
    if not targets:
        print("No .zst files found.")
        return 0
    print(f"Decompressing {len(targets)} file(s) (workers {MAX_WORKERS}, keep={args.keep})")
    worker = partial(_decompress_task, keep=args.keep, untar=not args.no_untar)
    results = run_parallel(targets, worker, MAX_WORKERS, "decompress", dry_run=args.dry_run)
    print_summary(results, "decompress")
    return 0 if all(r.success for r in results) else 1


def _verify_archive(archive: Path) -> None:
    print(f"\nVerifying {archive.name} …")
    try:
        dctx = zstd.ZstdDecompressor()
        with (
            archive.open("rb") as f,
            dctx.stream_reader(f) as reader,
            tarfile.open(fileobj=reader, mode="r|") as tar,
        ):
            count = 0
            for member in tar:
                count += 1
                if count <= 5:
                    kind = "dir" if member.isdir() else "file"
                    print(f"  {member.name} ({kind}, {member.size} B)")
            if count > 5:
                print(f"  … (+{count - 5} more)")
        print(f"OK — {count} entries")
    except Exception as exc:
        logger.error(f"Verification failed: {exc}")


def cmd_archive_cwd(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    parent = cwd.parent
    if str(cwd) == "/" or cwd == Path.home():
        logger.error("Refusing to archive root or home directory")
        return 1
    archive = parent / f"{cwd.name}{TAR_ZST_EXT}"
    if archive.exists() and not args.force:
        ans = input(f"Archive '{archive}' exists. Overwrite? (y/n): ").strip().lower()
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return 1
    try:
        cctx = zstd.ZstdCompressor(level=args.level, threads=args.threads or 0)
        print(f"Creating archive: {archive}")
        with (
            archive.open("wb") as f,
            cctx.stream_writer(f) as sw,
            tarfile.open(fileobj=sw, mode="w|") as tar,
        ):
            for p in cwd.rglob("*"):
                if ".git" in p.parts:
                    continue
                if p.name.endswith(TAR_ZST_EXT):
                    continue
                try:
                    arc = p.relative_to(parent)
                    tar.add(p, arcname=arc, recursive=False)
                except (OSError, ValueError) as exc:
                    logger.warning(f"Skip {p}: {exc}")
    except KeyboardInterrupt:
        archive.unlink(missing_ok=True)
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        archive.unlink(missing_ok=True)
        logger.error(f"Archive failed: {exc}")
        return 1
    if not archive.exists() or archive.stat().st_size == 0:
        logger.error("Archive creation produced an empty file")
        archive.unlink(missing_ok=True)
        return 1
    print(f"Archive created: {archive} ({human_size(archive.stat().st_size)})")
    if args.verify:
        _verify_archive(archive)
    if not args.no_remove:
        ans = input(f"Remove original directory '{cwd}'? (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            shutil.rmtree(cwd)
            print("Original directory removed.")
        else:
            print("Original directory preserved.")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    src = Path(args.input).resolve()
    if not src.is_file():
        logger.error(f"Input file not found: {src}")
        return 1
    if args.parts < 1:
        logger.error("N must be >= 1")
        return 1
    out_dir = Path(args.output_dir).resolve() if args.output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = src.stem
    base = base.removesuffix(".tar")
    print(f"Reading {src} …")
    dctx = zstd.ZstdDecompressor()
    with src.open("rb") as f:
        tar_bytes = dctx.stream_reader(f).read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        total_members = sum(1 for _ in tar)
    print(f"Total members: {total_members}")
    n = min(args.parts, total_members) or 1
    if n != args.parts:
        print(f"Warning: only {n} part(s) produced (fewer members than requested).")
    base_count, extra = divmod(total_members, n)
    part_num = 1
    current = 0
    target = base_count + (1 if part_num <= extra else 0)
    buf = io.BytesIO()
    writer = tarfile.open(fileobj=buf, mode="w|")
    cctx = zstd.ZstdCompressor(level=args.level)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        for member in tar:
            if member.isfile():
                writer.addfile(member, tar.extractfile(member))
            else:
                writer.addfile(member)
            current += 1
            if current >= target and part_num < n:
                writer.close()
                buf.seek(0)
                out_path = out_dir / f"{base}.part{part_num:02d}{TAR_ZST_EXT}"
                with out_path.open("wb") as f:
                    f.write(cctx.compress(buf.read()))
                print(f"  wrote {out_path.name}")
                part_num += 1
                current = 0
                target = base_count + (1 if part_num <= extra else 0)
                buf = io.BytesIO()
                writer = tarfile.open(fileobj=buf, mode="w|")
    writer.close()
    buf.seek(0)
    out_path = out_dir / f"{base}.part{part_num:02d}{TAR_ZST_EXT}"
    with out_path.open("wb") as f:
        f.write(cctx.compress(buf.read()))
    print(f"  wrote {out_path.name}")
    print(f"\nSplit into {part_num} parts.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zstd_toolkit.py",
        description="Unified zstandard compression / decompression toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_c = sub.add_parser("compress", help="Compress files and/or directories")
    p_c.add_argument("directory", nargs="?", default=".", help="Root directory (default: current)")
    p_c.add_argument(
        "-t",
        "--targets",
        choices=("files", "dirs", "both"),
        default="files",
        help="What to compress (default: files)",
    )
    p_c.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_LEVEL,
        help=f"zstd level 1-22 (default: {DEFAULT_LEVEL})",
    )
    p_c.add_argument(
        "-T",
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"zstd intra-file threads (default: {DEFAULT_THREADS})",
    )
    p_c.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep original files/dirs after compression",
    )
    p_c.add_argument(
        "-m",
        "--min-size",
        type=parse_size,
        default=0,
        help="Minimum size (bytes or '10MB'-style) to consider",
    )
    p_c.add_argument(
        "-e",
        "--ext",
        nargs="+",
        default=None,
        help="Whitelist of file extensions (e.g. .txt .log)",
    )
    p_c.add_argument("--exclude", nargs="+", default=[], help="Substrings of paths to exclude")
    p_c.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Descend into subdirectories when targeting files",
    )
    p_c.add_argument(
        "--only-if-smaller",
        action="store_true",
        help="Only keep the .zst if it is smaller than the original",
    )
    p_c.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be done, change nothing",
    )
    p_c.set_defaults(func=cmd_compress)
    p_d = sub.add_parser("decompress", help="Decompress .zst / .tar.zst files")
    p_d.add_argument("directory", nargs="?", default=".", help="Root directory (default: current)")
    p_d.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirectories")
    p_d.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the .zst archive after decompression",
    )
    p_d.add_argument(
        "--no-untar",
        action="store_true",
        help="Do not extract .tar.zst archives, just leave the .tar",
    )
    p_d.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be done, change nothing",
    )
    p_d.set_defaults(func=cmd_decompress)
    p_a = sub.add_parser("archive-cwd", help="Archive the current directory into its parent")
    p_a.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_ARCHIVE_LEVEL,
        help=f"zstd level (default: {DEFAULT_ARCHIVE_LEVEL})",
    )
    p_a.add_argument(
        "-T",
        "--threads",
        type=int,
        default=0,
        help="zstd threads (0 = single-threaded, default)",
    )
    p_a.add_argument("--verify", action="store_true", help="List the archive after creation")
    p_a.add_argument(
        "--no-remove",
        action="store_true",
        help="Do not prompt to remove the original directory",
    )
    p_a.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing archive without prompting",
    )
    p_a.set_defaults(func=cmd_archive_cwd)
    p_s = sub.add_parser("split", help="Split a .tar.zst archive into N parts")
    p_s.add_argument("input", help="Path to a .tar.zst file")
    p_s.add_argument("parts", type=int, help="Number of parts to create")
    p_s.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Output directory (default: same as input)",
    )
    p_s.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_LEVEL,
        help=f"zstd level (default: {DEFAULT_LEVEL})",
    )
    p_s.set_defaults(func=cmd_split)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
