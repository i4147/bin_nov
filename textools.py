#!/data/data/com.termux/files/usr/bin/env python
"""Text kit: count words and characters in text files in parallel."""

import argparse
from collections import Counter
from datetime import datetime
import json
import mimetypes
from multiprocessing import Pool
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from dh import MAX_WORKERS, SKIP_DIRS


try:
    import nltk
    from nltk.corpus import stopwords as _nltk_stopwords
    from nltk.tokenize import word_tokenize as _nltk_word_tokenize

    _HAS_NLTK = True
except ImportError:
    _HAS_NLTK = False
TEXT_EXTS: set[str] = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".css",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".rs",
    ".go",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".sql",
    ".pl",
    ".lua",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".scss",
    ".less",
    ".log",
    ".csv",
    ".tsv",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".gradle",
    ".maven",
    ".cmake",
    ".makefile",
    ".dockerfile",
    ".gitignore",
    ".editorconfig",
    ".eslintrc",
    ".prettierrc",
}
BINARY_EXTS: set[str] = {
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".o",
    ".a",
    ".zip",
    ".tar",
    ".gz",
    ".jpg",
    ".png",
    ".gif",
    ".ico",
    ".pdf",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".mkv",
    ".webm",
    ".wav",
    ".flac",
}
SPECIAL_NAMES: set[str] = {
    "makefile",
    "dockerfile",
    "gemfile",
    "procfile",
    "rakefile",
    "guardfile",
    "capfile",
    "thorfile",
}


def is_text_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTS:
        return False
    if suffix in TEXT_EXTS:
        return True
    name = path.name.lower()
    if name in SPECIAL_NAMES:
        return True
    if suffix == "":
        if name.startswith("."):
            return True
        try:
            with path.open("rb") as fh:
                chunk = fh.read(8192)
        except OSError:
            return False
        if not chunk:
            return False
        printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
        return printable / len(chunk) > 0.75
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime.startswith("text/")
    return False


def iter_text_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(root.rglob("*")):
        if entry.is_dir():
            continue
        if any(part in SKIP_DIRS for part in entry.parts):
            continue
        if entry.is_symlink():
            continue
        if is_text_file(entry):
            out.append(entry)
    return out


_WORD_RE = re.compile(r"[a-z]+")
_ALNUM_RE = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str, min_length: int = 3, mode: str = "regex") -> list[str]:
    text = text.lower()
    if mode == "nltk":
        if not _HAS_NLTK:
            msg = "nltk is not installed; use --tokenize regex|alnum instead"
            raise RuntimeError(msg)
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
        tokens = [t for t in _nltk_word_tokenize(text) if t.isalnum()]
        return [t for t in tokens if len(t) >= min_length]
    if mode == "alnum":
        return [t for t in _ALNUM_RE.findall(text) if len(t) >= min_length]
    return [t for t in _WORD_RE.findall(text) if len(t) >= min_length]


def load_stopwords(source: str, path: Path | None) -> set[str]:
    if source == "none":
        return set()
    if source == "nltk":
        if not _HAS_NLTK:
            print(
                "Warning: nltk not installed; no stopwords will be used.",
                file=sys.stderr,
            )
            return set()
        try:
            nltk.data.find("corpora/stopwords")
        except LookupError:
            nltk.download("stopwords", quiet=True)
        return set(_nltk_stopwords.words("english"))
    if path is None or not path.exists():
        print(
            f"Warning: stopwords file {path!r} not found; none loaded.",
            file=sys.stderr,
        )
        return set()
    words: set[str] = set()
    with path.open(errors="ignore") as fh:
        for line in fh:
            w = line.strip().lower()
            if w and not w.startswith("#"):
                words.add(w)
    return words


def _worker_word_counts(
    task: tuple[str, int, str, set[str]],
) -> tuple[str, Counter, str | None]:
    path_str, min_length, mode, stopwords = task
    try:
        text = Path(path_str).read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return path_str, Counter(), str(exc)
    tokens = tokenize(text, min_length, mode)
    if stopwords:
        tokens = [t for t in tokens if t not in stopwords]
    return path_str, Counter(tokens), None


def _worker_collect_chars(path_str: str) -> set[str]:
    try:
        with open(path_str, "r", encoding="utf-8", errors="ignore") as fh:
            chars: set[str] = set()
            while True:
                chunk = fh.read(8192)
                if not chunk:
                    break
                chars.update(chunk)
            return chars
    except OSError:
        return set()


def _resolve_files(raw: Sequence[Path]) -> list[Path]:
    if not raw:
        return iter_text_files(Path.cwd())
    out: list[Path] = []
    for item in raw:
        if item.is_dir():
            out.extend(iter_text_files(item))
        elif item.is_file():
            out.append(item)
        else:
            print(f"Warning: {item} not found; skipping.", file=sys.stderr)
    return out


def _write_json(path: Path, counter: Counter, with_metadata: bool) -> None:
    sorted_items = dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))
    if with_metadata:
        payload: dict[str, Any] = {
            "metadata": {
                "total_words": sum(counter.values()),
                "unique_words": len(counter),
                "timestamp": datetime.now().isoformat(),
            },
            "word_counts": sorted_items,
        }
    else:
        payload = sorted_items
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Results saved to {path}")


def _emit_aggregate(counter: Counter, args: argparse.Namespace) -> None:
    total_tokens = sum(counter.values())
    items = counter.most_common()
    if args.min_count > 1:
        items = [(w, c) for w, c in items if c >= args.min_count]
    items = items[: args.top]
    if args.format == "table":
        print(f"\nTop {len(items)} most frequent words:")
        print("-" * 40)
        print(f"{'Rank':<6} {'Word':<25} {'Count':<8} {'Frequency %'}")
        print("-" * 40)
        for rank, (word, count) in enumerate(items, 1):
            pct = (count / total_tokens * 100) if total_tokens else 0.0
            print(f"{rank:<6} {word:<25} {count:<8} {pct:.2f}%")
        print("-" * 40)
        print(f"Total unique words: {len(counter)}")
        print(f"Total words: {total_tokens}")
    elif args.format == "one-line":
        print(" ".join(w for w, _ in items))
    elif args.format == "json":
        print(json.dumps(dict(items), indent=2, ensure_ascii=False))
    else:
        for word, count in items:
            print(f"{word:<15} {count}")


def cmd_words(args: argparse.Namespace) -> int:
    files = _resolve_files(args.files)
    if not files:
        print("No input files found.", file=sys.stderr)
        return 1
    stopwords = load_stopwords(args.stopwords_source, args.stopwords_file)
    tasks = [(str(p), args.min_length, args.tokenize, stopwords) for p in files]
    results: list[tuple[str, Counter, str | None]] = []
    if MAX_WORKERS > 1 and len(tasks) > 1:
        with Pool(processes=MAX_WORKERS) as pool:
            results = list(pool.imap(_worker_word_counts, tasks))
    else:
        for t in tasks:
            results.append(_worker_word_counts(t))
    for path_str, _, err in results:
        if err:
            print(f"Warning: could not read {path_str}: {err}", file=sys.stderr)
    if args.per_file:
        for path_str, counter, err in results:
            if err:
                continue
            top = counter.most_common(args.top)
            if args.format in {"one-line", "plain"}:
                print(" ".join(w for w, _ in top))
            elif args.format == "table":
                print(f"{path_str}:")
                for rank, (w, c) in enumerate(top, 1):
                    print(f"  {rank:<4} {w:<20} {c}")
            elif args.format == "json":
                print(json.dumps({path_str: dict(top)}, ensure_ascii=False))
        if args.json:
            _write_json(
                args.json,
                Counter(),
                with_metadata=False,
            )
        return 0
    total: Counter = Counter()
    for _, counter, _ in results:
        total.update(counter)
    if args.json:
        _write_json(args.json, total, with_metadata=args.with_metadata)
    _emit_aggregate(total, args)
    return 0


def cmd_chars(args: argparse.Namespace) -> int:
    path: Path = args.file
    if not path.exists():
        print(f"Error: File '{path}' not found.", file=sys.stderr)
        return 1
    if path.is_symlink() or not is_text_file(path):
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    n_chars = len(text)
    print(f"Number of characters in '{path}': {n_chars}")
    if args.bytes:
        print(f"char : {n_chars}\nsize : {path.stat().st_size}")
    return 0


def cmd_collect_chars(args: argparse.Namespace) -> int:
    root: Path = args.directory.resolve()
    out_path: Path = args.output
    print(f"Scanning directory: {root}")
    files = iter_text_files(root)
    if not files:
        print("No text files found!")
        return 0
    print(f"Found {len(files):,} text files")
    print(f"\nProcessing files with {MAX_WORKERS} workers...")
    unique_chars: set[str] = set()
    processed = 0
    if MAX_WORKERS > 1 and len(files) > 1:
        with Pool(processes=MAX_WORKERS) as pool:
            for chars in pool.imap(_worker_collect_chars, [str(f) for f in files]):
                unique_chars.update(chars)
                processed += 1
                if processed % 100 == 0:
                    print(f"  Processed: {processed:,} files | Unique chars so far: {len(unique_chars):,}")
    else:
        for f in files:
            unique_chars.update(_worker_collect_chars(str(f)))
            processed += 1
    print(f"\n\u2713 Processed: {processed:,} files")
    print(f"\u2713 Unique characters found: {len(unique_chars):,}")

    def sort_key(ch: str) -> tuple[int, int]:
        code = ord(ch)
        if code < 32 and code not in (9, 10, 13):
            return (0, code)
        if 32 <= code <= 126:
            return (1, code)
        if code == 32:
            return (2, code)
        return (3, code)

    ordered = sorted(unique_chars, key=sort_key)
    print(f"Saving unique characters to {out_path}...")
    with out_path.open("w", encoding="utf-8") as fh:
        for ch in ordered:
            if ch == "\n":
                fh.write("\\n\n")
            elif ch == "\r":
                fh.write("\\r\n")
            elif ch == "\t":
                fh.write("\\t\n")
            elif ch == " ":
                fh.write("SPACE\n")
            elif ord(ch) < 32:
                fh.write(f"\\x{ord(ch):02x}\n")
            else:
                fh.write(f"{ch}\n")
    print(f"\u2713 Saved to {out_path.resolve()}")
    ascii_count = sum(1 for c in unique_chars if ord(c) < 128)
    ctrl_count = sum(1 for c in unique_chars if ord(c) < 32)
    ws_count = sum(1 for c in unique_chars if c.isspace())
    digit_count = sum(1 for c in unique_chars if c.isdigit())
    alpha_count = sum(1 for c in unique_chars if c.isalpha())
    uni_count = len(unique_chars) - ascii_count
    print("\n\U0001f4ca Statistics:")
    print(f"  Total unique characters: {len(unique_chars)}")
    print(f"  ASCII characters: {ascii_count}")
    print(f"  Control characters: {ctrl_count}")
    print(f"  Whitespace characters: {ws_count}")
    print(f"  Digits: {digit_count}")
    print(f"  Letters: {alpha_count}")
    print(f"  Unicode characters: {uni_count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="textkit",
        description=(
            "Unified text-analysis toolkit: word frequency, character counting, and unique-character collection."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  textkit.py words notes.txt --top 20\n"
            "  textkit.py words --per-file --top 30 .\n"
            "  textkit.py words --json counts.json --with-metadata .\n"
            "  textkit.py chars notes.txt --bytes\n"
            "  textkit.py collect-chars ./src -o chars.txt\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_words = sub.add_parser(
        "words",
        help="Word-frequency analysis (aggregate or per-file).",
        description=(
            "Count word frequencies. With no positional arguments the current directory is scanned for text files."
        ),
    )
    p_words.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Files or directories to analyze (default: cwd scan).",
    )
    p_words.add_argument(
        "-n",
        "--top",
        type=int,
        default=50,
        help="Max words to display (default: 50).",
    )
    p_words.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Drop words seen fewer than this many times (default: 1).",
    )
    p_words.add_argument(
        "--min-length",
        type=int,
        default=3,
        help="Drop tokens shorter than this (default: 3).",
    )
    p_words.add_argument(
        "--tokenize",
        choices=["regex", "alnum", "nltk"],
        default="regex",
        help="Tokenization strategy (default: regex).",
    )
    p_words.add_argument(
        "--stopwords-source",
        choices=["none", "file", "nltk"],
        default="none",
        help="Where to load stopwords from (default: none).",
    )
    p_words.add_argument(
        "--stopwords-file",
        type=Path,
        default=Path("/sdcard/data/stopwords"),
        help="Path used when --stopwords-source=file.",
    )
    p_words.add_argument(
        "--per-file",
        action="store_true",
        help="Print top-N per file instead of aggregating.",
    )
    p_words.add_argument(
        "--format",
        choices=["plain", "table", "one-line", "json"],
        default="plain",
        help="Output format for aggregate mode (default: plain).",
    )
    p_words.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write results to this JSON file.",
    )
    p_words.add_argument(
        "--with-metadata",
        action="store_true",
        help="Wrap JSON output with a metadata block.",
    )
    p_words.set_defaults(func=cmd_words)
    p_chars = sub.add_parser(
        "chars",
        help="Character count of a single file.",
        description="Count characters (and optionally bytes) of a file.",
    )
    p_chars.add_argument("file", type=Path, help="Input file.")
    p_chars.add_argument(
        "--bytes",
        action="store_true",
        help="Also print the file size in bytes (charcount.py behavior).",
    )
    p_chars.set_defaults(func=cmd_chars)
    p_cc = sub.add_parser(
        "collect-chars",
        help="Collect unique characters across a directory tree.",
        description="Scan text files and write their unique characters to a file.",
    )
    p_cc.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="Directory to scan (default: current directory).",
    )
    p_cc.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("chars.txt"),
        help="Output file (default: chars.txt).",
    )
    p_cc.set_defaults(func=cmd_collect_chars)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
