#!/data/data/com.termux/files/usr/bin/env python
"""Beautify HTML files in parallel with js-beautify.
Runs ``html-beautify`` on the ``.html``/``.htm``/``.xhtml`` files (plus the suffixes passed with ``--ext``) through a temporary file and eight workers, logging only the warnings with ``--quiet``.
Usage: bhtml [options] [paths ...] Positional arguments: paths ...
Files or directories to process Options: --ext Extra file suffix to include (repeatable), e.g.
--ext .vue -q, --quiet Only log warnings and errors.
Examples: $ bhtml --ext .vue templates/ Command-line script: run ``python bhtml.py`` or the ``bhtml`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable, Iterator

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


BEAUTIFIER: tuple[str, ...] = ("html-beautify", "--quiet")
DEFAULT_SUFFIXES: tuple[str, ...] = (".html", ".htm", ".xhtml")
DEFAULT_JOBS: int = 8
TMP_SUFFIX: str = "..tmp"


def _is_html(path: Path, suffixes: frozenset[str]) -> bool:
    return path.suffix.lower() in suffixes


def iter_html_files(root: Path, suffixes: frozenset[str]) -> Iterator[Path]:
    if root.is_symlink():
        logger.debug("skip symlink: {}", root)
        return
    if root.is_file():
        if _is_html(root, suffixes):
            yield root
        return
    if not root.is_dir():
        logger.warning("not a file or directory: {}", root)
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as exc:
            logger.error("cannot scan {}: {}", current, exc)
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    candidate = Path(entry.path)
                    if _is_html(candidate, suffixes):
                        yield candidate
            except OSError as exc:
                logger.error("stat failed for {}: {}", entry.path, exc)


def collect_targets(raw_paths: Iterable[str], suffixes: frozenset[str]) -> list[Path]:
    seen: set[Path] = set()
    targets: list[Path] = []
    for raw in raw_paths:
        for path in iter_html_files(Path(raw).expanduser(), suffixes):
            try:
                key = path.resolve(strict=False)
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            targets.append(path)
    return targets


def beautify(path: Path) -> Path:
    tmp = path.with_name(path.name + TMP_SUFFIX)
    try:
        with path.open("rb") as fin, tmp.open("wb") as fout:
            proc = subprocess.run(
                BEAUTIFIER,
                stdin=fin,
                stdout=fout,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            msg = f"{BEAUTIFIER[0]} exited with {proc.returncode}: {stderr}"
            raise RuntimeError(msg)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beautify-html",
        description="Parallel HTML beautifier via html-beautify (js-beautify).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process",
    )
    parser.add_argument(
        "--ext",
        action="append",
        default=None,
        metavar="SUFFIX",
        help="Extra file suffix to include (repeatable), e.g. --ext .vue",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only log warnings and errors.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger.remove()
    logger.add(
        sys.stderr,
        level="WARNING" if args.quiet else "INFO",
        format="<level>{level: <8}</level> | <level>{message}</level>",
    )
    if shutil.which(BEAUTIFIER[0]) is None:
        logger.critical(
            "'{}' not found. Install it with: npm install -g js-beautify",
            BEAUTIFIER[0],
        )
        return 127
    if MAX_WORKERS < 1:
        logger.critical("--jobs must be >= 1 (got {})", MAX_WORKERS)
        return 2
    suffixes: frozenset[str] = (
        frozenset(s.lower() if s.startswith(".") else f".{s.lower()}" for s in (args.ext or DEFAULT_SUFFIXES))
        if args.ext
        else frozenset(DEFAULT_SUFFIXES)
    )
    targets = collect_targets(args.paths or ["."], suffixes)
    if not targets:
        logger.warning("No HTML files found for: {}", ", ".join(args.paths or ["."]))
        return 0
    logger.info("Beautifying {} file(s) with {} workers", len(targets), MAX_WORKERS)
    failures: list[tuple[Path, BaseException]] = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(beautify, p): p for p in targets}
        for future in as_completed(futures):
            path = futures[future]
            try:
                future.result()
                logger.success("beautified: {}", path)
            except BaseException as exc:
                failures.append((path, exc))
                logger.error("failed: {} -> {}", path, exc)
    if failures:
        logger.critical("{} file(s) failed to beautify", len(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
