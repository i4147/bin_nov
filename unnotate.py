#!/data/data/com.termux/files/usr/bin/env python
"""Remove Python type annotations from files in place."""

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import multiprocessing as mp
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS
import libcst as cst
from rich.console import Console
from rich.text import Text


TYPE_COMMENT_RE: re.Pattern[str] = re.compile(r"#\s*type:\s*(?!ignore\b)")
NUM_WIDTH: int = 10
SUFFIX: str = ""
MIN_PATH_WIDTH: int = 10


@dataclass(frozen=True, slots=True)
class Result:
    path: Path
    changed: bool
    error: str | None
    reduced: int


def strip_small_statements(
    body: Sequence[cst.BaseSmallStatement],
) -> list[cst.BaseSmallStatement]:
    out: list[cst.BaseSmallStatement] = []
    removed: bool = False
    for stmt in body:
        if isinstance(stmt, cst.AnnAssign):
            if stmt.value is None:
                removed = True
                continue
            out.append(
                cst.Assign(
                    targets=[cst.AssignTarget(target=stmt.target)],
                    value=stmt.value,
                    semicolon=stmt.semicolon,
                )
            )
        else:
            out.append(stmt)
    if removed and out:
        out[-1] = out[-1].with_changes(semicolon=cst.MaybeSentinel.DEFAULT)
    return out


class AnnotationStripper(cst.CSTTransformer):
    def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.Param:
        if updated_node.annotation is None:
            return updated_node
        equal: cst.AssignEqual | cst.MaybeSentinel = updated_node.equal
        if (
            isinstance(equal, cst.AssignEqual)
            and isinstance(equal.whitespace_before, cst.SimpleWhitespace)
            and isinstance(equal.whitespace_after, cst.SimpleWhitespace)
        ):
            equal = cst.AssignEqual(
                whitespace_before=cst.SimpleWhitespace(""),
                whitespace_after=cst.SimpleWhitespace(""),
            )
        return updated_node.with_changes(annotation=None, equal=equal)

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        if updated_node.returns is None:
            return updated_node
        return updated_node.with_changes(returns=None)

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
        body: list[cst.BaseSmallStatement] = strip_small_statements(updated_node.body)
        if not body:
            return cst.RemoveFromParent()
        return updated_node.with_changes(body=body)

    def leave_SimpleStatementSuite(
        self, original_node: cst.SimpleStatementSuite, updated_node: cst.SimpleStatementSuite
    ) -> cst.SimpleStatementSuite:
        body: list[cst.BaseSmallStatement] = strip_small_statements(updated_node.body)
        if not body:
            body = [cst.Pass()]
        return updated_node.with_changes(body=body)

    def leave_IndentedBlock(
        self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock
    ) -> cst.IndentedBlock:
        if updated_node.body:
            return updated_node
        return updated_node.with_changes(body=[cst.SimpleStatementLine(body=[cst.Pass()])])

    def leave_Comment(self, original_node: cst.Comment, updated_node: cst.Comment) -> cst.Comment | cst.RemovalSentinel:
        if TYPE_COMMENT_RE.match(updated_node.value):
            return cst.RemoveFromParent()
        return updated_node

    def leave_TrailingWhitespace(
        self, original_node: cst.TrailingWhitespace, updated_node: cst.TrailingWhitespace
    ) -> cst.TrailingWhitespace:
        if original_node.comment is not None and updated_node.comment is None:
            return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""))
        return updated_node


def process_file(path_str: str) -> Result:
    path: Path = Path(path_str)
    try:
        src: bytes = path.read_bytes()
    except OSError as exc:
        return Result(path, False, f"read error: {exc}", 0)
    try:
        module: cst.Module = cst.parse_module(src)
    except Exception as exc:
        return Result(path, False, f"parse error: {exc}", 0)
    try:
        new: bytes = module.visit(AnnotationStripper()).bytes
    except Exception as exc:
        return Result(path, False, f"transform error: {exc}", 0)
    if new == src:
        return Result(path, False, None, 0)
    try:
        atomic_write(path, new)
    except OSError as exc:
        return Result(path, False, f"write error: {exc}", 0)
    return Result(path, True, None, len(src) - len(new))


def walk_dir(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath, name)


def gather_files(paths: Sequence[str]) -> tuple[list[Path], list[str]]:
    targets: list[str] = list(paths) if paths else ["."]
    found: set[Path] = set()
    missing: list[str] = []
    for raw in targets:
        p: Path = Path(raw)
        if p.is_dir():
            found.update(f.resolve() for f in walk_dir(p))
        elif p.is_file():
            found.add(p.resolve())
        else:
            missing.append(raw)
    return sorted(found), missing


def display_path(path: Path) -> str:
    try:
        return Path(path).relative_to(Path())
    except ValueError:
        return str(path)


def fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return "…" + text[-(width - 1) :]


def format_line(
    index: int,
    total: int,
    result: Result,
    counter_width: int,
    path_width: int,
) -> Text:
    line: Text = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"[{index}/{total}]".ljust(counter_width), style="dim")
    line.append("  ")
    line.append(fit(display_path(result.path), path_width))
    line.append("  ")
    if result.error is not None:
        line.append("error".rjust(NUM_WIDTH), style="bold red")
        line.append(" -> ")
        line.append(result.error, style="red")
        return line
    number: str = f"{result.reduced:,}".rjust(NUM_WIDTH)
    if result.reduced > 0:
        line.append(number, style="green")
    elif result.reduced < 0:
        line.append(number, style="red")
    else:
        line.append(number, style="dim")
    line.append(SUFFIX, style="dim" if result.reduced == 0 else "")
    return line


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Remove Python type annotations from files in place."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to process (default: current directory, recursively)",
    )
    args: argparse.Namespace = parser.parse_args(argv)
    console: Console = Console(highlight=False, markup=False)
    err_console: Console = Console(stderr=True, highlight=False, markup=False)
    files: list[Path]
    missing: list[str]
    files, missing = gather_files(args.paths)
    for m in missing:
        err_console.print(Text(f"error: {m} -> not found", style="red"))
    if not files:
        err_console.print("No .py files found.")
        return 2 if missing else 1
    total: int = len(files)
    counter_width: int = len(f"[{total}/{total}]")
    longest: int = max(len(display_path(f)) for f in files)
    overhead: int = counter_width + 2 + 2 + NUM_WIDTH + len(SUFFIX)
    path_width: int = max(MIN_PATH_WIDTH, min(longest, console.size.width - overhead))
    workers: int = max(1, min(MAX_WORKERS, total))
    results: list[Result] = []
    try:
        with mp.Pool(processes=workers) as pool:
            for i, res in enumerate(pool.imap_unordered(process_file, [str(f) for f in files]), start=1):
                results.append(res)
                console.print(
                    format_line(i, total, res, counter_width, path_width),
                    no_wrap=True,
                    overflow="ellipsis",
                )
    except KeyboardInterrupt:
        err_console.print("Interrupted.")
        return 130
    updated: int = sum(1 for r in results if r.changed)
    failed: int = sum(1 for r in results if r.error is not None)
    unchanged: int = len(results) - updated - failed
    saved: int = sum(r.reduced for r in results)
    summary: Text = Text()
    summary.append("\nSummary: ", style="bold")
    summary.append(f"processed={len(results)} ")
    summary.append(f"updated={updated} ", style="green" if updated else "dim")
    summary.append(f"no-change={unchanged} ", style="dim")
    summary.append(f"errors={failed + len(missing)} ", style="red" if failed or missing else "dim")
    summary.append(f"bytes-reduced={saved:,}", style="green" if saved > 0 else "dim")
    console.print(summary)
    return 2 if failed or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
