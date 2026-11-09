#!/data/data/com.termux/files/usr/bin/env python
"""Unified text splitting and merging toolkit."""


import argparse
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
import re
import string
import sys
from typing import Optional, Sequence
import uuid

from dh import MAX_WORKERS
from loguru import logger


try:
    from loguru import logger as _loguru_logger
except ImportError:
    _loguru_logger = None
try:
    from binaryornot.check import is_binary as _is_binary
except ImportError:
    _is_binary = None
try:
    from nltk.tokenize import sent_tokenize as _sent_tokenize
except ImportError:
    _sent_tokenize = None
PART_RE = re.compile(r"^(?P<prefix>.+)\.part(?P<num>\d+)$")
SENTENCE_RE = re.compile(r"[.!?]\s+")
WHITESPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"\S+\s*", re.DOTALL)


def setup_logging() -> None:
    pass


def is_binary(path: Path) -> bool:
    if _is_binary is not None:
        return _is_binary(str(path))
    try:
        chunk = path.read_bytes()[:1024]
    except OSError:
        return False
    return b"\x00" in chunk


def read_text(path: Path, encoding: str = "utf-8", errors: str = "ignore") -> Optional[str]:
    try:
        return path.read_text(encoding=encoding, errors=errors)
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1", errors=errors)
        except OSError:
            return None
    except OSError:
        return None


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def collect_files(
    inputs: Sequence[Path],
    recursive: bool = True,
    extensions: Optional[set[str]] = None,
) -> list[Path]:
    if not inputs:
        inputs = [Path.cwd()]
    result: list[Path] = []
    for p in inputs:
        if p.is_file():
            if extensions is None or p.suffix.lower().lstrip(".") in extensions:
                result.append(p)
        elif p.is_dir():
            iterator = p.rglob("*") if recursive else p.glob("*")
            for f in iterator:
                if f.is_file() and (extensions is None or f.suffix.lower().lstrip(".") in extensions):
                    result.append(f)
    return list(dict.fromkeys(result))


def random_filename(ext: str = "txt") -> str:
    return f"{uuid.uuid4().hex}.{ext}" if ext else f"{uuid.uuid4().hex}.txt"


def _find_boundary(segment: str, order: Sequence[str]) -> Optional[int]:
    for kind in order:
        if kind == "sentence":
            matches = list(SENTENCE_RE.finditer(segment))
            if matches:
                return matches[-1].end()
        elif kind == "whitespace":
            matches = list(WHITESPACE_RE.finditer(segment))
            if matches:
                return matches[-1].end()
        elif kind == "newline":
            idx = segment.rfind("\n")
            if idx >= 0:
                return idx + 1
    return None


def _apply_strip(part: str, mode: str) -> str:
    if mode == "right":
        return part.rstrip()
    if mode == "left":
        return part.lstrip()
    if mode == "both":
        return part.strip()
    return part


def split_text_range(
    text: str,
    min_chars: int,
    max_chars: int,
    boundary_order: Sequence[str],
    strip_mode: str,
) -> list[str]:
    if min_chars < 0 or max_chars <= 0:
        msg = "min_chars must be >= 0 and max_chars > 0"
        raise ValueError(msg)
    if min_chars > max_chars:
        min_chars, max_chars = max_chars, min_chars
    parts: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        if n - pos <= max_chars:
            part = _apply_strip(text[pos:], strip_mode)
            if part:
                parts.append(part)
            break
        search_start = min(pos + min_chars, n)
        search_end = min(pos + max_chars, n)
        segment = text[search_start:search_end]
        rel = _find_boundary(segment, boundary_order)
        cut = search_start + rel if rel is not None else search_end
        if cut <= pos:
            cut = min(pos + max_chars, n)
        part = _apply_strip(text[pos:cut], strip_mode)
        if part:
            parts.append(part)
        pos = cut
    return parts


def split_text_target_window(
    text: str,
    target: int,
    window: int,
    max_chars: int,
    boundary_order: Sequence[str],
    strip_mode: str,
) -> list[str]:
    parts: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        if n - pos <= max_chars:
            part = _apply_strip(text[pos:], strip_mode)
            if part:
                parts.append(part)
            break
        desired = pos + target
        search_start = max(pos, desired - window)
        search_end = min(n, desired + window)
        segment = text[search_start:search_end]
        rel = _find_boundary(segment, boundary_order)
        if rel is not None:
            cut = search_start + rel
        else:
            cut = desired
            for i in range(min(desired, n - 1), search_start - 1, -1):
                if text[i] in " \n\t":
                    cut = i + 1
                    break
        cut = min(cut, pos + max_chars)
        if cut <= pos:
            cut = min(pos + target, n)
        part = _apply_strip(text[pos:cut], strip_mode)
        if part:
            parts.append(part)
        pos = cut
    return parts


def split_text_sentences(
    text: str,
    max_chars: int,
    tokenizer: str,
    strip_mode: str,
) -> list[str]:
    if tokenizer == "nltk" and _sent_tokenize is not None:
        sentences = _sent_tokenize(text)
    elif tokenizer == "words":
        sentences = WORD_RE.findall(text)
    else:
        sentences = re.split(r"(?<=[.!?])\s+", text)
    parts: list[str] = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) <= max_chars:
            current += sent
        else:
            if current:
                parts.append(current)
            if len(sent) > max_chars:
                words = WORD_RE.findall(sent)
                chunk = ""
                for w in words:
                    if len(chunk) + len(w) <= max_chars:
                        chunk += w
                    else:
                        if chunk:
                            parts.append(chunk)
                        if len(w) > max_chars:
                            for i in range(0, len(w), max_chars):
                                parts.append(w[i : i + max_chars])
                            chunk = ""
                        else:
                            chunk = w
                if chunk:
                    parts.append(chunk)
                current = ""
            else:
                current = sent
    if current:
        parts.append(current)
    if strip_mode != "none":
        parts = [_apply_strip(p, strip_mode) for p in parts]
    return [p for p in parts if p]


@dataclass
class SplitCharsConfig:
    algorithm: str
    min_chars: int
    max_chars: int
    boundary_order: list[str]
    strip_mode: str
    target: int
    window: int
    tokenizer: str
    output_dir: Path
    output_same_dir: bool
    pad_width: int
    recursive: bool
    extensions: Optional[set[str]]


def process_split_chars_file(path: Path, config: SplitCharsConfig) -> tuple[Path, int]:
    if is_binary(path):
        print(f"Skipping binary file: {path}")
        return path, 0
    text = read_text(path)
    if not text or not text.strip():
        print(f"Skipping empty file: {path}")
        return path, 0
    if config.algorithm == "range":
        parts = split_text_range(
            text,
            config.min_chars,
            config.max_chars,
            config.boundary_order,
            config.strip_mode,
        )
    elif config.algorithm == "target-window":
        parts = split_text_target_window(
            text,
            config.target,
            config.window,
            config.max_chars,
            config.boundary_order,
            config.strip_mode,
        )
    elif config.algorithm == "sentences":
        parts = split_text_sentences(text, config.max_chars, config.tokenizer, config.strip_mode)
    else:
        msg = f"Unknown algorithm: {config.algorithm}"
        raise ValueError(msg)
    if not parts:
        print(f"No parts generated for: {path}")
        return path, 0
    out_dir = path.parent if config.output_same_dir else config.output_dir
    ensure_dir(out_dir)
    stem = path.stem
    suffix = path.suffix
    for i, part in enumerate(parts, 1):
        if config.pad_width > 0:
            name = f"{stem}_{i:0{config.pad_width}d}{suffix}"
        else:
            name = f"{stem}_{i}{suffix}"
        (out_dir / name).write_text(part, encoding="utf-8")
    print(f"Split {path.name} into {len(parts)} parts")
    return path, len(parts)


def cmd_split_chars(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    extensions = {e.lower().lstrip(".") for e in args.extensions} if args.extensions else None
    files = collect_files(inputs, recursive=not args.no_recursive, extensions=extensions)
    if not files:
        logger.error("No text files found to process")
        return 1
    print(f"Found {len(files)} file(s) to process")
    config = SplitCharsConfig(
        algorithm=args.algorithm,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        boundary_order=[x.strip() for x in args.boundary_order.split(",") if x.strip()],
        strip_mode=args.strip,
        target=args.target,
        window=args.window,
        tokenizer=args.tokenizer,
        output_dir=Path(args.output),
        output_same_dir=args.output_same_dir,
        pad_width=args.pad_width,
        recursive=not args.no_recursive,
        extensions=extensions,
    )
    total_parts = 0
    processed = 0
    if MAX_WORKERS > 1:
        with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_split_chars_file, f, config) for f in files]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    _path, n = fut.result()
                    total_parts += n
                    processed += 1
                except Exception as exc:
                    logger.error("Failed to process a file: {}", exc)
    else:
        for f in files:
            _path, n = process_split_chars_file(f, config)
            total_parts += n
            processed += 1
    print(f"Processing complete: {processed} files split into {total_parts} parts")
    return 0


def cmd_split_lines(args: argparse.Namespace) -> int:
    path = Path(args.path)
    n = args.n
    if n <= 0:
        print("n must be a positive integer", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    if is_binary(path):
        print(f"Error: binary file '{path}' detected. Aborting.", file=sys.stderr)
        return 1
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"Failed to read input file: {exc}", file=sys.stderr)
        return 1
    lines = text.splitlines(keepends=True)
    total = len(lines)
    width = len(str(n))
    chunk_size = total // n
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    pos = 0
    for i in range(1, n + 1):
        end = total if i == n else pos + chunk_size
        part = "".join(lines[pos:end])
        out_name = f"{stem}_{str(i).zfill(width)}{suffix}"
        out_path = parent / out_name
        out_path.write_text(part, encoding="utf-8")
        print(f"Created: {out_path}")
        pos = end
    return 0


def cmd_split_delimiter(args: argparse.Namespace) -> int:
    path = Path(args.path)
    delim = args.delimiter
    if not delim:
        print("Error: delimiter cannot be empty", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    text = read_text(path)
    if text is None:
        print(f"Error: could not read {path}", file=sys.stderr)
        return 1
    if args.output_dir is None:
        parts = text.split(delim)
        out_lines = []
        for part in parts:
            part = _apply_strip(part, args.strip)
            out_lines.append(part + delim + "\n")
        path.write_text("".join(out_lines), encoding="utf-8")
        print(f"{path} updated.")
    else:
        out_dir = Path(args.output_dir)
        ensure_dir(out_dir)
        stem = path.stem
        suffix = path.suffix
        parts = text.split(delim)
        for i, part in enumerate(parts):
            part = _apply_strip(part, args.strip)
            out_path = out_dir / f"{stem}{i}{suffix}"
            content = (delim if args.prefix_delimiter else "") + part + (delim if args.suffix_delimiter else "") + "\n"
            out_path.write_text(content, encoding="utf-8")
            print(f"{out_path} created")
    return 0


def cmd_split_by_letter(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    out_dir = Path(args.output_dir)
    ensure_dir(out_dir)
    handles = {letter: (out_dir / f"{letter}.txt").open("w", encoding="utf-8") for letter in string.ascii_lowercase}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.lstrip()
                if not stripped:
                    continue
                first = stripped[0].lower()
                if first in handles:
                    handles[first].write(line)
    finally:
        for handle in handles.values():
            handle.close()
    return 0


def collect_part_files(paths: Sequence[Path]) -> list[Path]:
    if not paths:
        paths = [Path.cwd()]
    result: list[Path] = []
    for p in paths:
        if p.is_dir():
            result.extend(x for x in p.rglob("*") if x.is_file() and PART_RE.match(x.name))
        elif p.is_file() and PART_RE.match(p.name):
            result.append(p)
    return result


def cmd_merge_parts(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.paths] if args.paths else []
    part_files = collect_part_files(paths)
    if not part_files:
        print("No .part files found")
        return 1
    groups: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    for p in part_files:
        m = PART_RE.match(p.name)
        if not m:
            continue
        key = (p.parent.resolve(), m.group("prefix"))
        groups.setdefault(key, []).append((int(m.group("num")), p))
    outputs: list[Path] = []
    for (parent, prefix), items in groups.items():
        items.sort(key=lambda x: x[0])
        out_path = parent / prefix
        with out_path.open("wb") as out:
            for _num, part in items:
                with part.open("rb") as inp:
                    while True:
                        chunk = inp.read(1048576)
                        if not chunk:
                            break
                        out.write(chunk)
        outputs.append(out_path)
    for out in outputs:
        print(out)
    return 0


def should_skip(path: Path) -> bool:
    return path.name.startswith(".") or path.is_dir()


def cmd_merge_text(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    files = [f for f in cwd.iterdir() if f.is_file() and not should_skip(f)]
    if args.extensions:
        exts = {e.lower().lstrip(".") for e in args.extensions}
        files = [f for f in files if f.suffix.lower().lstrip(".") in exts]
    valid: list[tuple[Path, str]] = []
    for f in files:
        if is_binary(f):
            continue
        text = read_text(f)
        if text is not None and text.strip():
            valid.append((f, text))
    if not valid:
        print("No files to merge.")
        return 0
    if not args.group:
        ext = "txt"
        if args.extensions is None:
            suffixes = {f.suffix.lower().lstrip(".") for f, _ in valid}
            if len(suffixes) == 1:
                ext = next(iter(suffixes)) or "txt"
        out_path = cwd / random_filename(ext)
        total_bytes = 0
        with out_path.open("w", encoding="utf-8") as out:
            for f, text in valid:
                rel = f.relative_to(cwd)
                out.write(f"# File: {rel}\n")
                out.write(text)
                if not text.endswith("\n"):
                    out.write("\n")
                total_bytes += len(text)
        print(f"Merged {len(valid)} files ({total_bytes:,} bytes) into: {out_path}")
    else:
        out_dir = cwd / "merged"
        ensure_dir(out_dir)
        groups: dict[str, list[tuple[Path, str]]] = {}
        for f, text in valid:
            ext = f.suffix.lower().lstrip(".")
            groups.setdefault(ext, []).append((f, text))
        for ext, items in groups.items():
            out_path = out_dir / (random_filename(ext) if ext else random_filename("txt"))
            total_bytes = 0
            with out_path.open("w", encoding="utf-8") as out:
                for f, text in items:
                    rel = f.relative_to(cwd)
                    out.write(f"# File: {rel}\n")
                    out.write(text)
                    if not text.endswith("\n"):
                        out.write("\n")
                    total_bytes += len(text)
            print(f"Merged {len(items)} .{ext} files ({total_bytes:,} bytes) into: {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="text_toolkit.py",
        description="Unified text splitting and merging toolkit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("split-chars", help="Split text files into character-based chunks.")
    p.add_argument(
        "inputs",
        nargs="*",
        help="Input files or directories. Default: current directory.",
    )
    p.add_argument(
        "-o",
        "--output",
        default="split_output",
        help="Output directory. Default: split_output",
    )
    p.add_argument(
        "--output-same-dir",
        action="store_true",
        help="Write output files next to input files.",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not search directories recursively.",
    )
    p.add_argument(
        "--min-chars",
        type=int,
        default=4900,
        help="Minimum characters per part. Default: 4900",
    )
    p.add_argument(
        "--max-chars",
        type=int,
        default=4990,
        help="Maximum characters per part. Default: 4990",
    )
    p.add_argument(
        "--algorithm",
        choices=["range", "target-window", "sentences"],
        default="range",
        help="Splitting algorithm. Default: range",
    )
    p.add_argument(
        "--target",
        type=int,
        default=5000,
        help="Target chunk size for target-window/sentences. Default: 5000",
    )
    p.add_argument(
        "--window",
        type=int,
        default=500,
        help="Window size for target-window. Default: 500",
    )
    p.add_argument(
        "--boundary-order",
        default="sentence,whitespace",
        help="Comma-separated boundary preference: sentence, whitespace, newline. Default: sentence,whitespace",
    )
    p.add_argument(
        "--strip",
        choices=["none", "left", "right", "both"],
        default="right",
        help="Strip whitespace from parts. Default: right",
    )
    p.add_argument(
        "--tokenizer",
        choices=["nltk", "regex", "words"],
        default="nltk",
        help="Tokenizer for sentences algorithm. Default: nltk",
    )
    p.add_argument(
        "--pad-width",
        type=int,
        default=3,
        help="Zero-pad width for part numbers. 0 disables padding. Default: 3",
    )
    p.add_argument("--extensions", nargs="+", help="Only process these extensions.")
    p.set_defaults(func=cmd_split_chars)
    p = sub.add_parser(
        "split-lines",
        help="Split a text file into N approximately equal line-based parts.",
    )
    p.add_argument("path", help="Input file.")
    p.add_argument("n", type=int, help="Number of parts.")
    p.set_defaults(func=cmd_split_lines)
    p = sub.add_parser("split-delimiter", help="Split a text file by a delimiter.")
    p.add_argument("path", help="Input file.")
    p.add_argument("delimiter", help="Delimiter string.")
    p.add_argument("-o", "--output-dir", help="Output directory. If omitted, modify file in-place.")
    p.add_argument(
        "--prefix-delimiter",
        action="store_true",
        help="Prefix each output part with the delimiter.",
    )
    p.add_argument(
        "--suffix-delimiter",
        action="store_true",
        help="Suffix each output part with the delimiter.",
    )
    p.add_argument(
        "--strip",
        choices=["none", "left", "right", "both"],
        default="both",
        help="Strip whitespace from parts. Default: both",
    )
    p.set_defaults(func=cmd_split_delimiter)
    p = sub.add_parser("split-by-letter", help="Split lines into files named by first letter.")
    p.add_argument("path", help="Input file.")
    p.add_argument("-o", "--output-dir", default="output", help="Output directory. Default: output")
    p.set_defaults(func=cmd_split_by_letter)
    p = sub.add_parser("merge-parts", help="Merge .partNNN files back into original files.")
    p.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. Default: current directory.",
    )
    p.set_defaults(func=cmd_merge_parts)
    p = sub.add_parser("merge-text", help="Merge text files in current directory.")
    p.add_argument(
        "-e",
        "--extensions",
        nargs="+",
        help="File extensions to merge (e.g., py cpp js).",
    )
    p.add_argument(
        "-c",
        "--group",
        action="store_true",
        help="Group files by extension into separate output files in 'merged' directory.",
    )
    p.set_defaults(func=cmd_merge_text)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
