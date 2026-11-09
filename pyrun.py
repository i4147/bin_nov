#!/data/data/com.termux/files/usr/bin/env python
"""Run every Python file of a directory with a per-file timeout, in parallel.
Discovers the ``.py`` files, executes each one with a subprocess bounded by ``--timeout`` (30 s default), classifies the outcomes (ok, error, timeout) and prints the per-file result and the summary.
Usage: pyrun DIRECTORY [--timeout SECONDS] [--no-recursive] [-v] Positional arguments: directory Directory to search for Python files.
Options: --no-recursive Do not descend into subdirectories.
--timeout --verbose Enable verbose logging.
Examples: $ pyrun examples/ --timeout 10 Command-line script: run ``python pyrun.py`` or the ``pyrun`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing import Pool
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from dh import MAX_WORKERS
from loguru import logger


DEFAULT_TIMEOUT: float = 30.0
MAX_ERROR_MSG_LEN: int = 200


class Outcome(str, Enum):
    SUCCESS = "success"
    MODULE_NOT_FOUND = "ModuleNotFoundError"
    IMPORT_ERROR = "ImportError"
    SYNTAX_ERROR = "SyntaxError"
    ATTRIBUTE_ERROR = "AttributeError"
    TYPE_ERROR = "TypeError"
    VALUE_ERROR = "ValueError"
    KEYBOARD_INTERRUPT = "KeyboardInterrupt"
    TIMEOUT = "TimeoutError"
    OTHER_ERROR = "OtherError"


@dataclass(slots=True)
class FileResult:
    path: Path
    outcome: Outcome
    returncode: int | None = None
    stderr: str = ""
    duration: float = 0.0


@dataclass(slots=True)
class Summary:
    results: list[FileResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.SUCCESS)

    @property
    def failed(self) -> int:
        return self.total - self.succeeded

    def counts_by_outcome(self) -> dict[Outcome, int]:
        counts: dict[Outcome, int] = {}
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts


def discover_python_files(directory: Path, recursive: bool) -> list[Path]:
    pattern = "*.py"
    if recursive:
        return sorted(p for p in directory.rglob(pattern) if p.is_file())
    return sorted(p for p in directory.glob(pattern) if p.is_file())


def _classify_failure(stderr: str, returncode: int | None) -> Outcome:
    lower_stderr = stderr.lower()
    if "modulenotfounderror" in lower_stderr or "no module named" in lower_stderr:
        return Outcome.MODULE_NOT_FOUND
    if "importerror" in lower_stderr:
        return Outcome.IMPORT_ERROR
    if "syntaxerror" in lower_stderr:
        return Outcome.SYNTAX_ERROR
    if "attributeerror" in lower_stderr:
        return Outcome.ATTRIBUTE_ERROR
    if "typeerror" in lower_stderr:
        return Outcome.TYPE_ERROR
    if "valueerror" in lower_stderr:
        return Outcome.VALUE_ERROR
    if "keyboardinterrupt" in lower_stderr or returncode in (-2, 130):
        return Outcome.KEYBOARD_INTERRUPT
    return Outcome.OTHER_ERROR


def run_file(path: Path, timeout: float) -> FileResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(path.parent),
        )
    except subprocess.TimeoutExpired:
        return FileResult(
            path=path,
            outcome=Outcome.TIMEOUT,
            duration=time.monotonic() - start,
        )
    except KeyboardInterrupt:
        return FileResult(
            path=path,
            outcome=Outcome.KEYBOARD_INTERRUPT,
            duration=time.monotonic() - start,
        )
    except Exception as exc:
        return FileResult(
            path=path,
            outcome=Outcome.OTHER_ERROR,
            stderr=str(exc),
            duration=time.monotonic() - start,
        )
    duration = time.monotonic() - start
    if completed.returncode == 0:
        return FileResult(
            path=path,
            outcome=Outcome.SUCCESS,
            returncode=0,
            duration=duration,
        )
    err_output = (completed.stderr or completed.stdout or "").strip()
    outcome = _classify_failure(err_output, completed.returncode)
    return FileResult(
        path=path,
        outcome=outcome,
        returncode=completed.returncode,
        stderr=err_output,
        duration=duration,
    )


def _log_result(result: FileResult, verbose: bool) -> None:
    if result.outcome is Outcome.SUCCESS:
        if verbose:
            logger.success(f"OK   {result.path} ({result.duration:.2f}s)")
    else:
        logger.error(f"FAIL {result.path} -> {result.outcome.value} (rc={result.returncode}, {result.duration:.2f}s)")
        if verbose and result.stderr:
            msg = result.stderr
            if len(msg) > MAX_ERROR_MSG_LEN:
                msg = msg[:MAX_ERROR_MSG_LEN] + "..."
            logger.debug(f"   {msg}")


def report_summary(summary: Summary, elapsed: float) -> None:
    print("=" * 60)
    print(f"Summary: {summary.total} file(s), {summary.succeeded} succeeded, {summary.failed} failed in {elapsed:.2f}s")
    counts = summary.counts_by_outcome()
    for outcome in Outcome:
        count = counts.get(outcome)
        if count:
            print(f"  {outcome.value:<22} {count}")
    print("=" * 60)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyrunner",
        description="Recursively find and execute all .py files in a directory with a per-file timeout, in parallel.",
    )
    parser.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=Path(),
        help="Directory to search for Python files.",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Per-file timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable verbose logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    recursive = not args.no_recursive
    timeout = args.timeout
    verbose = args.verbose
    if not directory.exists() or not directory.is_dir():
        logger.error(f"Not a valid directory: {directory}")
        return 2
    files = discover_python_files(directory, recursive)
    if not files:
        logger.warning(f"No Python files found in {directory}")
        return 0
    mode = "recursively" if recursive else "non-recursively"
    print(f"Scanning {mode} in: {directory}")
    print(f"Found {len(files)} Python file(s) | Using {MAX_WORKERS} workers with {timeout}s timeout")
    print("-" * 60)
    summary = Summary()
    start_time = time.monotonic()
    try:
        with Pool(processes=MAX_WORKERS) as pool:
            pending = [(path, pool.apply_async(run_file, (path, timeout))) for path in files]
            for path, async_result in pending:
                try:
                    result = async_result.get(timeout=timeout + 5.0)
                except Exception as exc:
                    result = FileResult(
                        path=path,
                        outcome=Outcome.OTHER_ERROR,
                        stderr=str(exc),
                    )
                summary.results.append(result)
                _log_result(result, verbose)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user; shutting down workers.")
        return 130
    elapsed_time = time.monotonic() - start_time
    report_summary(summary, elapsed_time)
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
