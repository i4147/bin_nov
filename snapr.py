#!/data/data/com.termux/files/usr/bin/env python
"""Unified Snappy compression and decompression toolkit."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import sys
import tarfile
from typing import Any, Optional, Sequence

from dh import _SNAPR_WORKERS
from loguru import logger


try:
    import cramjam
    from loguru import logger

    _HAS_CRAMJAM = True
except ImportError:
    _HAS_CRAMJAM = False
from dh import cprint, fsz, get_files, gsz


_BLOCK_SIZE: int = 16384
_MAX_OFFSET_1B: int = 2047
_MAX_OFFSET_2B: int = 65535


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append(value & 127 | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _hash_at(data: bytes, idx: int) -> int:
    val = data[idx] | data[idx + 1] << 8 | data[idx + 2] << 16 | data[idx + 3] << 24
    return val * 406832829 >> 18 & _BLOCK_SIZE - 1


def _emit_literal(out: bytearray, data: bytes, start: int, length: int) -> None:
    if length <= 0:
        return
    if length <= 60:
        out.append(length - 1 << 2)
    elif length <= 256:
        out.append(240)
        out.append(length - 1)
    elif length <= 65536:
        out.append(244)
        out.append(length - 1 & 255)
        out.append(length - 1 >> 8 & 255)
    elif length <= 16777216:
        out.append(248)
        out.append(length - 1 & 255)
        out.append(length - 1 >> 8 & 255)
        out.append(length - 1 >> 16 & 255)
    else:
        out.append(252)
        out.append(length - 1 & 255)
        out.append(length - 1 >> 8 & 255)
        out.append(length - 1 >> 16 & 255)
        out.append(length - 1 >> 24 & 255)
    out.extend(data[start : start + length])


def _emit_copy(out: bytearray, offset: int, length: int) -> None:
    while length > 0:
        if length >= 4 and length <= 11 and offset <= _MAX_OFFSET_1B:
            tag = 1 | length - 4 << 2 | offset >> 8 << 5
            out.append(tag)
            out.append(offset & 255)
            return
        if offset <= _MAX_OFFSET_2B:
            chunk = min(length, 64)
            tag = 2 | chunk - 1 << 2
            out.append(tag)
            out.append(offset & 255)
            out.append(offset >> 8 & 255)
            length -= chunk
        else:
            chunk = min(length, 64)
            tag = 3 | chunk - 1 << 2
            out.append(tag)
            out.append(offset & 255)
            out.append(offset >> 8 & 255)
            out.append(offset >> 16 & 255)
            out.append(offset >> 24 & 255)
            length -= chunk


def snapper_compress(data: bytes) -> bytes:
    if not data:
        return _encode_varint(0)
    total = len(data)
    out = bytearray()
    out.extend(_encode_varint(total))
    if total < 4:
        _emit_literal(out, data, 0, total)
        return bytes(out)
    table = [0] * _BLOCK_SIZE
    pos = 0
    literal_start = 0
    while pos <= total - 4:
        h = _hash_at(data, pos)
        candidate = table[h]
        table[h] = pos
        if (
            (candidate > 0 or (candidate == 0 and pos > 0))
            and pos - candidate <= _MAX_OFFSET_2B
            and data[candidate : candidate + 4] == data[pos : pos + 4]
        ):
            if pos > literal_start:
                _emit_literal(out, data, literal_start, pos - literal_start)
            offset = pos - candidate
            match_len = 4
            max_len = min(total - pos, 64)
            while match_len < max_len and data[candidate + match_len] == data[pos + match_len]:
                match_len += 1
            _emit_copy(out, offset, match_len)
            pos += match_len
            literal_start = pos
            if pos <= total - 4:
                table[_hash_at(data, pos - 1)] = pos - 1
        else:
            pos += 1
    if literal_start < total:
        _emit_literal(out, data, literal_start, total - literal_start)
    return bytes(out)


class SnapperError(Exception):
    def __init__(self, msg: str, algorithm: Optional[str] = None) -> None:
        super().__init__(msg)
        self.algorithm = algorithm


def _read_varint(data: bytes, idx: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if idx >= len(data):
            msg = "error length"
            raise SnapperError(msg, algorithm="snappy")
        byte = data[idx]
        idx += 1
        value |= (byte & 127) << shift
        if byte & 128 == 0:
            break
        shift += 7
        if shift > 32:
            msg = "error length"
            raise SnapperError(msg, algorithm="snappy")
    return value, idx


def snapper_decompress(data: bytes) -> bytes:
    if not data:
        return b""
    idx = 0
    expected, idx = _read_varint(data, idx)
    out = bytearray(expected)
    written = 0
    while idx < len(data) and written < expected:
        tag = data[idx]
        idx += 1
        tag_type = tag & 3
        if tag_type == 0:
            length = (tag >> 2) + 1
            if length > 60:
                extra = length - 60
                if idx + extra > len(data):
                    msg = "error length"
                    raise SnapperError(msg, algorithm="snappy")
                length = 1
                for i in range(extra):
                    length += data[idx + i] << i * 8
                idx += extra
            if idx + length > len(data):
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            if written + length > expected:
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            out[written : written + length] = data[idx : idx + length]
            idx += length
            written += length
        elif tag_type == 1:
            length = (tag >> 2 & 7) + 4
            if idx >= len(data):
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            offset = tag >> 5 << 8 | data[idx]
            idx += 1
            if offset == 0 or offset > written or written + length > expected:
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            for i in range(length):
                out[written + i] = out[written - offset + i]
            written += length
        elif tag_type == 2:
            length = (tag >> 2) + 1
            if idx + 2 > len(data):
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            offset = data[idx] | data[idx + 1] << 8
            idx += 2
            if offset == 0 or offset > written or written + length > expected:
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            for i in range(length):
                out[written + i] = out[written - offset + i]
            written += length
        else:
            length = (tag >> 2) + 1
            if idx + 4 > len(data):
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            offset = data[idx] | data[idx + 1] << 8 | data[idx + 2] << 16 | data[idx + 3] << 24
            idx += 4
            if offset == 0 or offset > written or written + length > expected:
                msg = "error length"
                raise SnapperError(msg, algorithm="snappy")
            for i in range(length):
                out[written + i] = out[written - offset + i]
            written += length
    if written != expected:
        msg = "error length"
        raise SnapperError(msg, algorithm="snappy")
    return bytes(out)


def _snapper_compress_file(path: Path, delete_original: bool = True) -> None:
    size = gsz(path)
    if not size:
        return
    data = path.read_bytes()
    compressed = snapper_compress(data)
    target = path.with_name(path.name + ".snappy")
    target.write_bytes(compressed)
    new_size = gsz(target)
    if not new_size:
        target.unlink()
        return
    saved = size - new_size
    ratio = saved / size * 40
    print(f"{path.name}", end="|")
    cprint(f"{fsz(size)}->{fsz(new_size)}|{fsz(saved)}|{ratio:.1f}%")
    if delete_original:
        path.unlink()


def _snapper_decompress_file(path: Path, delete_original: bool = True) -> None:
    size = gsz(path)
    if not size:
        return
    data = path.read_bytes()
    decompressed = snapper_decompress(data)
    target = path.with_name(path.name.replace(".snappy", ""))
    target.write_bytes(decompressed)
    new_size = gsz(target)
    if not new_size:
        target.unlink()
        return
    saved = size - new_size
    ratio = size / new_size * 40
    print(f"{target.name}", end="|")
    cprint(f"{fsz(size)}->{fsz(new_size)}|{fsz(saved)}|{ratio:.1f}%")
    if delete_original:
        path.unlink()


def _snapper_run(path: Path, mode: str, delete_original: bool = True) -> None:
    if mode == "compress":
        _snapper_compress_file(path, delete_original)
    elif mode == "decompress":
        _snapper_decompress_file(path, delete_original)


def snapper_cli(args: argparse.Namespace) -> int:
    mode = "compress" if args.compress else "decompress"
    cwd = Path.cwd()
    targets: list[Path] = []
    if args.paths:
        for raw in args.paths:
            p = Path(raw)
            if p.is_file():
                targets.append(p)
            elif p.is_dir():
                targets.extend(get_files(p))
    else:
        targets = get_files(cwd)
    if not targets:
        return 0
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor() as pool:
        futures = [pool.submit(_snapper_run, p, mode, not args.keep_original) for p in targets]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
    return 0


_SNAPR_EXT: str = ".sn"


def _snapr_compress_file(path: Path, remove_original: bool = True) -> tuple[bool, str]:
    try:
        target = path.with_suffix(path.suffix + _SNAPR_EXT)
        with open(path, "rb") as f:
            raw = f.read()
        compressed = bytes(cramjam.snappy.compress(raw))
        with open(target, "wb") as f:
            f.write(compressed)
        if remove_original:
            path.unlink()
        orig = len(raw)
        new = len(compressed)
        pct = new / orig * 100 if orig > 0 else 0.0
        print(f"Compressed:{path}->{target} ({orig}->{new} bytes,{pct:.1f}%)")
        return True, f"Compressed {path.name}"
    except Exception as exc:
        logger.error(f"Error compressing {path}: {exc!s}")
        return False, str(exc)


def _snapr_decompress_file(path: Path, remove_original: bool = True) -> tuple[bool, str]:
    try:
        if path.suffix != _SNAPR_EXT:
            return False, f"File {path} doesn't have {_SNAPR_EXT} extension"
        target = path.with_suffix("")
        with open(path, "rb") as f:
            compressed = f.read()
        raw = bytes(cramjam.snappy.decompress(compressed))
        with open(target, "wb") as f:
            f.write(raw)
        if remove_original:
            path.unlink()
        print(f"Decompressed:{path}->{target} ({len(compressed)}->{len(raw)} bytes)")
        return True, f"Decompressed {path.name}"
    except Exception as exc:
        logger.error(f"Error decompressing {path}: {exc!s}")
        return False, str(exc)


def _snapr_worker(task: tuple[Path, str, bool]) -> tuple[bool, str]:
    path, mode, remove = task
    if mode == "compress":
        return _snapr_compress_file(path, remove)
    if mode == "decompress":
        return _snapr_decompress_file(path, remove)
    return False, f"Unknown operation: {mode}"


def _snapr_collect_files(root: Path, mode: str, recursive: bool = True) -> list[Path]:
    files: list[Path] = []
    pattern = "**/*" if recursive else "*"
    if mode == "compress":
        for p in root.glob(pattern):
            if p.is_file() and p.suffix != _SNAPR_EXT:
                files.append(p)
    else:
        suffix = f"*{_SNAPR_EXT}"
        pattern = f"**/{suffix}" if recursive else suffix
        for p in root.glob(pattern):
            if p.is_file():
                files.append(p)
    return files


def _snapr_tar_directory(directory: Path, remove_original: bool = True) -> Optional[Path]:
    try:
        archive = directory.with_suffix(".tar")
        print(f"Creating tar archive: {archive}")
        with tarfile.open(archive, "w") as tar:
            tar.add(directory, arcname=directory.name)
        if remove_original:
            import shutil

            shutil.rmtree(directory)
            print(f"Removed original directory: {directory}")
        print(f"Created tar archive: {archive}")
        return archive
    except Exception as exc:
        logger.error(f"Error creating tar archive for {directory}: {exc!s}")
        return None


def _snapr_tar_subdirs(root: Path, remove_original: bool = True) -> list[Path]:
    archives: list[Path] = []
    for child in root.iterdir():
        if child.is_dir():
            archive = _snapr_tar_directory(child, remove_original)
            if archive is not None:
                archives.append(archive)
    return archives


def _snapr_process_files(files: list[Path], mode: str, remove_original: bool = True) -> tuple[int, int]:
    if not files:
        logger.warning(f"No files found to {mode}")
        return 0, 0
    print(f"Processing {len(files)} files with {_SNAPR_WORKERS} workers")
    success = 0
    failed = 0
    tasks = [(fp, mode, remove_original) for fp in files]
    from multiprocessing import Pool

    pool = Pool(processes=_SNAPR_WORKERS)
    try:
        results = [(task[0], pool.apply_async(_snapr_worker, (task,))) for task in tasks]
        for path, async_result in results:
            try:
                ok, msg = async_result.get()
                if ok:
                    success += 1
                else:
                    failed += 1
                    logger.error(f"Failed to process {path}: {msg}")
            except Exception as exc:
                failed += 1
                logger.error(f"Error processing {path}: {exc!s}")
    finally:
        pool.close()
        pool.join()
    return success, failed


def snapr_cli(args: argparse.Namespace) -> int:
    if not _HAS_CRAMJAM:
        print("Error: snapr backend requires 'cramjam' and 'loguru'", file=sys.stderr)
        return 1
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    root = Path(args.directory)
    if not root.exists() or not root.is_dir():
        logger.error(f"Directory not found: {root}")
        return 1
    remove_original = not args.keep_original
    mode = args.operation
    recursive = not args.no_recursive
    print(f"Starting {mode} operation on {root}")
    print(f"Remove original: {remove_original}, Recursive: {recursive}")
    if args.tar and mode == "compress":
        print("Tarring subdirectories...")
        archives = _snapr_tar_subdirs(root, remove_original)
        print(f"Created {len(archives)} tar archives")
    files = _snapr_collect_files(root, mode, recursive)
    if not files:
        logger.warning(f"No files found to {mode}")
        return 0
    print(f"Found {len(files)} files to {mode}")
    ok, bad = _snapr_process_files(files, mode, remove_original)
    print(f"Completed {mode} operation")
    print(f"Success: {ok}, Failed: {bad}")
    return 1 if bad > 0 else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified Snappy compression/decompression toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python merged.py snapper -c .\n"
            "  python merged.py snapper -d /path/to/dir\n"
            "  python merged.py snapr compress /path/to/dir\n"
            "  python merged.py snapr decompress /path/to/dir --tar\n"
        ),
    )
    sub = parser.add_subparsers(dest="backend", required=True)
    p_snapper = sub.add_parser("snapper", help="Pure-Python custom Snappy codec")
    mode = p_snapper.add_mutually_exclusive_group(required=True)
    mode.add_argument("-c", "--compress", action="store_true", help="Compress files")
    mode.add_argument("-d", "--decompress", action="store_true", help="Decompress files")
    p_snapper.add_argument("--keep-original", action="store_true", help="Keep original files (default: remove them)")
    p_snapper.add_argument("paths", nargs="*", type=str, help="Files/directories to process (default: cwd)")
    p_snapper.set_defaults(func=snapper_cli)
    p_snapr = sub.add_parser("snapr", help="cramjam-backed Snappy codec with tar support")
    p_snapr.add_argument("operation", choices=["compress", "decompress"], help="Operation to perform")
    p_snapr.add_argument("directory", type=str, help="Directory to process")
    p_snapr.add_argument("-t", "--tar", action="store_true", help="Tar subdirectories first before compression")
    p_snapr.add_argument("--keep-original", action="store_true", help="Keep original files (default: remove them)")
    p_snapr.add_argument("--no-recursive", action="store_true", help="Do not process subdirectories recursively")
    p_snapr.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    p_snapr.set_defaults(func=snapr_cli)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
