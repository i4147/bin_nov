#!/data/data/com.termux/files/usr/bin/env python
"""Remove inline (trailing) comments from Python files in place with LibCST."""


import argparse
import ast
import contextlib
import io
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import tokenize
from typing import Any, Iterable, Iterator, Sequence

from dh import NUM_WORKERS, SKIP_DIRS
import libcst as cst


CHUNKSIZE: int = 4


class InlineCommentRemover(cst.CSTTransformer):
    def __init__(self) -> None:
        super().__init__()
        self.comments_removed: int = 0

    def leave_TrailingWhitespace(
        self,
        original_node: cst.TrailingWhitespace,
        updated_node: cst.TrailingWhitespace,
    ) -> cst.TrailingWhitespace:
        if updated_node.comment is None:
            return updated_node
        self.comments_removed += 1
        return updated_node.with_changes(
            whitespace=cst.SimpleWhitespace(""),
            comment=None,
        )


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        mode: int | None = path.stat().st_mode
    except OSError:
        mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def process_file(path: Path) -> tuple[Path, int, str | None]:
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        return path, 0, f"read error: {exc}"
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source_bytes).readline)
        source = source_bytes.decode(encoding)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return path, 0, f"encoding error: {exc}"
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return path, 0, f"libcst parse error: {exc}"
    except Exception as exc:
        return path, 0, f"parse error: {type(exc).__name__}: {exc}"
    transformer = InlineCommentRemover()
    try:
        new_module = module.visit(transformer)
    except Exception as exc:
        return path, 0, f"transform error: {type(exc).__name__}: {exc}"
    if transformer.comments_removed == 0:
        return path, 0, None
    new_source = new_module.code
    try:
        ast.parse(new_source, filename=str(path))
    except SyntaxError as exc:
        return path, 0, f"post-transform validation failed: {exc}"
    try:
        _atomic_write(path, new_source.encode(encoding))
    except (OSError, UnicodeEncodeError) as exc:
        return path, 0, f"write error: {exc}"
    return path, transformer.comments_removed, None


def iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()

    def _on_error(exc: OSError) -> None:
        print(f"warning: {exc}", file=sys.stderr)

    for root in roots:
        try:
            if root.is_file():
                if root.suffix == ".py":
                    key = root.resolve()
                    if key not in seen:
                        seen.add(key)
                        yield root
            elif root.is_dir():
                for dirpath, dirnames, filenames in root.walk(on_error=_on_error):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in filenames:
                        if not name.endswith(".py"):
                            continue
                        candidate = dirpath / name
                        try:
                            key = candidate.resolve()
                        except OSError:
                            continue
                        if key in seen:
                            continue
                        seen.add(key)
                        yield candidate
            else:
                print(f"warning: skipping non-existent path: {root}", file=sys.stderr)
        except OSError as exc:
            print(f"warning: cannot access {root}: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strip_inline_comments",
        description=(
            "Remove inline (trailing) comments from Python files using libcst. "
            "Files are modified in place. Standalone comments are preserved."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help=("Files or directories to process. Defaults to the current directory, walked recursively."),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots: list[Path] = args.paths or [Path.cwd()]
    total_files = 0
    changed_files = 0
    total_removed = 0
    error_count = 0
    with mp.Pool(processes=NUM_WORKERS) as pool:
        results = pool.imap_unordered(
            process_file,
            iter_python_files(roots),
            chunksize=CHUNKSIZE,
        )
        for path, removed, error in results:
            total_files += 1
            if error is not None:
                error_count += 1
                print(f"ERROR  {path}: {error}", file=sys.stderr)
            elif removed > 0:
                changed_files += 1
                total_removed += removed
                print(f"{path}: removed {removed} inline comment(s)")
    if total_files == 0:
        print("No Python files found.", file=sys.stderr)
        return 1
    summary = (
        f"\nProcessed {total_files} file(s): "
        f"{changed_files} changed, "
        f"{total_removed} inline comment(s) removed, "
        f"{error_count} error(s)."
    )
    print(summary, file=sys.stderr if error_count else sys.stdout)
    return 2 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
