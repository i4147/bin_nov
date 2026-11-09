#!/data/data/com.termux/files/usr/bin/env python
"""HTML, CSS, JS and JSON formatter with several backends.
The ``format`` subcommand beautifies with tree-sitter (HTML), js-beautifier, prettier or prettify (``--tool``), the ``list-large`` one reports the big HTML files; it can move the failures with ``--move-errors-to`` and runs the batches with the chosen executor.
Usage: pyfmt {format,list-large} [options] [paths ...] Positional arguments: paths ...
Files or directories to scan (default: $HOME).
Subcommands: list-large List HTML files larger than a size threshold (bightml.py).
format Beautify / reformat files using the selected backend.
Options: -v, --verbose Enable debug logging.
--min-size-mb Minimum size in MiB (default: 1.0).
--tool Formatting backend.
-e, --ext Override file extensions (e.g.
--ext .js .css).
--executor Parallelism strategy (default: auto).
--dry-run List files that would be processed without modifying them.
--progress Show a tqdm progress bar (requires tqdm).
--move-errors-to Move failed files into a DIR next to them (pret4.py).
--skip-dir Directory names to skip while scanning.
--max-bytes Tree-sitter: skip files larger than this (default: 50 MiB).
...
plus 5 more; run ``pyfmt --help`` for the full list.
Examples: $ pyfmt format --tool prettier dist/ $ pyfmt list-large --min-size-mb 2 Command-line script: run ``python pyfmt.py`` or the ``pyfmt`` command.
The module is importable as well."""

import argparse
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Optional

from dh import MAX_WORKERS, atomic_write
from loguru import logger


LOG = logger
HTML_EXTS: set[str] = {".html", ".htm", ".xhtml"}
JSBEAUTIFIER_EXTS: set[str] = {".js", ".css", ".html", ".htm", ".json"}
PRETTIER_EXTS: set[str] = {
    ".js",
    ".css",
    ".html",
    ".json",
    ".mjs",
    ".cjs",
    ".ts",
    ".jsx",
    ".tsx",
}
PRETTIFY_EXTS: set[str] = {".html", ".htm", ".css", ".js"}
DEFAULT_EXTS_BY_TOOL: dict[str, set[str]] = {
    "treesitter": HTML_EXTS,
    "jsbeautifier": JSBEAUTIFIER_EXTS,
    "prettier": PRETTIER_EXTS,
    "prettify": PRETTIFY_EXTS,
}


@dataclass
class FileResult:
    path: Path
    success: bool
    modified: bool = False
    bytes_processed: int = 0
    detail: int = 0
    error: Optional[str] = None


def read_text(path: Path, max_bytes: int = 0) -> Optional[str]:
    try:
        if max_bytes and path.stat().st_size > max_bytes:
            LOG.warning("File too large ({} bytes): {}", path.stat().st_size, path)
            return None
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            LOG.debug("UTF-8 decode failed, trying latin-1: {}", path)
            return path.read_text(encoding="latin-1")
    except OSError as exc:
        LOG.error("Failed to read {}: {}", path, exc)
        return None


def discover(
    paths: Sequence[Path],
    exts: set[str],
    skip_dir_names: Iterable[str] = (),
) -> list[Path]:
    seen: set[Path] = set()
    skip = set(skip_dir_names)
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in exts and p.resolve() not in seen:
                seen.add(p.resolve())
                out.append(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in exts:
                    continue
                if skip and any(part in skip for part in f.parts):
                    continue
                key = f.resolve()
                if key in seen:
                    continue
                seen.add(key)
                out.append(f)
        else:
            LOG.warning("Path does not exist: {}", p)
    return out


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 1
    while True:
        cand = target.with_name(f"{stem}_{n}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def _move_to_error(path: Path, dest_dir: str) -> None:
    target_dir = path.parent / dest_dir
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(target_dir / path.name)
        shutil.move(str(path), str(dest))
    except Exception as exc:
        LOG.error("Failed to move {} -> {}: {}", path, target_dir, exc)


class HtmlTagFormatter:
    INLINE_TAGS: frozenset[str] = frozenset({
        "b",
        "i",
        "u",
        "em",
        "strong",
        "code",
        "small",
        "sub",
        "sup",
    })

    def __init__(self) -> None:
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_html as ts_html
        except ImportError as exc:
            msg = "Required packages not installed. Install with: pip install tree-sitter tree-sitter-html"
            raise RuntimeError(msg) from exc
        try:
            self._language = Language(ts_html.language())
            self._parser = Parser(self._language)
        except Exception as exc:
            msg = f"Failed to initialize Tree-sitter parser: {exc}"
            raise RuntimeError(msg) from exc

    def format(self, src: str) -> tuple[str, int]:
        if not src.strip():
            return src, 0
        try:
            tree = self._parser.parse(src.encode("utf-8"))
            edits = self._collect_edits(tree.root_node, src)
            if not edits:
                return src, 0
            edits.sort(key=lambda e: e[0], reverse=True)
            out = src
            for pos, replacement, length in edits:
                out = out[:pos] + replacement + out[pos + length :]
            return self._collapse_blank_lines(out), len(edits)
        except Exception as exc:
            LOG.error("Error formatting HTML: {}", exc)
            return src, 0

    def _collect_edits(self, root, src: str) -> list[tuple[int, str, int]]:
        edits: list[tuple[int, str, int]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "element":
                tag = self._tag_name(node, src)
                if tag and tag not in self.INLINE_TAGS:
                    start, end = node.start_byte, node.end_byte
                    line_start = src.rfind("\n", 0, start) + 1
                    prefix = src[line_start:start]
                    if prefix.strip() and not prefix.strip().endswith("\n"):
                        edits.append((start, "\n", 0))
                    nl = src.find("\n", end)
                    if nl == -1:
                        nl = len(src)
                    suffix = src[end:nl]
                    if suffix.strip():
                        edits.append((end, "\n", 0))
            for child in reversed(node.children):
                stack.append(child)
        return edits

    @staticmethod
    def _tag_name(element_node, src: str) -> Optional[str]:
        for child in element_node.children:
            if child.type == "start_tag":
                for sub in child.children:
                    if sub.type == "tag_name":
                        return src[sub.start_byte : sub.end_byte].lower()
        return None

    @staticmethod
    def _collapse_blank_lines(text: str) -> str:
        out: list[str] = []
        blank_run = 0
        for line in text.split("\n"):
            if line.strip():
                blank_run = 0
                out.append(line)
            else:
                blank_run += 1
                if blank_run <= 1:
                    out.append(line)
        return "\n".join(out)


_FORMATTER_CACHE: Optional[HtmlTagFormatter] = None


def _get_formatter() -> HtmlTagFormatter:
    global _FORMATTER_CACHE
    if _FORMATTER_CACHE is None:
        _FORMATTER_CACHE = HtmlTagFormatter()
    return _FORMATTER_CACHE


def _beautify_treesitter(path: Path, *, max_bytes: int = 50 * 1024 * 1024) -> FileResult:
    src = read_text(path, max_bytes=max_bytes)
    if src is None:
        return FileResult(path, success=False, error="Failed to read file")
    try:
        formatter = _get_formatter()
    except RuntimeError as exc:
        return FileResult(path, success=False, error=str(exc))
    out, count = formatter.format(src)
    modified = out != src
    if modified and not atomic_write(path, out):
        return FileResult(path, success=False, error="Failed to write file")
    return FileResult(
        path,
        success=True,
        modified=modified,
        bytes_processed=len(src),
        detail=count,
    )


def _beautify_jsbeautifier(
    path: Path,
    *,
    indent_size: int = 4,
    json_indent: int = 4,
) -> FileResult:
    src = read_text(path)
    if src is None:
        return FileResult(path, success=False, error="Failed to read file")
    ext = path.suffix.lower()
    try:
        if ext == ".json":
            try:
                data = json.loads(src)
            except json.JSONDecodeError as exc:
                return FileResult(path, success=False, error=f"Invalid JSON: {exc}")
            out = json.dumps(data, indent=json_indent)
        else:
            import jsbeautifier

            opts = jsbeautifier.default_options()
            opts.indent_size = indent_size
            if ext == ".css":
                out = jsbeautifier.css(src, opts)
            elif ext in (".html", ".htm"):
                out = jsbeautifier.html(src, opts)
            else:
                out = jsbeautifier.beautify(src, opts)
    except ImportError as exc:
        return FileResult(path, success=False, error=f"Missing dependency: {exc}")
    except Exception as exc:
        return FileResult(path, success=False, error=str(exc))
    modified = out != src
    if modified and not atomic_write(path, out):
        return FileResult(path, success=False, error="Failed to write file")
    return FileResult(path, success=True, modified=modified, bytes_processed=len(src))


def _beautify_prettier(
    path: Path,
    *,
    use_npx: bool = False,
    translate_path: bool = False,
    timeout: int = 300,
) -> FileResult:
    target = str(path)
    if translate_path:
        target = target.replace("/storage/emulated/0", "/sdcard")
    cmd = (["npx", "prettier", "--write"] if use_npx else ["prettier", "--write"]) + [target]
    try:
        before = path.stat().st_mtime_ns
    except OSError:
        before = 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return FileResult(path, success=False, error=f"prettier not found: {exc}")
    except subprocess.TimeoutExpired:
        return FileResult(path, success=False, error="Timeout")
    except Exception as exc:
        return FileResult(path, success=False, error=str(exc))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Unknown error").strip()
        return FileResult(path, success=False, error=err)
    try:
        after = path.stat().st_mtime_ns
    except OSError:
        after = before
    return FileResult(path, success=True, modified=(before != after))


def _beautify_prettify(path: Path) -> FileResult:
    src = read_text(path)
    if src is None:
        return FileResult(path, success=False, error="Failed to read file")
    ext = path.suffix.lower()
    try:
        if ext in (".html", ".htm"):
            from bs4 import BeautifulSoup

            out = BeautifulSoup(src, "html.parser").prettify()
        elif ext == ".css":
            import cssbeautifier

            out = cssbeautifier.beautify(src)
        elif ext == ".js":
            import yapf

            out, _ = yapf.yapf_api.FormatCode(src)
        else:
            return FileResult(path, success=True, modified=False)
    except ImportError as exc:
        return FileResult(path, success=False, error=f"Missing dependency: {exc}")
    except Exception as exc:
        return FileResult(path, success=False, error=str(exc))
    modified = out != src
    if modified and not atomic_write(path, out):
        return FileResult(path, success=False, error="Failed to write file")
    return FileResult(path, success=True, modified=modified, bytes_processed=len(src))


def run_batch(
    paths: Sequence[Path],
    worker: Callable[[Path], FileResult],
    *,
    jobs: int = 8,
    executor: str = "process",
    progress: bool = False,
    move_errors_dir: Optional[str] = None,
) -> dict[str, int]:
    stats = {"ok": 0, "fail": 0, "modified": 0, "bytes": 0, "detail": 0}
    pbar = None
    if progress:
        try:
            from tqdm import tqdm

            pbar = tqdm(total=len(paths), desc="Formatting", unit="file")
        except ImportError:
            LOG.warning("tqdm not installed; --progress will be ignored")

    def handle(result: FileResult) -> None:
        if result.success:
            stats["ok"] += 1
            stats["bytes"] += result.bytes_processed
            stats["detail"] += result.detail
            if result.modified:
                stats["modified"] += 1
                print(f"✓ Formatted: {result.path}")
            else:
                LOG.debug("✓ Already formatted: {}", result.path)
        else:
            stats["fail"] += 1
            LOG.error("✗ Failed: {} - {}", result.path, result.error)
            if move_errors_dir:
                _move_to_error(result.path, move_errors_dir)
        if pbar is not None:
            pbar.update(1)

    try:
        if executor == "serial" or jobs <= 1:
            for p in paths:
                handle(worker(p))
        elif executor == "thread":
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(worker, p): p for p in paths}
                for fut in as_completed(futures):
                    handle(fut.result())
        else:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(worker, p): p for p in paths}
                for fut in as_completed(futures):
                    handle(fut.result())
    finally:
        if pbar is not None:
            pbar.close()
    return stats


def cmd_list_large(args: argparse.Namespace) -> int:
    root = Path.home()
    min_bytes = int(args.min_size_mb * 1024 * 1024)
    paths = args.paths or [root]
    for f in discover(paths, HTML_EXTS):
        try:
            if f.stat().st_size > min_bytes:
                try:
                    shown = f.relative_to(root)
                except ValueError:
                    shown = f
                print(shown)
        except OSError as exc:
            LOG.debug("stat failed for {}: {}", f, exc)
    return 0


def _normalise_exts(raw: Optional[Sequence[str]], tool: str) -> set[str]:
    if not raw:
        return set(DEFAULT_EXTS_BY_TOOL[tool])
    out: set[str] = set()
    for e in raw:
        e = e.strip().lower()
        if not e:
            continue
        out.add(e if e.startswith(".") else "." + e)
    return out


def _make_worker(args: argparse.Namespace) -> Callable[[Path], FileResult]:
    tool = args.tool
    if tool == "treesitter":
        return partial(_beautify_treesitter, max_bytes=args.max_bytes)
    if tool == "jsbeautifier":
        return partial(
            _beautify_jsbeautifier,
            indent_size=args.indent_size,
            json_indent=args.json_indent,
        )
    if tool == "prettier":
        return partial(
            _beautify_prettier,
            use_npx=args.npx,
            translate_path=args.translate_path,
            timeout=args.timeout,
        )
    if tool == "prettify":
        return _beautify_prettify
    msg = f"Unknown tool: {tool}"
    raise ValueError(msg)


def cmd_format(args: argparse.Namespace) -> int:
    exts = _normalise_exts(args.ext, args.tool)
    skip_dirs = list(args.skip_dir or [])
    if args.move_errors_to and args.move_errors_to not in skip_dirs:
        skip_dirs.append(args.move_errors_to)
    targets = discover(args.paths or [Path.cwd()], exts, skip_dir_names=skip_dirs)
    if not targets:
        LOG.warning("No matching files found")
        return 0
    print(f"Found {len(targets)} file(s)")
    if args.dry_run:
        for p in targets:
            print(f"Would process: {p}")
        return 0
    worker = _make_worker(args)
    executor = args.executor
    if executor == "auto":
        executor = "thread" if args.tool == "prettier" else "process"
    try:
        stats = run_batch(
            targets,
            worker,
            jobs=MAX_WORKERS,
            executor=executor,
            progress=args.progress,
            move_errors_dir=args.move_errors_to,
        )
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        return 130
    total = stats["ok"] + stats["fail"]
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Files processed:  {total}")
    print(f"  Successful:     {stats['ok']}")
    print(f"  Failed:         {stats['fail']}")
    print(f"  Modified:       {stats['modified']}")
    print(f"  Unchanged:      {stats['ok'] - stats['modified']}")
    if stats["detail"]:
        print(f"Total tags formatted: {stats['detail']}")
    print(f"Total bytes processed: {stats['bytes']:,}")
    return 0 if stats["fail"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fmt",
        description="Unified HTML/CSS/JS/JSON formatter (merges 8 scripts).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  bightml.py     -> list-large --min-size-mb 1\n"
            "  htmlformat.py  -> format --tool treesitter\n"
            "  pret3.py       -> format --tool jsbeautifier --ext .js .css .html\n"
            "  pret4.py       -> format --tool prettier --move-errors-to error\n"
            "  pretp.py       -> format --tool prettier --npx --progress --executor thread\n"
            "  pretret.py     -> format --tool prettier --translate-path\n"
            "  prettify.py    -> format --tool prettify\n"
            "  pypret.py      -> format --tool jsbeautifier --ext .js .html .css .json"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)
    ll = sub.add_parser(
        "list-large",
        help="List HTML files larger than a size threshold (bightml.py).",
    )
    ll.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=None,
        help="Files or directories to scan (default: $HOME).",
    )
    ll.add_argument(
        "--min-size-mb",
        type=float,
        default=1.0,
        help="Minimum size in MiB (default: 1.0).",
    )
    fmt = sub.add_parser(
        "format",
        help="Beautify / reformat files using the selected backend.",
    )
    fmt.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=None,
        help="Files or directories to process (default: cwd).",
    )
    fmt.add_argument(
        "--tool",
        required=True,
        choices=["treesitter", "jsbeautifier", "prettier", "prettify"],
        help="Formatting backend.",
    )
    fmt.add_argument(
        "-e",
        "--ext",
        nargs="*",
        default=None,
        help="Override file extensions (e.g. --ext .js .css).",
    )
    fmt.add_argument(
        "--executor",
        choices=["auto", "process", "thread", "serial"],
        default="auto",
        help="Parallelism strategy (default: auto).",
    )
    fmt.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be processed without modifying them.",
    )
    fmt.add_argument(
        "--progress",
        action="store_true",
        help="Show a tqdm progress bar (requires tqdm).",
    )
    fmt.add_argument(
        "--move-errors-to",
        default=None,
        metavar="DIR",
        help="Move failed files into a DIR next to them (pret4.py).",
    )
    fmt.add_argument(
        "--skip-dir",
        nargs="*",
        default=None,
        metavar="NAME",
        help="Directory names to skip while scanning.",
    )
    fmt.add_argument(
        "--max-bytes",
        type=int,
        default=50 * 1024 * 1024,
        help="tree-sitter: skip files larger than this (default: 50 MiB).",
    )
    fmt.add_argument(
        "--indent-size",
        type=int,
        default=4,
        help="jsbeautifier: indentation width (default: 4).",
    )
    fmt.add_argument(
        "--json-indent",
        type=int,
        default=4,
        help="jsbeautifier: indent for JSON re-serialization (default: 4).",
    )
    fmt.add_argument(
        "--npx",
        action="store_true",
        help="prettier: invoke via `npx prettier` (pretp.py).",
    )
    fmt.add_argument(
        "--translate-path",
        action="store_true",
        help="prettier: rewrite /storage/emulated/0 -> /sdcard (pretret.py).",
    )
    fmt.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="prettier: per-file subprocess timeout in seconds (default: 300).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-large":
        return cmd_list_large(args)
    if args.command == "format":
        return cmd_format(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
