#!/data/data/com.termux/files/usr/bin/env python
"""Normalise shebangs to Termux paths, including extensionless scripts."""

from __future__ import annotations
import argparse
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
import difflib
from enum import StrEnum
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import TYPE_CHECKING, Final

from dh import SKIP_DIRS, atomic_write
import libcst as cst
from loguru import logger


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

TERMUX_BIN: Final[str] = "/data/data/com.termux/files/usr/bin"
MAX_WORKERS: Final[int] = min(8, os.cpu_count() or 4)
SNIFF_BYTES: Final[int] = 1024

# Conservative content-based detection for extensionless Python scripts that
# have no shebang. Requires *both* a Python-looking line and a successful
# libcst parse, which keeps false positives from prose/config files out.
PY_INDICATORS: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"\s*import\s+[\w.]+"
    r"|\s*from\s+[\w.]+\s+import\s+"
    r"|\s*def\s+\w+\s*\("
    r"|\s*class\s+\w+\s*[:(]"
    r"|\s*if\s+__name__\s*=="
    r"|\s*@\w+"
    r")",
    re.MULTILINE,
)


class Status(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Language:
    name: str
    shebang: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class Result:
    path: Path
    status: Status
    detail: str = ""
    diff: str = ""


@dataclass(frozen=True, slots=True)
class Config:
    dry_run: bool = False
    assume: str | None = None
    validate_python: bool = True
    backup: bool = False
    diff: bool = False


LANGUAGES: Final[dict[str, Language]] = {
    ".py": Language(
        "python",
        f"#!{TERMUX_BIN}/env python",
        re.compile(r"^#!.*\bpython[23]?(?:\.\d+)?\b"),
    ),
    ".zsh": Language("zsh", f"#!{TERMUX_BIN}/zsh", re.compile(r"^#!.*\bzsh\b")),
    ".sh": Language(
        "sh", f"#!{TERMUX_BIN}/sh", re.compile(r"^#!.*\b(?:sh|bash|dash|ksh)\b")
    ),
    ".rb": Language(
        "ruby", f"#!{TERMUX_BIN}/env ruby", re.compile(r"^#!.*\bruby[\d.]*\b")
    ),
    ".pl": Language(
        "perl", f"#!{TERMUX_BIN}/env perl", re.compile(r"^#!.*\bperl[\d.]*\b")
    ),
}

# Maps ``--assume`` values to the canonical suffix key in ``LANGUAGES``.
ASSUME_TO_SUFFIX: Final[dict[str, str]] = {
    "python": ".py",
    "sh": ".sh",
    "zsh": ".zsh",
    "ruby": ".rb",
    "perl": ".pl",
}


def display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def detect_language(first_line: str) -> Language | None:
    return next(
        (lang for lang in LANGUAGES.values() if lang.pattern.search(first_line)),
        None,
    )


def detect_by_content(text: str) -> Language | None:
    """Detect extensionless Python scripts that lack a shebang.

    Both an indicator line *and* a successful libcst parse are required so
    that prose, config files, and other plain text stay untouched.
    """
    if not text.strip():
        return None
    if PY_INDICATORS.search(text) is None:
        return None
    try:
        cst.parse_module(text)
    except cst.ParserSyntaxError:
        return None
    return LANGUAGES[".py"]


def sniff_binary(path: Path) -> str | None:
    """Return a skip reason if ``path`` looks binary, else ``None``.

    Raises :class:`OSError` if the file cannot be opened.
    """
    with path.open("rb") as handle:
        sample = handle.read(SNIFF_BYTES)
    if b"\x00" in sample:
        return "binary file"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.end < len(sample):
            return "binary file (invalid UTF-8)"
    return None


def process_file(path: Path, config: Config) -> Result:
    if path.is_symlink():
        return Result(path, Status.SKIPPED, "symlink")
    has_suffix = bool(path.suffix)
    if not has_suffix:
        try:
            reason = sniff_binary(path)
        except OSError as exc:
            return Result(path, Status.ERROR, str(exc))
        if reason is not None:
            return Result(path, Status.SKIPPED, reason)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Result(path, Status.ERROR, str(exc))

    head, _, body = text.partition("\n")
    first_line = head.rstrip("\r")

    if has_suffix:
        language = LANGUAGES.get(path.suffix.lower())
        if language is None:
            return Result(path, Status.SKIPPED, "unsupported extension")
    elif first_line.startswith("#!"):
        language = detect_language(first_line)
        if language is None:
            return Result(path, Status.SKIPPED, "unsupported shebang")
    elif config.assume is not None:
        suffix = ASSUME_TO_SUFFIX.get(config.assume)
        if suffix is None:
            return Result(path, Status.SKIPPED, f"unknown --assume: {config.assume}")
        language = LANGUAGES[suffix]
    else:
        detected = detect_by_content(text)
        if detected is None:
            return Result(path, Status.SKIPPED, "no shebang, no recognised content")
        language = detected

    if first_line.startswith("#!"):
        if language.pattern.search(first_line) is None:
            return Result(path, Status.SKIPPED, f"shebang is not {language.name}")
        if first_line == language.shebang:
            return Result(path, Status.UNCHANGED)
        status = Status.UPDATED
        new_text = f"{language.shebang}\n{body}"
    else:
        status = Status.ADDED
        new_text = f"{language.shebang}\n{text}"

    if language.name == "python" and config.validate_python:
        try:
            cst.parse_module(new_text)
        except cst.ParserSyntaxError as exc:
            return Result(path, Status.ERROR, f"libcst parse failed: {exc}")

    diff_text = ""
    if config.diff and status is not Status.UNCHANGED:
        diff_text = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
                n=3,
            )
        )

    if config.dry_run:
        return Result(path, status, diff=diff_text)

    try:
        if config.backup:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        mode = stat.S_IMODE(path.stat().st_mode)
        written = atomic_write(path, new_text, mode)
    except OSError as exc:
        return Result(path, Status.ERROR, str(exc))
    if not written:
        return Result(path, Status.ERROR, "atomic_write reported failure")
    return Result(path, status, diff=diff_text)


def walk_dir(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts[:-1]
        if any(part in SKIP_DIRS for part in relative_parts):
            continue
        if path.is_file() or path.is_symlink():
            yield path


def iter_targets(inputs: Iterable[Path]) -> Iterator[Path]:
    for item in inputs:
        if item.is_dir() and not item.is_symlink():
            yield from walk_dir(item)
        elif item.is_file() or item.is_symlink():
            yield item
        else:
            logger.warning("Path not found: {}", item)


def make_executor(workers: int) -> Executor:
    return ThreadPoolExecutor(max_workers=workers)


@dataclass(frozen=True, slots=True)
class Args:
    paths: list[Path]
    dry_run: bool
    workers: int
    log_level: str
    assume: str | None
    validate: bool
    check: bool
    backup: bool
    diff: bool
    extensions: tuple[str, ...]
    exclude: tuple[str, ...]


def parse_args(argv: list[str] | None) -> Args:
    parser = argparse.ArgumentParser(
        description="Normalise shebangs to Termux paths.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, help="Files or directories (default: .)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report changes without writing"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Like --dry-run, but exit 1 if any file would be modified",
    )
    parser.add_argument(
        "--workers", type=int, default=MAX_WORKERS, help="Parallel workers"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--assume",
        choices=sorted(ASSUME_TO_SUFFIX),
        default=None,
        help="Assume this language for extensionless files lacking a shebang",
    )
    parser.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        help="Skip libcst validation for Python files",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Write <name>.bak alongside every modified file",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print unified diffs for every modification",
    )
    parser.add_argument(
        "--extensions",
        action="append",
        default=[],
        metavar="EXT",
        help="Restrict to these extensions (repeatable, e.g. --extensions .py)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude paths matching GLOB (repeatable)",
    )
    ns = parser.parse_args(argv)
    return Args(
        paths=list(ns.paths),
        dry_run=bool(ns.dry_run),
        workers=int(ns.workers),
        log_level=str(ns.log_level),
        assume=ns.assume,
        validate=bool(ns.validate),
        check=bool(ns.check),
        backup=bool(ns.backup),
        diff=bool(ns.diff),
        extensions=tuple(ns.extensions),
        exclude=tuple(ns.exclude),
    )


def filter_targets(targets: list[Path], args: Args) -> list[Path]:
    if args.extensions:
        exts = {
            e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.extensions
        }
        targets = [p for p in targets if p.suffix.lower() in exts]
    if args.exclude:
        targets = [p for p in targets if not any(p.match(pat) for pat in args.exclude)]
    return targets


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.remove()
    logger.add(
        sys.stderr,
        level=args.log_level,
        format="<level>{level: <8}</level> {message}",
    )

    inputs: list[Path] = args.paths or [Path()]
    targets = filter_targets(list(dict.fromkeys(iter_targets(inputs))), args)
    if not targets:
        logger.warning("No candidate files found")
        return 0

    dry_run = args.dry_run or args.check
    config = Config(
        dry_run=dry_run,
        assume=args.assume,
        validate_python=args.validate,
        backup=args.backup,
        diff=args.diff,
    )

    counts: dict[Status, int] = dict.fromkeys(Status, 0)
    error_count = 0

    with make_executor(args.workers) as executor:
        futures = [executor.submit(process_file, path, config) for path in targets]
        for path, future in zip(targets, futures, strict=True):
            try:
                result = future.result()
            except Exception as exc:
                error_count += 1
                logger.opt(exception=exc).error("{}: worker failed", display(path))
                continue
            counts[result.status] += 1
            name = display(result.path)
            if result.diff:
                sys.stderr.write(result.diff)
            match result.status:
                case Status.ERROR:
                    error_count += 1
                    logger.error("{}: {}", name, result.detail)
                case Status.SKIPPED | Status.UNCHANGED:
                    logger.debug("{}: {} {}", name, result.status.value, result.detail)
                case _:
                    logger.info("{} {}", result.status.value, name)

    logger.info(
        "Summary: updated={} added={} unchanged={} skipped={} errors={} total={}",
        counts[Status.UPDATED],
        counts[Status.ADDED],
        counts[Status.UNCHANGED],
        counts[Status.SKIPPED],
        error_count,
        len(targets),
    )

    if args.check:
        changed = counts[Status.ADDED] + counts[Status.UPDATED]
        return 1 if changed or error_count else 0
    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
