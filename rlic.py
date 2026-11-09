#!/data/data/com.termux/files/usr/bin/env python
"""Find and remove repeated multiline blocks in text files."""


import argparse
import ast
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Optional, Sequence

from dh import MAX_WORKERS


DEFAULT_TEXT_EXTS: set[str] = {
    ".txt",
    ".md",
    ".rst",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".xml",
    ".svg",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".log",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".lua",
    ".r",
    ".swift",
    ".kt",
    ".scala",
    ".clj",
    ".groovy",
    ".sql",
}
BINARY_EXTS: set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".mkv",
    ".flv",
    ".wmv",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}
COMMENT_EXCEPTIONS: tuple[str, ...] = (
    "#!",
    "# type",
    "# fmt",
    "# pylint",
    "# ruff",
    "# mypy",
)


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


@dataclass
class Occurrence:
    path: Path
    start: int
    end: int
    normalized: str
    raw_lines: list[str] = field(default_factory=list)


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except OSError:
        return True


def _is_probably_text(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(1024)
        if not chunk:
            return True
        printable = sum(1 for b in chunk if 32 <= b <= 126 or b in (9, 10, 13))
        return printable / len(chunk) > 0.8
    except OSError:
        return False


def collect_files(root: Path, *, block_mode: str, extensions: Optional[set[str]]) -> list[Path]:
    if block_mode == "comment":
        wanted = extensions or {".py"}
    elif block_mode == "segment":
        wanted = extensions
    else:
        wanted = extensions or DEFAULT_TEXT_EXTS
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if ".git" in p.parts:
            continue
        if p.suffix.lower() in BINARY_EXTS:
            continue
        if block_mode == "segment":
            if (wanted is None or p.suffix.lower() in wanted) and not _is_binary(p):
                out.append(p)
            continue
        if p.suffix.lower() in wanted:
            out.append(p)
            continue
        if block_mode == "paragraph" and "." not in p.name and _is_probably_text(p):
            out.append(p)
    return sorted(out)


def _normalize(lines: Sequence[str]) -> str:
    return "\n".join(l.rstrip() for l in lines).strip()


def extract_paragraph_blocks(lines: list[str], min_lines: int, min_chars: int) -> list[Occurrence]:
    blocks: list[Occurrence] = []
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        start = i
        raw: list[str] = [lines[i]]
        i += 1
        while i < n and lines[i].strip():
            raw.append(lines[i])
            i += 1
        end = i - 1
        normalized = _normalize(raw)
        if len(raw) >= min_lines and len(normalized) >= min_chars:
            blocks.append(
                Occurrence(
                    path=Path(),
                    start=start,
                    end=end,
                    normalized=normalized,
                    raw_lines=list(raw),
                )
            )
    return blocks


def _is_generic_comment(line: str) -> bool:
    s = line.strip()
    if not s.startswith("#"):
        return False
    return not any(s.startswith(prefix) for prefix in COMMENT_EXCEPTIONS)


def extract_comment_blocks(lines: list[str], min_lines: int) -> list[Occurrence]:
    blocks: list[Occurrence] = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_generic_comment(lines[i]):
            start = i
            raw: list[str] = []
            stripped: list[str] = []
            while i < n and _is_generic_comment(lines[i]):
                raw.append(lines[i])
                stripped.append(lines[i].strip())
                i += 1
            if len(stripped) >= min_lines:
                normalized = "\n".join(stripped)
                blocks.append(
                    Occurrence(
                        path=Path(),
                        start=start,
                        end=i - 1,
                        normalized=normalized,
                        raw_lines=list(raw),
                    )
                )
        else:
            i += 1
    return blocks


def extract_segment_blocks(lines: list[str], min_lines: int) -> list[Occurrence]:
    blocks: list[Occurrence] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].lstrip().startswith("#!"):
            i += 1
            continue
        start = i
        raw: list[str] = []
        stripped: list[str] = []
        while i < n and not lines[i].lstrip().startswith("#!"):
            raw.append(lines[i])
            stripped.append(lines[i].strip())
            i += 1
        if len(stripped) >= min_lines:
            normalized = "\n".join(stripped)
            blocks.append(
                Occurrence(
                    path=Path(),
                    start=start,
                    end=i - 1,
                    normalized=normalized,
                    raw_lines=list(raw),
                )
            )
    return blocks


def extract_blocks(path: Path, block_mode: str, min_lines: int, min_chars: int) -> list[Occurrence]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        warn(f"cannot read {path}: {e}")
        return []
    if block_mode == "paragraph":
        blocks = extract_paragraph_blocks(lines, min_lines, min_chars)
    elif block_mode == "comment":
        blocks = extract_comment_blocks(lines, min_lines)
    elif block_mode == "segment":
        blocks = extract_segment_blocks(lines, min_lines)
    else:
        msg = f"Unknown block-mode: {block_mode!r}"
        raise ValueError(msg)
    for b in blocks:
        b.path = path
    return blocks


def _worker_scan(args: tuple[Path, str, int, int]) -> tuple[Path, list[Occurrence]]:
    path, block_mode, min_lines, min_chars = args
    return path, extract_blocks(path, block_mode, min_lines, min_chars)


def scan_directory(
    root: Path,
    *,
    block_mode: str,
    min_lines: int,
    min_chars: int,
    extensions: Optional[set[str]],
    workers: int,
) -> dict[str, list[Occurrence]]:
    files = collect_files(root, block_mode=block_mode, extensions=extensions)
    if not files:
        info("No text files found.")
        return {}
    info(
        f"Scanning {len(files)} file(s) "
        f"(block-mode={block_mode}, min-lines={min_lines}, "
        f"min-chars={min_chars}, workers={workers})..."
    )
    groups: dict[str, list[Occurrence]] = defaultdict(list)
    if workers <= 1 or len(files) == 1:
        for f in files:
            _, blocks = _worker_scan((f, block_mode, min_lines, min_chars))
            for b in blocks:
                groups[b.normalized].append(b)
    else:
        jobs = [(f, block_mode, min_lines, min_chars) for f in files]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for path, blocks in ex.map(_worker_scan, jobs):
                for b in blocks:
                    groups[b.normalized].append(b)
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _preview(text: str, limit: int = 200) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


def print_report(groups: dict[str, list[Occurrence]]) -> None:
    if not groups:
        print("No repeated multiline blocks found.")
        return
    print(f"Found {len(groups)} repeated multiline block(s):")
    for i, (key, occ) in enumerate(groups.items(), 1):
        n_files = len({o.path for o in occ})
        n_lines = key.count("\n") + 1
        print(f"\n--- Block {i} ({len(occ)} occurrences in {n_files} file(s), {n_lines} line(s)) ---")
        for line in key.split("\n"):
            print(f"  {line}")
        print("  Found in:")
        for o in occ:
            print(f"    {o.path}:{o.start + 1}-{o.end + 1}")


def save_report(groups: dict[str, list[Occurrence]], path: Path) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            f.write("Repeated Multiline Blocks Report\n")
            f.write("=" * 40 + "\n\n")
            for i, (key, occ) in enumerate(groups.items(), 1):
                f.write(f"Block #{i} (found {len(occ)} times):\n")
                f.write("-" * 30 + "\n")
                f.write(key)
                f.write("\n\nLocations:\n")
                for o in occ:
                    f.write(f"  {o.path}:{o.start + 1}-{o.end + 1}\n")
                f.write("\n")
        info(f"Report saved to {path}")
    except OSError as e:
        err(f"Error writing report to {path}: {e}")


def _remove_lines(path: Path, line_indices: set[int], validate_python: bool) -> tuple[int, bool]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        warn(f"cannot read {path}: {e}")
        return 0, False
    new_lines = [l for i, l in enumerate(lines) if i not in line_indices]
    if len(new_lines) == len(lines):
        return 0, True
    if validate_python and path.suffix.lower() == ".py":
        try:
            ast.parse("".join(new_lines))
        except SyntaxError as e:
            warn(f"{path}: removal would create invalid Python ({e}); skipping")
            return 0, False
    try:
        with path.open("w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except OSError as e:
        err(f"Error writing {path}: {e}")
        return 0, False
    return len(lines) - len(new_lines), True


def remove_blocks(groups: dict[str, list[Occurrence]], validate_python: bool) -> tuple[int, int]:
    per_file: dict[Path, set[int]] = defaultdict(set)
    for occ_list in groups.values():
        for o in occ_list:
            per_file[o.path].update(range(o.start, o.end + 1))
    total_files = 0
    total_lines = 0
    for path, idxs in per_file.items():
        removed, ok = _remove_lines(path, idxs, validate_python)
        if ok and removed > 0:
            total_files += 1
            total_lines += removed
            print(f"Removed {removed} line(s) from {path}")
        elif ok:
            print(f"No changes to {path}")
    return total_files, total_lines


def _parse_extensions(raw: Optional[Sequence[str]]) -> Optional[set[str]]:
    if not raw:
        return None
    out: set[str] = set()
    for e in raw:
        if not e:
            continue
        out.add(e if e.startswith(".") else "." + e)
    return out


def _default_min_lines(block_mode: str) -> int:
    return 3 if block_mode == "paragraph" else 2


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"Directory {root} does not exist")
        return 1
    min_lines = args.min_lines if args.min_lines is not None else _default_min_lines(args.block_mode)
    extensions = _parse_extensions(args.extensions)
    groups = scan_directory(
        root,
        block_mode=args.block_mode,
        min_lines=min_lines,
        min_chars=args.min_chars,
        extensions=extensions,
        workers=MAX_WORKERS,
    )
    if args.half and groups:
        scanned = collect_files(root, block_mode=args.block_mode, extensions=extensions)
        threshold = len(scanned) / 2
        before = len(groups)
        groups = {k: v for k, v in groups.items() if len({o.path for o in v}) >= threshold}
        info(f"--half filter: {before} → {len(groups)} block(s) (>= {int(threshold)} files)")
    print_report(groups)
    if not args.no_report and groups:
        report_path = Path(args.report)
        save_report(groups, report_path)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"Directory {root} does not exist")
        return 1
    min_lines = args.min_lines if args.min_lines is not None else _default_min_lines(args.block_mode)
    extensions = _parse_extensions(args.extensions)
    groups = scan_directory(
        root,
        block_mode=args.block_mode,
        min_lines=min_lines,
        min_chars=args.min_chars,
        extensions=extensions,
        workers=MAX_WORKERS,
    )
    if args.half and groups:
        scanned = collect_files(root, block_mode=args.block_mode, extensions=extensions)
        threshold = len(scanned) / 2
        groups = {k: v for k, v in groups.items() if len({o.path for o in v}) >= threshold}
    if not groups:
        print("No repeated multiline blocks to remove.")
        return 0
    print(
        f"Found {len(groups)} repeated block(s) across {len({o.path for occ in groups.values() for o in occ})} file(s)."
    )
    if not args.yes:
        ans = input("Remove all occurrences? (yes/no): ").strip().lower()
        if ans not in ("yes", "y"):
            print("Operation cancelled.")
            return 1
    validate = not args.no_validate
    files, lines = remove_blocks(groups, validate_python=validate)
    print(f"\nDone. Removed {lines} line(s) across {files} file(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repeated_blocks.py",
        description="Find and remove repeated multiline blocks in text files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  mlic.py   ->  scan   --block-mode paragraph --min-lines 3 --min-chars 100\n"
            "  mlic2.py  ->  scan   --block-mode paragraph --min-lines 3 --min-chars 10\n"
            "  pylic.py  ->  scan   --block-mode comment   --min-lines 2\n"
            "  tlic.py   ->  scan   --block-mode segment   --min-lines 2\n"
            "  Any --remove flag in the originals corresponds to the `remove` subcommand.\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
        p.add_argument(
            "--block-mode",
            choices=("paragraph", "comment", "segment"),
            default="paragraph",
            help="How to slice blocks (default: paragraph)",
        )
        p.add_argument(
            "--min-lines",
            type=int,
            default=None,
            help="Minimum lines per block (default: 3 for paragraph, 2 otherwise)",
        )
        p.add_argument(
            "--min-chars",
            type=int,
            default=10,
            help="Minimum characters per block (paragraph only, default: 10)",
        )
        p.add_argument(
            "--half",
            action="store_true",
            help="Keep only blocks appearing in >=50%% of scanned files",
        )
        p.add_argument(
            "--extensions",
            nargs="+",
            default=None,
            help="Extension filter, e.g. .py .md (default: per block-mode)",
        )

    p = sub.add_parser("scan", help="Find repeated blocks and report them")
    add_common(p)
    p.add_argument(
        "-o",
        "--report",
        default="lic_report.txt",
        help="Report file path (default: lic_report.txt)",
    )
    p.add_argument("--no-report", action="store_true", help="Don't write the report file")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("remove", help="Find repeated blocks and remove them")
    add_common(p)
    p.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip Python syntax validation on removal",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt",
    )
    p.set_defaults(func=cmd_remove)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
