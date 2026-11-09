#!/data/data/com.termux/files/usr/bin/env python
"""Comment out (or un-comment) a line range of a file.
Applies the comment prefix that matches the file type (``#``, ``//``, ``--``, ``"`` ...), accepts ranges and stdin (``-``), and offers ``-r`` to un-comment, ``--dry-run``, ``--output``, ``--backup``, ``--encoding`` and ``--preserve-shebang``; the work is chunked across a pool.
Usage: cout FILE START [END] [-r] [--dry-run] [--backup] Positional arguments: filename Path to file, or '-' for stdin/stdout.
start_line Start line (1-based).
end_line End line (inclusive); defaults to EOF.
Options: -r, --remove Remove the comment prefix from target lines.
-n, --dry-run Report changes without writing.
-o, --output Write to this file instead of replacing the input.
-b, --backup Save <file>.bak before replacing.
--comment-char Override the comment prefix.
--encoding Text encoding (default: utf-8).
--preserve-shebang Never comment line 1 if it starts with '#!'.
--ranges Multiple ranges like '1-10,20,30-40' (overrides positionals).
--strict Fail on decode errors instead of using surrogateescape.
--no-stats Suppress the summary line.
Examples: $ cout script.py 10 25 $ cout script.py 10 25 -r Command-line script: run ``python cout.py`` or the ``cout`` command.
The module is importable as well."""

import argparse
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from itertools import islice
from multiprocessing import Pool
from pathlib import Path
import shutil
import sys
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, TYPE_CHECKING

from loguru import logger


POOL_SIZE: int = 8
CHUNK_SIZE: int = 10_000
SMALL_FILE_BYTES: int = 1 << 20
DEFAULT_COMMENT: str = "#"
COMMENT_MAP: dict[str, str] = {
    ".vim": '"',
    ".lua": "--",
    ".py": "#",
    ".pyi": "#",
    ".sh": "#",
    ".bash": "#",
    ".zsh": "#",
    ".toml": "#",
    ".yml": "#",
    ".yaml": "#",
    ".js": "//",
    ".jsx": "//",
    ".mjs": "//",
    ".cjs": "//",
    ".ts": "//",
    ".tsx": "//",
    ".cpp": "//",
    ".cc": "//",
    ".cxx": "//",
    ".hpp": "//",
    ".hxx": "//",
    ".c": "//",
    ".h": "//",
    ".cs": "//",
    ".java": "//",
    ".go": "//",
    ".rs": "//",
    ".swift": "//",
    ".kt": "//",
    ".kts": "//",
    ".scala": "//",
    ".php": "//",
    ".gradle": "//",
    ".groovy": "//",
    ".dart": "//",
    ".zig": "//",
    ".v": "//",
    ".sol": "//",
    ".sql": "--",
    ".hs": "--",
    ".lhs": "--",
    ".elm": "--",
    ".rb": "#",
    ".pl": "#",
    ".pm": "#",
    ".r": "#",
    ".jl": "#",
    ".nim": "#",
    ".cr": "#",
    ".ex": "#",
    ".exs": "#",
    ".conf": "#",
    ".cfg": "#",
    ".mk": "#",
    ".ini": ";",
    ".tex": "%",
    ".m": "%",
}


@dataclass
class ChunkResult:
    lines: list[str]
    commented: int = 0
    uncommented: int = 0
    skipped: int = 0
    blanks: int = 0
    changed: int = 0


def _normalize_ranges(
    ranges: Sequence[tuple[int, int | None]],
) -> list[tuple[int, int]]:
    normalized: list[tuple[int, int]] = []
    for s, e in ranges:
        if e is None:
            e = sys.maxsize
        if e < s:
            s, e = e, s
        normalized.append((s, e))
    normalized.sort()
    merged: list[tuple[int, int]] = []
    for s, e in normalized:
        if merged and s <= merged[-1][1] + 1:
            ps, pe = merged[-1]
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def parse_ranges(spec: str) -> list[tuple[int, int | None]]:
    ranges: list[tuple[int, int | None]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, _, b_str = part.partition("-")
            a = int(a_str.strip())
            b = int(b_str.strip()) if b_str.strip() else None
            ranges.append((a, b))
        else:
            n = int(part)
            ranges.append((n, n))
    return ranges


def process_chunk(
    lines: Sequence[str],
    start_line: int,
    ranges: Sequence[tuple[int, int]],
    comment_char: str,
    remove: bool,
    preserve_shebang: bool,
) -> ChunkResult:
    out: list[str] = []
    commented = 0
    uncommented = 0
    skipped = 0
    blanks = 0
    clen = len(comment_char)
    for i, line in enumerate(lines):
        lineno = start_line + i
        stripped = line.lstrip()
        if not stripped:
            out.append(line)
            blanks += 1
            continue
        if preserve_shebang and lineno == 1 and stripped.startswith("#!"):
            out.append(line)
            skipped += 1
            continue
        in_range = False
        for s, e in ranges:
            if s > lineno:
                break
            if lineno <= e:
                in_range = True
                break
        if not in_range:
            out.append(line)
            continue
        if remove:
            if stripped.startswith(comment_char):
                leading = line[: len(line) - len(stripped)]
                out.append(leading + stripped[clen:])
                uncommented += 1
            else:
                out.append(line)
                skipped += 1
        elif stripped.startswith(comment_char):
            out.append(line)
            skipped += 1
        else:
            out.append(f"{comment_char}{line}")
            commented += 1
    return ChunkResult(out, commented, uncommented, skipped, blanks, commented + uncommented)


def _worker(
    args: tuple[Sequence[str], int, Sequence[tuple[int, int]], str, bool, bool],
) -> ChunkResult:
    return process_chunk(*args)


def _chunk_iter(stream: Iterable[Any], chunk_size: int) -> Iterator[list[str]]:
    while True:
        chunk = list(islice(stream, chunk_size))
        if not chunk:
            return
        yield chunk


def _arg_iter(
    chunks: Iterator[list[str]],
    ranges: Sequence[tuple[int, int]],
    comment_char: str,
    remove: bool,
    preserve_shebang: bool,
) -> Iterator[tuple]:
    current = 1
    for chunk in chunks:
        yield (chunk, current, ranges, comment_char, remove, preserve_shebang)
        current += len(chunk)


def _dispatch(arg_iter: Iterator[tuple], use_pool: bool) -> Iterator[ChunkResult]:
    if use_pool:
        with Pool(processes=POOL_SIZE) as pool:
            yield from pool.imap(_worker, arg_iter, chunksize=1)
    else:
        for args in arg_iter:
            yield _worker(args)


def _resolve_comment_char(file_path: Path | None, override: str | None) -> str:
    if override:
        return override
    if file_path is None:
        return DEFAULT_COMMENT
    ext = file_path.suffix.lower()
    cc = COMMENT_MAP.get(ext)
    if cc is None:
        logger.warning(
            "Unknown extension {}. Using default '{}' as comment char.",
            ext,
            DEFAULT_COMMENT,
        )
        return DEFAULT_COMMENT
    return cc


def _detect_bom(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def _accumulate(total: ChunkResult, part: ChunkResult) -> None:
    total.commented += part.commented
    total.uncommented += part.uncommented
    total.skipped += part.skipped
    total.blanks += part.blanks
    total.changed += part.changed


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="commentout",
        description="Comment out (or with -r, un-comment) line ranges in a file.",
    )
    p.add_argument("filename", help="Path to file, or '-' for stdin/stdout.")
    p.add_argument("start_line", nargs="?", type=int, help="Start line (1-based).")
    p.add_argument(
        "end_line",
        nargs="?",
        type=int,
        help="End line (inclusive); defaults to EOF.",
    )
    p.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove the comment prefix from target lines.",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Report changes without writing.",
    )
    p.add_argument(
        "-o",
        "--output",
        help="Write to this file instead of replacing the input.",
    )
    p.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="Save <file>.bak before replacing.",
    )
    p.add_argument("--comment-char", help="Override the comment prefix.")
    p.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding (default: utf-8).",
    )
    p.add_argument(
        "--preserve-shebang",
        action="store_true",
        help="Never comment line 1 if it starts with '#!'.",
    )
    p.add_argument(
        "--ranges",
        help="Multiple ranges like '1-10,20,30-40' (overrides positionals).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail on decode errors instead of using surrogateescape.",
    )
    p.add_argument(
        "--no-stats",
        dest="stats",
        action="store_false",
        help="Suppress the summary line.",
    )
    p.set_defaults(stats=True)
    return p


def _parse_args(
    argv: Sequence[str],
) -> tuple[argparse.Namespace, Path | None, list[tuple[int, int]]]:
    parser = _build_parser()
    ns = parser.parse_args(list(argv[1:]))
    raw_ranges: list[tuple[int, int | None]]
    if ns.ranges is not None:
        try:
            raw_ranges = parse_ranges(ns.ranges)
        except ValueError:
            parser.error("invalid --ranges format")
            return ns, None, []
    elif ns.start_line is not None:
        raw_ranges = [(ns.start_line, ns.end_line)]
    else:
        parser.error("provide <start_line> [end_line] or --ranges")
        return ns, None, []
    if not raw_ranges:
        parser.error("no ranges specified")
    for s, _ in raw_ranges:
        if s < 1:
            parser.error("line numbers must be >= 1")
    file_path: Path | None
    if ns.filename == "-":
        file_path = None
    else:
        file_path = Path(ns.filename)
        if not file_path.exists():
            logger.error("File {} not found.", file_path)
            raise SystemExit(1)
    if file_path is None and ns.output is not None:
        parser.error("-o is not allowed when reading from stdin")
    if file_path is None and ns.backup:
        parser.error("-b is not allowed when reading from stdin")
    if file_path is not None and ns.output is not None:
        try:
            if Path(ns.output).resolve() == file_path.resolve():
                parser.error("-o cannot be the same path as input; omit -o for in-place edits")
        except OSError:
            pass
    return ns, file_path, _normalize_ranges(raw_ranges)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv
    ns, file_path, ranges = _parse_args(args)
    is_stdin = file_path is None
    comment_char = _resolve_comment_char(file_path, ns.comment_char)
    errors = "strict" if ns.strict else "surrogateescape"
    read_encoding = ns.encoding
    write_encoding = ns.encoding
    if (
        not is_stdin
        and _detect_bom(file_path)
        and ns.encoding.lower().replace("_", "-")
        in (
            "utf-8",
            "utf8",
        )
    ):
        read_encoding = "utf-8-sig"
        write_encoding = "utf-8-sig"
    initial_stat = None if is_stdin else file_path.stat()
    file_size = initial_stat.st_size if initial_stat is not None else 0
    use_pool = file_size >= SMALL_FILE_BYTES
    if ns.dry_run:
        mode = "dry-run"
    elif is_stdin:
        mode = "stdout"
    elif ns.output:
        mode = "output"
    else:
        mode = "atomic"
    total = ChunkResult([])
    temp_path: Path | None = None
    success = False
    try:
        with ExitStack() as stack:
            if is_stdin:
                infile = sys.stdin
            else:
                infile = stack.enter_context(file_path.open("r", encoding=read_encoding, errors=errors, newline=""))
            chunks = _chunk_iter(infile, CHUNK_SIZE)
            arg_iter = _arg_iter(chunks, ranges, comment_char, ns.remove, ns.preserve_shebang)
            results = _dispatch(arg_iter, use_pool)
            if mode == "dry-run":
                for res in results:
                    _accumulate(total, res)
            elif mode == "stdout":
                for res in results:
                    _accumulate(total, res)
                    sys.stdout.writelines(res.lines)
                sys.stdout.flush()
            elif mode == "output":
                out_path = Path(str(ns.output))
                outfile = stack.enter_context(out_path.open("w", encoding=write_encoding, errors=errors, newline=""))
                for res in results:
                    _accumulate(total, res)
                    outfile.writelines(res.lines)
            else:
                tmp = stack.enter_context(
                    NamedTemporaryFile(
                        "w",
                        delete=False,
                        dir=file_path.parent,
                        encoding=write_encoding,
                        errors=errors,
                        newline="",
                    )
                )
                temp_path = Path(tmp.name)
                for res in results:
                    _accumulate(total, res)
                    tmp.writelines(res.lines)
        if mode == "atomic" and temp_path is not None:
            if total.changed == 0:
                with suppress(OSError):
                    temp_path.unlink()
                temp_path = None
            else:
                current_stat = file_path.stat()
                if (
                    current_stat.st_mtime_ns,
                    current_stat.st_size,
                ) != (initial_stat.st_mtime_ns, initial_stat.st_size):
                    logger.error("Input {} changed during processing; aborting.", file_path)
                    raise SystemExit(3)
                if ns.backup:
                    shutil.copy2(file_path, file_path.with_name(file_path.name + ".bak"))
                temp_path.replace(file_path)
                temp_path = None
        success = True
    finally:
        if not success and temp_path is not None and temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()
    if ns.stats:
        action = "un-commented" if ns.remove else "commented"
        if mode == "dry-run":
            logger.info(
                "DRY RUN: would {} {} range(s); {} line(s) changed, {} skipped, {} blank",
                action,
                len(ranges),
                total.changed,
                total.skipped,
                total.blanks,
            )
        else:
            if mode == "stdout":
                target = "<stdout>"
            elif mode == "output":
                target = str(ns.output)
            else:
                target = str(file_path)
            if total.changed == 0:
                logger.info(
                    "No changes needed for {} ({} blank line(s) scanned).",
                    target,
                    total.blanks,
                )
            else:
                logger.info(
                    "{} {} line(s) in {} using '{}' ({} skipped, {} blank).",
                    action.capitalize(),
                    total.changed,
                    target,
                    comment_char,
                    total.skipped,
                    total.blanks,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
