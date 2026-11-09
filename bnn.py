#!/data/data/com.termux/files/usr/bin/env python
"""Replace the literal ``\n`` sequences of text files with real newlines.
Streams each text file in chunks (1 MB by default, ``-c`` changes it), replaces the escapes with line breaks through joblib in parallel and writes the result back with a per-file byte count.
Usage: bnn [paths ...] [-c CHUNK] [--include-binary] Positional arguments: inputs ...
Files or directories to process (default: current directory) Options: -c, --chunk-size Chunk size for streaming (default: 1M, e.g., 512K, 2M) --include-binary Process binary files (not recommended) -v, --verbose Show detailed logging Examples: $ bnn dump.txt Command-line script: run ``python bnn.py`` or the ``bnn`` command.
The module is importable as well."""

import argparse
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING

from dh import MAX_WORKERS
from joblib import Parallel, delayed
from loguru import logger


@dataclass
class FileStats:
    path: Path
    success: bool
    replacements: int = 0
    original_size: int = 0
    new_size: int = 0
    error_msg: str | None = None

    def __str__(self) -> str:
        try:
            relpath = self.path.relative_to(Path.cwd())
        except ValueError:
            relpath = self.path
        if not self.success:
            return f"✗ {relpath}: {self.error_msg}"
        size_delta = self.new_size - self.original_size
        size_change = f"({size_delta:+d} bytes)" if size_delta != 0 else "(no size change)"
        return f"✓ {relpath}: {self.replacements} replacements {size_change}"


def is_text_file(path: Path, max_sample: int = 8192) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(max_sample)
            return b"\x00" not in chunk
    except OSError:
        return False


def should_process_file(path: Path, text_only: bool = True) -> bool:
    if path.is_dir() or path.is_symlink():
        return False
    return not (text_only and not is_text_file(path))


def collect_files(inputs: list[str | Path]) -> Generator[Path, None, None]:
    for input_path in inputs:
        path = Path(input_path).resolve()
        if path.is_file():
            if should_process_file(path):
                yield path
        elif path.is_dir():
            for path in path.rglob("*"):
                if should_process_file(path):
                    yield path
        else:
            logger.warning(f"Path not found: {path}")


def process_file_chunked(
    path: Path,
    chunk_size: int = 1024 * 1024,
) -> FileStats:
    stats = FileStats(path=path, success=True)
    if chunk_size <= 0:
        stats.success = False
        stats.error_msg = "Chunk size must be greater than zero"
        return stats
    temp_path: Path | None = None
    try:
        stats.original_size = path.stat().st_size
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            encoding="utf-8",
            newline="",
        ) as temp_file:
            temp_path = Path(temp_file.name)
            try:
                total_replacements = 0
                with open(path, "r", encoding="utf-8", errors="strict", newline="") as f:
                    buffer = ""
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            if buffer:
                                new_buffer, count = (
                                    buffer.replace("\\n", "\n"),
                                    buffer.count("\\n"),
                                )
                                temp_file.write(new_buffer)
                                total_replacements += count
                            break
                        buffer += chunk
                        last_safe_pos = len(buffer)
                        if buffer.endswith("\\"):
                            last_safe_pos -= 1
                        processable = buffer[:last_safe_pos]
                        buffer = buffer[last_safe_pos:]
                        new_content, count = (
                            processable.replace("\\n", "\n"),
                            processable.count("\\n"),
                        )
                        temp_file.write(new_content)
                        total_replacements += count
                stats.replacements = total_replacements
            except Exception as e:
                temp_path.unlink()
                raise
            temp_path.chmod(path.stat().st_mode)
        stats.new_size = temp_path.stat().st_size
        shutil.move(str(temp_path), str(path))
    except UnicodeDecodeError as e:
        stats.success = False
        stats.error_msg = f"Encoding error: {e}"
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    except PermissionError as e:
        stats.success = False
        stats.error_msg = f"Permission denied: {e}"
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    except Exception as e:
        stats.success = False
        stats.error_msg = f"Error: {type(e).__name__}: {e}"
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace literal \\n with actual newlines in files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s file.txt
  %(prog)s dir1 dir2 file.txt
  %(prog)s --workers 8 --chunk-size 2M dir/
        """,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Files or directories to process (default: current directory)",
    )
    parser.add_argument(
        "-c",
        "--chunk-size",
        default="1M",
        help="Chunk size for streaming (default: 1M, e.g., 512K, 2M)",
    )
    parser.add_argument(
        "--include-binary",
        action="store_true",
        help="Process binary files (not recommended)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed logging")
    args = parser.parse_args()
    if args.verbose:
        pass
    chunk_size_str = args.chunk_size.upper()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    try:
        if chunk_size_str[-1] in multipliers:
            chunk_size = int(chunk_size_str[:-1]) * multipliers[chunk_size_str[-1]]
        else:
            chunk_size = int(chunk_size_str)
    except (IndexError, ValueError):
        logger.error(f"Invalid chunk size: {args.chunk_size}")
        return 1
    if chunk_size <= 0:
        logger.error("Chunk size must be greater than zero")
        return 1
    if not args.inputs:
        inputs = [Path.cwd()]
        print("No inputs provided, processing current directory recursively")
    else:
        inputs = args.inputs
    files = list(collect_files(inputs))
    if not files:
        logger.warning("No files found to process")
        return 0
    print(f"Found {len(files)} files to process")
    print(f"Starting parallel processing with {MAX_WORKERS} workers")
    results = Parallel(n_jobs=MAX_WORKERS, verbose=0)(delayed(process_file_chunked)(path, chunk_size) for path in files)
    print("\n" + "=" * 40)
    print("PROCESSING RESULTS")
    print("=" * 40)
    successful = 0
    total_replacements = 0
    total_size_change = 0
    for stats in results:
        print(stats)
        if stats.success:
            successful += 1
            total_replacements += stats.replacements
            total_size_change += stats.new_size - stats.original_size
    failed = len(results) - successful
    print("=" * 40)
    print(f"Summary: {successful} succeeded, {failed} failed out of {len(results)} files")
    print(f"Total replacements: {total_replacements}")
    print(f"Total size change: {total_size_change:+d} bytes")
    print("=" * 40 + "\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
