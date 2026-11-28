#!/data/data/com.termux/files/usr/bin/env python
"""ripgrep-like search across files, compressed archives, and removed Git files (Python 3.14+)."""

from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import gzip
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from typing import Any, Optional, Sequence
import zipfile

from compression import zstd


TAR_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".tzst",
    ".tar",
)
ZIP_SUFFIXES: tuple[str, str, str, str] = (".zip", ".whl", ".apk", ".jar")
GZIP_SINGLE_SUFFIXES: tuple[str,] = (".gz",)
ZSTD_SINGLE_SUFFIXES: tuple[str,] = (".zst",)


@dataclass(frozen=True)
class SearchConfig:
    pattern: re.Pattern[str]
    max_filesize: int = 25 * 1024 * 1024  # 25 MB per entry/file
    search_archives: bool = True
    search_git_deleted: bool = True
    max_commits: Optional[int] = None


@dataclass(frozen=True)
class MatchResult:
    source: str
    line_no: int
    line: str


def compile_pattern(
    raw_pattern: str,
    case_sensitive: bool,
    smart_case: bool,
    fixed_strings: bool,
) -> re.Pattern[str]:
    flags = re.MULTILINE
    expr = re.escape(raw_pattern) if fixed_strings else raw_pattern

    if case_sensitive:
        pass
    elif smart_case and any(ch.isupper() for ch in raw_pattern):
        pass
    else:
        flags |= re.IGNORECASE

    return re.compile(expr, flags)


def is_probably_binary(data: bytes, sample_size: int = 4096) -> bool:
    if not data:
        return False
    chunk = data[:sample_size]
    if b"\x00" in chunk:
        return True
    non_text = sum(1 for b in chunk if b < 7 or (14 <= b <= 31))
    return (non_text / len(chunk)) > 0.30


def search_text_bytes(
    data: bytes, source_label: str, cfg: SearchConfig
) -> list[MatchResult]:
    if len(data) > cfg.max_filesize or is_probably_binary(data):
        return []
    text = data.decode("utf-8", errors="replace")
    results: list[MatchResult] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if cfg.pattern.search(line):
            results.append(MatchResult(source=source_label, line_no=idx, line=line))
    return results


def is_tar_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suf) for suf in TAR_SUFFIXES)


def is_zip_archive(path: Path) -> bool:
    return path.name.lower().endswith(ZIP_SUFFIXES)


def is_single_gz(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(GZIP_SINGLE_SUFFIXES) and not name.endswith(".tar.gz")


def is_single_zst(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(ZSTD_SINGLE_SUFFIXES) and not name.endswith(
        (".tar.zst", ".tzst")
    )


def is_archive_file(path: Path) -> bool:
    return (
        is_zip_archive(path)
        or is_tar_archive(path)
        or is_single_gz(path)
        or is_single_zst(path)
    )


def search_archive_file(path: Path, cfg: SearchConfig) -> list[MatchResult]:
    results: list[MatchResult] = []
    try:
        if is_zip_archive(path):
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > cfg.max_filesize:
                        continue
                    with zf.open(info, "r") as fh:
                        data = fh.read(cfg.max_filesize + 1)
                    label = f"{path}::{info.filename}"
                    results.extend(search_text_bytes(data, label, cfg))

        elif is_tar_archive(path):
            with tarfile.open(path, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or member.size > cfg.max_filesize:
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read(cfg.max_filesize + 1)
                    label = f"{path}::{member.name}"
                    results.extend(search_text_bytes(data, label, cfg))

        elif is_single_gz(path):
            if path.stat().st_size > cfg.max_filesize * 4:
                return []
            with gzip.open(path, "rb") as gf:
                data = gf.read(cfg.max_filesize + 1)
            inner_name = path.name[:-3] or "decompressed"
            label = f"{path}::{inner_name}"
            results.extend(search_text_bytes(data, label, cfg))

        elif is_single_zst(path):
            if path.stat().st_size > cfg.max_filesize * 4:
                return []
            with zstd.open(path, "rb") as zf:
                data = zf.read(cfg.max_filesize + 1)
            inner_name = path.name[:-4] or "decompressed"
            label = f"{path}::{inner_name}"
            results.extend(search_text_bytes(data, label, cfg))

    except (zipfile.BadZipFile, tarfile.TarError, zstd.ZstdError, EOFError, OSError):
        pass

    return results


def search_regular_file(path: Path, cfg: SearchConfig) -> list[MatchResult]:
    try:
        st = path.stat()
        if st.st_size > cfg.max_filesize:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    return search_text_bytes(data, str(path), cfg)


def discover_targets(
    roots: list[Path], cfg: SearchConfig
) -> tuple[list[Path], list[Path]]:
    """Return (regular_or_archive_files, git_repo_roots)."""
    files: list[Path] = []
    git_repos: list[Path] = []
    seen_repos: set[Path] = set()

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current_dir = Path(dirpath)

            if cfg.search_git_deleted and ".git" in dirnames:
                repo_root = current_dir.resolve()
                if repo_root not in seen_repos:
                    seen_repos.add(repo_root)
                    git_repos.append(repo_root)

            dirnames[:] = [d for d in dirnames if d != ".git"]

            for fname in filenames:
                files.append(current_dir / fname)

    return files, git_repos


def search_git_deleted_files(repo_root: Path, cfg: SearchConfig) -> list[MatchResult]:
    """Search content of files that were deleted in Git commits within repo_root."""
    results: list[MatchResult] = []
    cmd = ["git", "-C", str(repo_root), "rev-list", "--all"]
    if cfg.max_commits is not None:
        cmd.insert(4, f"-n{cfg.max_commits}")

    try:
        rev_proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []

    commits = [line.strip() for line in rev_proc.stdout.splitlines() if line.strip()]
    checked_entries: set[tuple[str, str]] = set()

    for commit in commits:
        name_cmd = [
            "git",
            "-C",
            str(repo_root),
            "diff-tree",
            "--no-commit-id",
            "--diff-filter=D",
            "--name-only",
            "-r",
            "-z",
            commit,
        ]
        try:
            name_proc = subprocess.run(
                name_cmd, capture_output=True, text=True, check=True
            )
        except subprocess.SubprocessError:
            continue

        deleted_paths = [p for p in name_proc.stdout.split("\x00") if p]
        if not deleted_paths:
            continue

        for rel_path in deleted_paths:
            key = (commit, rel_path)
            if key in checked_entries:
                continue
            checked_entries.add(key)

            blob_ref = f"{commit}^:{rel_path}"
            try:
                show_proc = subprocess.run(
                    ["git", "-C", str(repo_root), "show", blob_ref],
                    capture_output=True,
                    check=True,
                )
            except subprocess.SubprocessError:
                continue

            label = f"{repo_root} [deleted@{commit[:8]}]:{rel_path}"
            results.extend(search_text_bytes(show_proc.stdout, label, cfg))

    return results


def _worker_file(args: tuple[Path, SearchConfig]) -> list[MatchResult]:
    path, cfg = args
    if cfg.search_archives and is_archive_file(path):
        return search_archive_file(path, cfg)
    return search_regular_file(path, cfg)


def _worker_git(args: tuple[Path, SearchConfig]) -> list[MatchResult]:
    repo_root, cfg = args
    return search_git_deleted_files(repo_root, cfg)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search files, compressed archives (including zstd), and removed Git files using regex."
    )
    parser.add_argument("pattern", help="Search pattern (regex by default)")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to search (default: current directory)",
    )
    parser.add_argument(
        "-i", "--ignore-case", action="store_true", help="Case-insensitive search"
    )
    parser.add_argument(
        "-s", "--case-sensitive", action="store_true", help="Case-sensitive search"
    )
    parser.add_argument(
        "-S",
        "--smart-case",
        action="store_true",
        help="Case-sensitive if pattern has uppercase",
    )
    parser.add_argument(
        "-F",
        "--fixed-strings",
        action="store_true",
        help="Treat pattern as a literal string",
    )
    parser.add_argument(
        "--no-archives", action="store_true", help="Skip compressed archives"
    )
    parser.add_argument(
        "--no-git-deleted",
        action="store_true",
        help="Skip files removed in Git history",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=None,
        help="Limit Git commits inspected per repo",
    )
    parser.add_argument(
        "--max-filesize",
        type=int,
        default=25 * 1024 * 1024,
        help="Max file/entry size in bytes to read (default: 25MB)",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=os.cpu_count() or 4, help="Worker processes"
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    if sys.version_info < (3, 14):
        print(
            "Python 3.14+ is required for stdlib compression.zstd support.",
            file=sys.stderr,
        )
        return 2

    args = parse_args(argv)

    try:
        regex = compile_pattern(
            raw_pattern=args.pattern,
            case_sensitive=args.case_sensitive,
            smart_case=args.smart_case,
            fixed_strings=args.fixed_strings,
        )
    except re.error as exc:
        print(f"Invalid regex: {exc}", file=sys.stderr)
        return 2

    cfg = SearchConfig(
        pattern=regex,
        max_filesize=args.max_filesize,
        search_archives=not args.no_archives,
        search_git_deleted=not args.no_git_deleted,
        max_commits=args.max_commits,
    )

    roots = [Path(p) for p in args.paths]
    files, git_repos = discover_targets(roots, cfg)

    any_match = False

    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = []
        for f in files:
            futures.append(pool.submit(_worker_file, (f, cfg)))
        for repo in git_repos:
            futures.append(pool.submit(_worker_git, (repo, cfg)))

        for fut in as_completed(futures):
            for m in fut.result():
                any_match = True
                print(f"{m.source}:{m.line_no}:{m.line}")

    return 0 if any_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
