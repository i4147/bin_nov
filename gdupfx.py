#!/data/data/com.termux/files/usr/bin/env python
"""Fast xxhash-based duplicate file finder.
Pre-hashes the file heads, confirms with a 128 KB-chunked xxhash, keeps one copy per set and only deletes with the explicit ``-r``; ``-l`` lists the duplicates and ``-b`` benchmarks xxhash against hashlib.
Usage: gdupfx [PATH] [-r] [-l] [-b] Positional arguments: path Target directory path to search (defaults to current directory '.').
Options: -r, --remove Explicit flag required to delete duplicate files.
Keeps 1 original per set.
-l, --list List all duplicate file paths found in detail.
-b, --benchmark Run speed benchmark comparing xxHash against hashlib.
Examples: $ gdupfx ~/Downloads -l Command-line script: run ``python gdupfx.py`` or the ``gdupfx`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import sys
import time
from typing import Optional

from dh import MAX_WORKERS, SKIP_DIRS


try:
    import xxhash
except ImportError:
    print(
        "Error: 'xxhash' library is required. Install it using: pip install xxhash",
        file=sys.stderr,
    )
    sys.exit(1)
CHUNK_SIZE: int = 128 * 1024
PARTIAL_SIZE: int = 8 * 1024


def format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.2f} {units[i]}"


def get_partial_hash(filepath: str) -> tuple[str, Optional[str]]:
    try:
        with open(filepath, "rb") as f:
            header_chunk = f.read(PARTIAL_SIZE)
            return filepath, xxhash.xxh64(header_chunk).hexdigest()
    except (OSError, PermissionError):
        return filepath, None


def get_full_hash(filepath: str) -> tuple[str, Optional[str]]:
    try:
        hasher = xxhash.xxh64()
        with open(filepath, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                hasher.update(chunk)
        return filepath, hasher.hexdigest()
    except (OSError, PermissionError):
        return filepath, None


def get_full_hashlib_md5(filepath: str) -> tuple[str, Optional[str]]:
    try:
        with open(filepath, "rb") as f:
            if hasattr(hashlib, "file_digest"):
                return filepath, hashlib.file_digest(f, "md5").hexdigest()
            hasher = hashlib.md5()
            while chunk := f.read(CHUNK_SIZE):
                hasher.update(chunk)
            return filepath, hasher.hexdigest()
    except (OSError, PermissionError):
        return filepath, None


@dataclass
class Finder:
    path: Path
    same_content: dict[str, list[str]] = field(default_factory=dict)
    all_files: list[tuple[str, int]] = field(default_factory=list)
    total_files_count: int = 0
    total_files_size: int = 0
    dup_count: int = 0
    dup_size: int = 0

    def scan_and_find_duplicates(self) -> list[str]:
        target_path = self.path.expanduser().resolve()
        file_sizes: dict[int, list[str]] = defaultdict(list)
        print(f"Scanning '{target_path}'...", end="", flush=True)
        dirs = [str(target_path)]
        while dirs:
            curr_dir = dirs.pop()
            try:
                with os.scandir(curr_dir) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in SKIP_DIRS:
                                    continue
                                dirs.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                stat = entry.stat(follow_symlinks=False)
                                file_sizes[stat.st_size].append(entry.path)
                                self.all_files.append((entry.path, stat.st_size))
                                self.total_files_count += 1
                                self.total_files_size += stat.st_size
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError):
                continue
        print(" Done.")
        candidate_groups = [(size, paths) for size, paths in file_sizes.items() if len(paths) > 1]
        if not candidate_groups:
            self.print_summary()
            return []
        all_candidate_paths = [p for _, paths in candidate_groups for p in paths]
        path_to_size = {path: size for size, paths in candidate_groups for path in paths}
        partial_hash_groups: dict[tuple[int, str], list[str]] = defaultdict(list)
        total_candidates = len(all_candidate_paths)
        print(
            f"Running partial xxHash check ({total_candidates} candidates)...",
            end="",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(get_partial_hash, path) for path in all_candidate_paths]
            for future in as_completed(futures):
                filepath, p_hash = future.result()
                if p_hash:
                    size = path_to_size[filepath]
                    partial_hash_groups[(size, p_hash)].append(filepath)
        print(" Done.")
        full_hash_queue: list[tuple[int, list[str]]] = [
            (size, paths) for (size, _), paths in partial_hash_groups.items() if len(paths) > 1
        ]
        all_full_candidates = [p for _, paths in full_hash_queue for p in paths]
        if all_full_candidates:
            full_path_to_size = {path: size for size, paths in full_hash_queue for path in paths}
            full_hash_groups: dict[str, list[str]] = defaultdict(list)
            total_full = len(all_full_candidates)
            print(
                f"Running full xxHash verification ({total_full} candidates)...",
                end="",
                flush=True,
            )
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(get_full_hash, path) for path in all_full_candidates]
                for future in as_completed(futures):
                    filepath, f_hash = future.result()
                    if f_hash:
                        full_hash_groups[f_hash].append(filepath)
            print(" Done.")
            for f_hash, paths in full_hash_groups.items():
                if len(paths) > 1:
                    self.same_content[f_hash] = paths
                    sample_size = full_path_to_size[paths[0]]
                    self.dup_count += len(paths) - 1
                    self.dup_size += (len(paths) - 1) * sample_size
        self.print_summary()
        return all_full_candidates

    def print_summary(self) -> None:
        print("\n=== Scan Complete ===")
        print(f"Total files scanned: {self.total_files_count} ({format_size(self.total_files_size)})")
        print(f"Duplicates found:    {self.dup_count} file(s) wasting {format_size(self.dup_size)}")

    def list_duplicates(self) -> None:
        if not self.same_content:
            print("\nNo duplicates found to list.")
            return
        print("\n--- Duplicated File Groups ---")
        for group_idx, (_, filenames) in enumerate(self.same_content.items(), 1):
            sorted_files = sorted(filenames)
            print(f"\nGroup {group_idx}:")
            print(f"  Original (Kept): {sorted_files[0]}")
            for dup in sorted_files[1:]:
                print(f"  Duplicate (Target): {dup}")

    def process_deletions(self, remove: bool = False) -> None:
        if not self.same_content:
            print("\nNo duplicates found to process.")
            return
        dry_run = not remove
        status_label = "[REMOVING]" if remove else "[DRY-RUN]"
        print(f"\n{status_label} Processing duplicate files...")
        removed_count = 0
        for filenames in self.same_content.values():
            sorted_files = sorted(filenames)
            for target_file in sorted_files[1:]:
                if dry_run:
                    print(f"  Would delete: {target_file}")
                else:
                    try:
                        os.remove(target_file)
                        print(f"  Deleted: {target_file}")
                        removed_count += 1
                    except OSError as err:
                        print(f"  Failed to delete {target_file}: {err}")
        if dry_run:
            print("\n[DRY-RUN COMPLETE] No files were deleted. Pass '-r' or '--remove' to execute removal.")
        else:
            print(f"\n[REMOVAL COMPLETE] Successfully deleted {removed_count} duplicate file(s).")

    def run_benchmark(self, candidate_files: list[str]) -> None:
        sample_files = candidate_files
        if not sample_files:
            sample_files = [p for p, s in self.all_files if s > 0][:100]
        if not sample_files:
            print("\n[BENCHMARK] No suitable non-empty files found for benchmarking.")
            return
        total_bytes = 0
        valid_files = []
        for path in sample_files:
            try:
                sz = Path(path).stat().st_size
                total_bytes += sz
                valid_files.append(path)
            except OSError:
                continue
        if not valid_files:
            print("\n[BENCHMARK] Could not access sample files for benchmarking.")
            return
        print("\n=== BENCHMARK COMPARISON ===")
        print(f"Testing on {len(valid_files)} file(s) ({format_size(total_bytes)} total data)...")
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            list(executor.map(get_full_hash, valid_files))
        t_xxhash = time.perf_counter() - t0
        mb_xxhash = (total_bytes / (1024 * 1024)) / (t_xxhash or 0.00001)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            list(executor.map(get_full_hashlib_md5, valid_files))
        t_md5 = time.perf_counter() - t0
        mb_md5 = (total_bytes / (1024 * 1024)) / (t_md5 or 0.00001)
        speedup = (t_md5 / t_xxhash) if t_xxhash > 0 else 1.0
        print(f"\n  Hasher Engine     Time Elapsed    Throughput (MB/s)")
        print(f"  ---------------------------------------------------")
        print(f"  xxHash (xxh64)    {t_xxhash:8.4f}s       {mb_xxhash:10.2f} MB/s")
        print(f"  hashlib (MD5)     {t_md5:8.4f}s       {mb_md5:10.2f} MB/s")
        print(f"  ---------------------------------------------------")
        print(f"  Result: xxHash was {speedup:.2f}x faster than hashlib MD5\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fast duplicate file finder powered by xxHash (Parallel I/O, Dry-Run default)."
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path(),
        help="Target directory path to search (defaults to current directory '.').",
    )
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Explicit flag required to delete duplicate files. Keeps 1 original per set.",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List all duplicate file paths found in detail.",
    )
    parser.add_argument(
        "-b",
        "--benchmark",
        action="store_true",
        help="Run speed benchmark comparing xxHash against hashlib.",
    )
    args = parser.parse_args()
    if not args.path.is_dir():
        print(f"Error: Path '{args.path}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)
    finder = Finder(args.path)
    candidates = finder.scan_and_find_duplicates()
    if args.list:
        finder.list_duplicates()
    finder.process_deletions(remove=args.remove)
    if args.benchmark:
        finder.run_benchmark(candidates)


if __name__ == "__main__":
    main()
