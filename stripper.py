#!/data/data/com.termux/files/usr/bin/env python
"""
strip comments from files that use # as comment char

"""

import argparse
import codecs
import io
import multiprocessing as mp
import os
import stat
import sys
import tempfile
from collections import deque
from collections.abc import Iterable, Iterator, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Final, IO

from rich import filesize
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dh import SKIP_DIRS

PROBE_BYTES: Final[int] = 8192
COPY_CHUNK_BYTES: Final[int] = 65536


@dataclass(frozen=True, slots=True)
class CliConfig:
    targets: tuple[Path, ...]
    jobs: int


def walk_files(
    roots: Iterable[Path],
    skip_dirs: Set[str] = SKIP_DIRS,
) -> Iterator[Path]:
    stack: deque[Path] = deque()
    for root in roots:
        try:
            if root.is_symlink():
                continue
            if root.is_file():
                yield root
            elif root.is_dir() and root.name not in skip_dirs:
                stack.append(root)
        except OSError:
            continue

    while stack:
        current = stack.pop()
        try:
            entries = current.iterdir()
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in skip_dirs:
                        stack.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def strip_line_comment(
    line: str,
    in_quote: str | None,
    is_first_line: bool,
) -> tuple[str, str | None]:
    if is_first_line and line.lstrip("\ufeff").startswith("#!"):
        return line, in_quote

    body, ending = split_line_ending(line)

    if in_quote is None and "#" not in body and '"""' not in body and "'''" not in body and not body.endswith("\\"):
        return line, None

    if in_quote is not None and len(in_quote) == 3 and in_quote not in body and not body.endswith("\\"):
        return line, in_quote

    i = 0
    n = len(body)
    comment_pos: int | None = None
    escaped_eol = False

    while i < n:
        ch = body[i]
        if ch == "\\":
            if i + 1 < n:
                i += 2
                continue
            escaped_eol = True
            i += 1
            continue

        if in_quote is None:
            if body.startswith('"""', i) or body.startswith("'''", i):
                in_quote = body[i : i + 3]
                i += 3
                continue
            if ch in ('"', "'"):
                in_quote = ch
                i += 1
                continue
            if ch == "#":
                if (i > 0 and body[i - 1] == "$") or (i >= 2 and body[i - 2 : i] == "${"):
                    i += 1
                    continue
                comment_pos = i
                break
        elif len(in_quote) == 3:
            if body.startswith(in_quote, i):
                in_quote = None
                i += 3
                continue
        elif ch == in_quote:
            in_quote = None
            i += 1
            continue

        i += 1

    if in_quote in ('"', "'") and not escaped_eol:
        in_quote = None

    if comment_pos is None:
        return line, in_quote

    prefix = body[:comment_pos]
    if not prefix.strip():
        return "", in_quote
    return prefix.rstrip(" \t") + ending, in_quote


def is_valid_text_head(raw: IO[bytes]) -> bool:
    head = raw.read(PROBE_BYTES)
    if not head or b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(head, final=len(head) < PROBE_BYTES)
    except UnicodeDecodeError:
        return False
    raw.seek(0)
    return True


def copy_exact_bytes(src_path: Path, dst: IO[bytes], count: int) -> None:
    remaining = count
    with src_path.open("rb") as src:
        while remaining > 0:
            chunk = src.read(min(COPY_CHUNK_BYTES, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)


def process_file(path: Path) -> tuple[Path, int]:
    try:
        st = path.stat(follow_symlinks=False)
    except OSError:
        return path, 0

    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return path, 0

    tmp_file: IO[bytes] | None = None
    tmp_path: Path | None = None
    replaced = False
    prefix_bytes = 0
    original_bytes = 0
    written_bytes = 0
    in_quote: str | None = None

    try:
        with path.open("rb") as raw:
            if not is_valid_text_head(raw):
                return path, 0

            reader = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            try:
                for line_idx, line in enumerate(reader):
                    if "\x00" in line:
                        return path, 0

                    cleaned, in_quote = strip_line_comment(line, in_quote, line_idx == 0)
                    orig_encoded = line.encode("utf-8")

                    if tmp_file is None:
                        if cleaned == line:
                            prefix_bytes += len(orig_encoded)
                            continue

                        tmp_file = tempfile.NamedTemporaryFile(
                            mode="wb",
                            dir=path.parent,
                            prefix=f".{path.name}.",
                            suffix=".tmp",
                            delete=False,
                        )
                        tmp_path = Path(tmp_file.name)
                        os.chmod(tmp_file.fileno(), stat.S_IMODE(st.st_mode))
                        if prefix_bytes > 0:
                            copy_exact_bytes(path, tmp_file, prefix_bytes)
                        original_bytes = prefix_bytes
                        written_bytes = prefix_bytes

                    clean_encoded = cleaned.encode("utf-8")
                    tmp_file.write(clean_encoded)
                    original_bytes += len(orig_encoded)
                    written_bytes += len(clean_encoded)
            finally:
                reader.detach()

        if tmp_file is None or tmp_path is None:
            return path, 0

        bytes_reduced = original_bytes - written_bytes
        if bytes_reduced <= 0:
            return path, 0

        tmp_file.flush()
        tmp_file.close()
        tmp_path.replace(path)
        replaced = True
        return path, bytes_reduced
    except (OSError, UnicodeDecodeError):
        return path, 0
    finally:
        if tmp_file is not None and not tmp_file.closed:
            try:
                tmp_file.close()
            except OSError:
                pass
        if tmp_path is not None and not replaced:
            tmp_path.unlink(missing_ok=True)


def render_row(
    console: Console,
    index: int,
    total: int,
    counter_width: int,
    file_path: Path,
    bytes_reduced: int,
) -> None:
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=counter_width, no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)

    size_style = "green" if bytes_reduced > 0 else "dim"
    grid.add_row(
        Text(f"[{index}/{total}]"),
        Text(str(file_path)),
        Text(str(bytes_reduced), style=size_style),
    )
    console.print(grid)


def format_freed_space(total_bytes: int) -> str:
    if total_bytes < 1000:
        unit = "byte" if total_bytes == 1 else "bytes"
        return f"{total_bytes} {unit}"
    return f"{total_bytes:,} bytes ({filesize.decimal(total_bytes)})"


def deduplicate_inputs(paths: Sequence[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        norm = os.path.normpath(path)
        if norm not in seen:
            seen.add(norm)
            unique.append(path)
    return tuple(unique)


def parse_args(argv: Sequence[str] | None = None) -> CliConfig:
    parser = argparse.ArgumentParser(description="Remove #-style comments from files in-place.")
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        default=[Path(".")],
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=max(1, mp.cpu_count()),
    )
    ns = parser.parse_args(argv)
    return CliConfig(
        targets=deduplicate_inputs(ns.files),
        jobs=max(1, int(ns.jobs)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    console = Console()

    total_files = sum(1 for _ in walk_files(config.targets))
    if total_files == 0:
        console.print(Text("Total space freed: 0 bytes", style="dim"))
        return 0

    counter_width = len(f"[{total_files}/{total_files}]")
    workers = max(1, min(config.jobs, total_files))
    chunksize = max(1, min(32, total_files // (workers * 4) or 1))
    total_freed = 0

    try:
        with mp.Pool(processes=workers) as pool:
            stream = pool.imap_unordered(
                process_file,
                walk_files(config.targets),
                chunksize=chunksize,
            )
            for idx, (file_path, reduced) in enumerate(stream, start=1):
                total_freed += reduced
                render_row(
                    console,
                    idx,
                    max(idx, total_files),
                    counter_width,
                    file_path,
                    reduced,
                )
    except KeyboardInterrupt:
        console.print(Text("\nInterrupted.", style="yellow"))
        return 130

    summary_style = "bold green" if total_freed > 0 else "dim"
    console.print(
        Text(
            f"Total space freed: {format_freed_space(total_freed)}",
            style=summary_style,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
