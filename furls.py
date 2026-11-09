#!/data/data/com.termux/files/usr/bin/env python
"""Collect the URLs found in the files and archives of a tree.
Scans the text files and the members of the zip/tar/zst/br archives (nested archives included, size limit via ``-m``), extracts every ``http(s)`` link, separates the GitHub ones and saves ``urls.txt`` plus ``gitlinks.txt``/``repos.txt``.
Usage: furls [-o urls.txt] [-m MAX_MB] [-e .py,.md] [--max-recursion N] Options: -o, --output Output file (one URL per line).
-m, --max-mb -e, --extensions Comma-separated list of file extensions to scan (e.g.
.py,.md).
If empty, all files are scanned.
Applies to archive members too.
--max-recursion Max nested-archive recursion depth (default 999).
Examples: $ furls -o urls.txt --max-mb 5 Command-line script: run ``python furls.py`` or the ``furls`` command.
The module is importable as well."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import tarfile
from tarfile import TarFile
import tempfile
from urllib.parse import urlparse
import zipfile
from zipfile import ZipFile
from typing import Any, Iterable

from dh import append_text, is_valid_url
import zstd


DEFAULT_MAX_MB: int = 15
EXCLUDE_DIRS: set[str] = {".git", "__pycache__"}
URL_RE = re.compile(r"https?://[^\s'\"<>()]+", flags=re.IGNORECASE)
GIT_FILE: Path = Path("gitlinks.txt")
REPO_FILE: Path = Path("repos.txt")
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".tar.zst",
    ".tar",
    ".zip",
    ".whl",
    ".zst",
    ".br",
    ".xz",
    ".gz",
    ".7z",
    ".tar.7z",
    ".tar.br",
    ".tar.7z",
    ".t7z",
    ".tbz",
    "tzz",
)


def should_skip_dir(dirname: str) -> bool:
    return any(part in EXCLUDE_DIRS for part in dirname.split(os.sep))


def find_urls_in_text(text: str):
    found = set()
    for m in URL_RE.findall(text):
        url = m.rstrip(".,;:)]}>\"'")
        if url:
            found.add(url)
    return found


def decode_bytes_to_text(b: int) -> str:
    for enc in ("utf-8", "latin-1", "utf-16"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="ignore")


def scan_bytes_for_urls(b: bytes, max_bytes: bytes, exts: list[Any], name_hint = None):
    if exts is not None and name_hint:
        _, ext = (str(Path(name_hint).with_suffix("")), Path(name_hint).suffix)
        if ext and ext.lower() not in exts:
            return set()
    if len(b) > max_bytes:
        return set()
    text = decode_bytes_to_text(b)
    return find_urls_in_text(text)


def is_archive_name(name: str) -> bool:
    nl = name.lower()
    return any(nl.endswith(suf) for suf in ARCHIVE_SUFFIXES)


def open_tar_from_zst_path(path: Path):
    temp = tempfile.TemporaryFile()
    with Path(path).open("rb") as fh:
        dctx = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(fh)
        try:
            while True:
                chunk = reader.read(16384)
                if not chunk:
                    break
                temp.write(chunk)
        finally:
            with contextlib.suppress(Exception):
                reader.close()
    temp.seek(0)
    try:
        tf = tarfile.open(fileobj=temp, mode="r:*")
        return (tf, temp)
    except Exception:
        with contextlib.suppress(Exception):
            temp.close()
        return (None, None)


def process_zipfile_zipped(zipf: ZipFile, max_bytes: bytes, exts: list[Any], found: list[Any], recursion_depth: int, max_recursion: Any) -> None:
    for zi in zipf.infolist():
        if zi.is_dir():
            continue
        name = zi.filename
        if zi.file_size > max_bytes:
            continue
        try:
            with zipf.open(zi) as member_f:
                b = member_f.read()
        except Exception:
            continue
        if recursion_depth < max_recursion and is_archive_name(name):
            process_bytes_as_archive(b, name, max_bytes, exts, found, recursion_depth + 1, max_recursion)
        else:
            found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))


def process_tarfile_obj(tarf: TarFile, max_bytes: bytes, exts: list[Any], found: list[Any], recursion_depth: int, max_recursion: Any) -> None:
    for member in tarf.getmembers():
        if not member.isfile():
            continue
        name = member.name
        if member.size > max_bytes:
            continue
        try:
            f = tarf.extractfile(member)
            if f is None:
                continue
            b = f.read()
        except Exception:
            continue
        if recursion_depth < max_recursion and is_archive_name(name):
            process_bytes_as_archive(b, name, max_bytes, exts, found, recursion_depth + 1, max_recursion)
        else:
            found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))


def process_bytes_as_archive(b: int, name: str, max_bytes: bytes, exts: list[Any], found: list[Any], recursion_depth: int = 0, max_recursion: int = 3) -> None:
    lname = name.lower()
    bio = io.BytesIO(b)
    try:
        if lname.endswith((".zip", ".whl")):
            try:
                with zipfile.ZipFile(bio) as zf:
                    process_zipfile_zipped(zf, max_bytes, exts, found, recursion_depth, max_recursion)
            except zipfile.BadZipFile:
                found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
            return
        if any(
            lname.endswith(suf)
            for suf in (
                ".tar",
                ".tar.gz",
                ".tgz",
                ".tar.xz",
                ".txz",
                ".tar.bz2",
                ".tbz2",
            )
        ):
            try:
                bio.seek(0)
                with tarfile.open(fileobj=bio, mode="r:*") as tf:
                    process_tarfile_obj(tf, max_bytes, exts, found, recursion_depth, max_recursion)
            except tarfile.ReadError:
                found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
            return
        if lname.endswith(".tar.zst"):
            if zstd is None:
                found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
                return
            try:
                dctx = zstd.ZstdDecompressor()
                with (
                    dctx.stream_reader(io.BytesIO(b)) as reader,
                    tempfile.TemporaryFile() as tmpf,
                ):
                    while True:
                        chunk = reader.read(16384)
                        if not chunk:
                            break
                        tmpf.write(chunk)
                    tmpf.seek(0)
                    try:
                        with tarfile.open(fileobj=tmpf, mode="r:*") as tf:
                            process_tarfile_obj(
                                tf,
                                max_bytes,
                                exts,
                                found,
                                recursion_depth,
                                max_recursion,
                            )
                    except tarfile.ReadError:
                        found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
                return
            except Exception:
                found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
                return
        found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))
    except Exception:
        found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=name))


def process_path(path: str, max_bytes: int, exts: list[Any], found: list[Any], recursion_limit = 999) -> None:
    p = Path(path)
    try:
        size = p.stat().st_size
    except Exception:
        return
    if size > max_bytes and (not is_archive_name(path)):
        return
    lname = path.lower()
    try:
        if any(lname.endswith(suf) for suf in (".zip", ".whl")):
            try:
                with ZipFile(p) as zf:
                    process_zipfile_zipped(zf, max_bytes, exts, found, 0, recursion_limit)
                return
            except zipfile.BadZipFile:
                pass
        if any(
            lname.endswith(suf)
            for suf in (
                ".tar",
                ".tar.gz",
                ".tgz",
                ".tar.xz",
                ".txz",
                ".tar.bz2",
                ".tbz2",
            )
        ):
            try:
                with tarfile.open(p, mode="r:*") as tf:
                    process_tarfile_obj(tf, max_bytes, exts, found, 0, recursion_limit)
                return
            except (tarfile.ReadError, EOFError):
                pass
        if lname.endswith(".tar.zst"):
            if zstd is None:
                try:
                    b = p.read_bytes()[: max_bytes + 1]
                    found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=path))
                except Exception:
                    pass
                return
            tf, tmpf = open_tar_from_zst_path(path)
            if tf is None:
                return
            try:
                process_tarfile_obj(tf, max_bytes, exts, found, 0, recursion_limit)
            finally:
                with contextlib.suppress(Exception):
                    tf.close()
                with contextlib.suppress(Exception):
                    tmpf.close()
            return
        b = p.read_bytes()[: max_bytes + 1]
        found.update(scan_bytes_for_urls(b, max_bytes, exts, found=found, name_hint=path))
    except TypeError:
        try:
            b = p.read_bytes()[: max_bytes + 1]
            found.update(scan_bytes_for_urls(b, max_bytes, exts, name_hint=path))
        except Exception:
            return
    except Exception:
        return


def is_github_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return "github.com" in result.netloc
    except:
        return False


def extract_git_repos(urls: Iterable[Any]):
    github_regex = re.compile(r"https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:[/?#]|$)")
    return list({f"{m.group(1)}/{m.group(2)}" for url in urls if (m := github_regex.search(url))})


def extract_and_save_gitlinks(urllist: Iterable[Any]) -> None:
    glinks = []
    for url in urllist:
        if is_github_url(url):
            glinks.append(url)
    repoz = extract_git_repos(glinks)
    if repoz:
        repos = "\n".join(repoz)
        append_text(REPO_FILE, repos)
        git_links = "\n".join(glinks)
        append_text(GIT_FILE, git_links)
    else:
        print("no git link")


def iter_files(root: Path):
    root = root.resolve()
    for current_dir, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        if should_skip_dir(current_dir):
            continue
        for fname in filenames:
            p = Path(current_dir) / fname
            if not p.is_symlink():
                yield p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find URLs in files and supported archives recursively and save them to a file."
    )
    parser.add_argument("-o", "--output", default="urls.txt", help="Output file (one URL per line).")
    parser.add_argument(
        "-m",
        "--max-mb",
        type=float,
        default=DEFAULT_MAX_MB,
        help=f"Max file/member size to scan in MB (default {DEFAULT_MAX_MB}).",
    )
    parser.add_argument(
        "-e",
        "--extensions",
        default="",
        help="Comma-separated list of file extensions to scan (e.g. .py,.md). If empty, all files are scanned. Applies to archive members too.",
    )
    parser.add_argument(
        "--max-recursion",
        type=int,
        default=999,
        help="Max nested-archive recursion depth (default 999).",
    )
    args = parser.parse_args()
    max_bytes = int(args.max_mb * 1024 * 1024)
    exts = {e.strip().lower() for e in args.extensions.split(",") if e.strip()} if args.extensions else None
    found = set()
    for p in iter_files(Path()):
        print(f"processing {p.name}")
        process_path(str(p), max_bytes, exts, found, recursion_limit=args.max_recursion)
    if not found:
        print("no url found")
        sys.exit(0)
    sorted_urls = sorted(found)
    extract_and_save_gitlinks(sorted_urls)
    out_path = Path(args.output)
    try:
        if out_path.exists():
            print("urls.txt exists. appending new urls")
            with out_path.open("a", encoding="utf-8") as out:
                out.write("\n\n")
                for u in sorted_urls:
                    if is_valid_url(u):
                        out.write(u + "\n")
        else:
            with out_path.open("w", encoding="utf-8") as out:
                for u in sorted_urls:
                    if is_valid_url(u):
                        out.write(u + "\n")
        print(f"Wrote {len(sorted_urls)} unique URLs to {args.output}")
        any(p.endswith(".tar.zst") for p in sorted_urls)
    except OSError as e:
        print(f"Error writing output file: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
