#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python files with per-file limits and reports.
``-c`` removes ordinary comments, ``-d`` the function/class docstrings, ``-a`` everything (module docstrings and annotations included) and ``--remove-blank-lines`` the empty lines.
``--dry-run``/``--diff`` preview, ``--backup`` keeps a copy, size/token/depth budgets bound the work and ``--report-json`` streams a JSON report.
Usage: rmc_termux [options] [paths ...] Positional arguments: inputs ...
Options: -c Remove ordinary comments -d Remove function/class docstrings -a, --all Remove all comments/docstrings, including normally preserved ones --remove-blank-lines --dryrun, --dry-run --backup --diff -t, --time --color --exclude ...
plus 12 more; run ``rmc_termux --help`` for the full list.
Examples: $ rmc_termux -c -d src/ --backup $ rmc_termux --all --dry-run src/ Command-line script: run ``python rmc_termux.py`` or the ``rmc_termux`` command.
The module is importable as well."""

import argparse
import ast
from dataclasses import asdict, dataclass
import difflib
import fnmatch
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
from time import perf_counter
import tokenize
import traceback
from typing import Any, Iterable, Iterator, Sequence

from atomicio import atomic_write
from codeutils import ASTStripper, CSTStripper, TSStripper
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger

LOGGER = logger
_COOKIE_RE = re.compile(rb"coding[:=]\s*[-\w.]+")


@dataclass(slots=True)
class StripFileResult:
    path: Path
    saved: int = 0
    modified: bool = False
    error: str | None = None
    elapsed: float = 0.0
    diff: str = ""
    backup: Path | None = None
    traceback: str = ""
    skipped: str | None = None


def make_diff(before: str, after: str, name: str) -> str:
    output: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    ):
        output.append(line)
        if not line.endswith("\n"):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def create_backup(path: Path, original: bytes) -> Path:
    backup = path.with_name(path.name + ".bak")
    with backup.open("xb") as stream:
        try:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
            backup.chmod(stat.S_IMODE(path.stat().st_mode))
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    return backup


def remove_blank_lines(source: str) -> str:
    protected: set[int] = set()
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.start[0] != token.end[0]:
            protected.update(range(token.start[0], token.end[0] + 1))
    lines = io.StringIO(source).readlines()
    for row, line in enumerate(lines, 1):
        if line.rstrip("\r\n").endswith("\\"):
            protected.add(row + 1)
        if row <= 2 and _COOKIE_RE.search(line.encode("utf-8")):
            protected.update(range(1, row + 1))
    result = "".join(line for row, line in enumerate(lines, 1) if row in protected or line.strip(" \t\f\r\n"))
    before = ast.dump(ast.parse(source), include_attributes=False)
    after = ast.dump(ast.parse(result), include_attributes=False)
    if before != after:
        msg = "Blank-line removal would change the AST; not written"
        raise ValueError(msg)
    return result


def _process_file(
    path: Path,
    stripper: str,
    *,
    dryrun: bool = False,
    backup: bool = False,
    show_diff: bool = False,
    limits: Limits | None = None,
    remove_blank_lines_flag: bool = False,
    force_utf8: bool = False,
) -> StripFileResult:
    started = perf_counter()
    limits = limits or Limits()
    result = StripFileResult(path)
    try:
        if path.is_symlink():
            msg = "Refusing to replace a symbolic link"
            raise ValueError(msg)
        if path.stat().st_size > limits.max_bytes:
            msg = f"file exceeds {limits.max_bytes:,} bytes"
            raise BudgetExceeded(msg)
        original = read_bounded(path, limits.max_bytes)
        encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
        source = original.decode(encoding)
        check_budget(source, limits)
        stripped = stripper.strip(source)
        if remove_blank_lines_flag:
            stripped = remove_blank_lines(stripped)
        if stripped != source:
            output_encoding = "utf-8" if force_utf8 else encoding
            data = stripped.encode(output_encoding)
            actual_encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            if data.decode(actual_encoding) != stripped:
                msg = "Output encoding declaration does not match serialized source"
                raise ValueError(msg)
            result.saved = len(original) - len(data)
            result.modified = True
            if show_diff:
                result.diff = make_diff(source, stripped, path.name)
            if not dryrun:
                if backup:
                    if path.is_symlink() or read_bounded(path, len(original)) != original:
                        msg = "File changed while processing; not replaced"
                        raise RuntimeError(msg)
                    result.backup = create_backup(path, original)
                atomic_write(path, data)
    except BudgetExceeded as exc:
        result.skipped = str(exc)
        result.saved = 0
        result.modified = False
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.traceback = traceback.format_exc()
        result.saved = 0
        result.modified = False
    result.elapsed = perf_counter() - started
    return result


def create_stripper(config: dict):
    cfg = dict(config)
    cls_name = cfg.pop("class")
    if cls_name == "ASTStripper":
        cls = ASTStripper
    elif cls_name == "TSStripper":
        cls = TSStripper
    elif cls_name == "CSTStripper":
        cls = CSTStripper
    else:
        msg = f"Unknown stripper class: {cls_name}"
        raise ValueError(msg)
    return cls(**cfg)


def process_file_with_config(
    path: Path,
    config: dict,
    *,
    dryrun: bool = False,
    backup: bool = False,
    show_diff: bool = False,
    limits: Limits | None = None,
    remove_blank_lines_flag: bool = False,
    force_utf8: bool = False,
) -> StripFileResult:
    stripper = create_stripper(config)
    return _process_file(
        path,
        stripper,
        dryrun=dryrun,
        backup=backup,
        show_diff=show_diff,
        limits=limits,
        remove_blank_lines_flag=remove_blank_lines_flag,
        force_utf8=force_utf8,
    )


def colorize_diff(diff: str, color: bool) -> str:
    if not color:
        return diff
    output: list[str] = []
    for line in diff.splitlines(keepends=True):
        code = (
            "1"
            if line.startswith(("---", "+++"))
            else (
                "32"
                if line.startswith("+")
                else "31"
                if line.startswith("-")
                else "36"
                if line.startswith("@@")
                else ""
            )
        )
        output.append(f"\033[{code}m{line}\033[0m" if code else line)
    return "".join(output)


@dataclass(frozen=True, slots=True)
class Limits:
    max_bytes: int = 1_048_576
    max_lines: int = 20_000
    max_tokens: int = 50_000
    max_depth: int = 64


class BudgetExceeded(ValueError):
    pass


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        msg = f"file exceeds {limit:,} bytes or grew while processing"
        raise BudgetExceeded(msg)
    return data


def check_budget(source: str, limits: Limits) -> None:
    if source.count("\n") + 1 > limits.max_lines:
        msg = f"file exceeds {limits.max_lines:,} lines"
        raise BudgetExceeded(msg)
    for count, _ in enumerate(tokenize.generate_tokens(io.StringIO(source).readline), 1):
        if count > limits.max_tokens:
            msg = f"file exceeds {limits.max_tokens:,} tokens"
            raise BudgetExceeded(msg)


def is_python_file(path: Path) -> bool:
    if path.suffix.lower() == ".py":
        return True
    if path.suffix:
        return False
    with path.open("rb") as stream:
        first = stream.readline(4096).decode("utf-8", errors="replace")
    if not first.startswith("#!"):
        return False
    try:
        words = shlex.split(first[2:])
        if not words:
            return False
        program = Path(words[0]).name
        if program == "env":
            expanded = [part for word in words[1:] for part in shlex.split(word)]
            program = next(
                (word for word in expanded if not word.startswith("-") and "=" not in word),
                "",
            )
        return re.fullmatch(r"(?:python|pypy)(?:[23](?:\.\d+)*)?", Path(program).name) is not None
    except ValueError:
        return False


def has_symlink_component(path: Path) -> bool:
    return path.is_symlink() or any(parent.is_symlink() for parent in path.parents)


def excluded(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(
        fnmatch.fnmatchcase(path.name, pattern) or fnmatch.fnmatchcase(relative, pattern) for pattern in patterns
    )


def normalize_inputs(inputs: list[Path]) -> list[Path]:
    paths = sorted(
        {Path(Path(path.expanduser()).absolute()) for path in inputs},
        key=lambda path: (len(path.parts), str(path)),
    )
    roots: list[Path] = []
    for path in paths:
        if has_symlink_component(path):
            LOGGER.warning("Skipping symlink input: {}", path)
            continue
        if not path.exists():
            msg = f"Input does not exist: {path}"
            raise FileNotFoundError(msg)
        if not path.is_dir() and not path.is_file():
            msg = f"Not a regular file/directory: {path}"
            raise ValueError(msg)
        if any(root.is_dir() and path.is_relative_to(root) for root in roots):
            continue
        roots.append(path)
    return roots


@dataclass(slots=True)
class Stats:
    files: int = 0
    changed: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_reduced: int = 0


def walk_python_files(
    roots: list[Path],
    *,
    limits: Limits,
    skip_dirs: frozenset[str],
    patterns: tuple[str, ...],
    outputs: set[Path],
    stats: Stats,
) -> Iterator[Path]:
    def visit(directory: Path, root: Path, depth: int) -> Iterator[Path]:
        if depth > limits.max_depth:
            stats.skipped += 1
            LOGGER.warning("Depth limit reached: {}", directory)
            return
        try:
            if directory.is_symlink():
                return
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if path in outputs or excluded(path, root, patterns):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if path.name not in skip_dirs and not path.name.endswith(".egg-info"):
                                yield from visit(path, root, depth + 1)
                        elif entry.is_file(follow_symlinks=False) and is_python_file(path):
                            yield path
                    except OSError:
                        stats.errors += 1
                        LOGGER.exception("Cannot inspect {}", path)
        except OSError:
            stats.errors += 1
            LOGGER.exception("Cannot traverse {}", directory)

    for root in roots:
        if root.is_dir():
            if root.name not in skip_dirs and not root.name.endswith(".egg-info"):
                yield from visit(root, root, 0)
        elif root not in outputs and root.parent.name not in skip_dirs:
            try:
                if not excluded(root, root.parent, patterns) and is_python_file(root):
                    yield root
            except OSError:
                stats.errors += 1
                LOGGER.exception("Cannot inspect {}", root)


class StreamingReport:
    def __init__(self, path: Path) -> None:
        self.path = path
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        self.temporary = Path(name)
        self.stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        self.first = True
        self.stream.write('{"version":1,"files":[')

    def add(self, result: StripFileResult) -> None:
        data = asdict(result)
        data["path"] = str(result.path)
        data["backup"] = str(result.backup) if result.backup else None
        data.pop("traceback", None)
        data.pop("diff", None)
        if not self.first:
            self.stream.write(",")
        json.dump(data, self.stream, ensure_ascii=False)
        self.first = False

    def finish(self, stats: Stats, dryrun: bool, elapsed: float) -> None:
        self.stream.write('],"summary":')
        json.dump({**asdict(stats), "dryrun": dryrun, "elapsed_seconds": elapsed}, self.stream)
        self.stream.write("}\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.temporary.replace(self.path)

    def close(self) -> None:
        self.stream.close()
        self.temporary.unlink(missing_ok=True)


def configure_logging(error_log: Path, log_file: Path | None, level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level, format="{level} | {message}")
    logger.add(error_log, level="ERROR", rotation="1 MB", retention=2, encoding="utf-8")
    if log_file is not None:
        logger.add(log_file, level=level, rotation="2 MB", retention=2, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", type=Path, metavar="PATH")
    parser.add_argument("-c", action="store_true", help="Remove ordinary comments")
    parser.add_argument("-d", action="store_true", help="Remove function/class docstrings")
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Remove all comments/docstrings, including normally preserved ones",
    )
    parser.add_argument("--remove-blank-lines", action="store_true")
    parser.add_argument("--dryrun", "--dry-run", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("-t", "--time", action="store_true")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--exclude-dir", action="append", default=[], metavar="NAME")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=1_048_576,
        help="Per-file input cap (default: 1048576)",
    )
    parser.add_argument("--max-lines", type=int, default=20_000)
    parser.add_argument("--max-tokens", type=int, default=50_000)
    parser.add_argument(
        "--max-depth",
        type=int,
        default=64,
        help="Maximum traversal depth (default: 64)",
    )
    parser.add_argument("--report-json", type=Path, help="Stream a JSON report to disk atomically")
    parser.add_argument("--error-log", type=Path, default=Path("striper_termux.errors.log"))
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="WARNING")
    parser.add_argument(
        "--stripper", choices=("ast", "ts", "cst"), default="cst", help="Stripper backend (default: cst)"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mp", action="store_true", help="Use multiprocessing.Pool.imap_unordered")
    group.add_argument("--cf", action="store_true", help="Use concurrent.futures.ProcessPoolExecutor")
    args = parser.parse_intermixed_args()
    if any(value < 1 for value in (args.max_bytes, args.max_lines, args.max_tokens, args.max_depth, MAX_WORKERS)):
        parser.error("All resource limits and workers must be positive; zero/unlimited is not supported")
    if any(not name or "/" in name or "\\" in name for name in args.exclude_dir):
        parser.error("--exclude-dir requires a directory basename")
    if not (args.c or args.d or args.all or args.remove_blank_lines):
        args.c = args.d = True
    configure_logging(args.error_log, args.log_file, args.log_level)
    started = perf_counter()
    stats = Stats()
    report: StreamingReport | None = None
    interrupted = False
    try:
        roots = normalize_inputs(args.inputs or [Path.cwd()])
        limits = Limits(args.max_bytes, args.max_lines, args.max_tokens, args.max_depth)
        if args.report_json is not None:
            report = StreamingReport(args.report_json)
        outputs = {
            Path(Path(path).absolute())
            for path in (
                args.error_log,
                args.log_file,
                args.report_json,
                report.temporary if report else None,
            )
            if path is not None
        }
        skip_dirs = SKIP_DIRS | frozenset(args.exclude_dir)
        color = args.color == "always" or (
            args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ
        )
        if args.all:
            LOGGER.warning("--all removes shebangs, encoding comments, tool directives and module docstrings")
        if args.stripper == "ast":
            cls_name = "ASTStripper"
        elif args.stripper == "ts":
            cls_name = "TSStripper"
        else:
            cls_name = "CSTStripper"
        stripper_config = {
            "class": cls_name,
            "strip_comments": args.c or args.all,
            "strip_docstrings": args.d or args.all,
            "preserve_shebangs": not args.all,
            "preserve_module_docstring": not args.all,
            "preserve_directives": not args.all,
            "validate": True,
            "preserve_commented_code": True,
        }
        force_utf8 = args.all
        remove_blank_lines_flag = args.remove_blank_lines
        paths = list(
            walk_python_files(
                roots,
                limits=limits,
                skip_dirs=skip_dirs,
                patterns=tuple(args.exclude),
                outputs=outputs,
                stats=stats,
            )
        )
        total = len(paths)
        term_w = shutil.get_terminal_size((80, 24)).columns
        num_w = len(str(total)) if total > 0 else 1
        prefix_w = 2 * num_w + 3
        longest_name = max((len(p.name) for p in paths), default=20)
        bytes_w = min(max(len(f"-{limits.max_bytes:,}"), 6), 20)
        avail_for_name = term_w - prefix_w - 4 - bytes_w
        name_w = min(longest_name, max(10, avail_for_name))

        def format_line(idx: int, name: str, value: str) -> str:
            idx_part = f"[{idx:>{num_w}}/{total}]"
            if len(name) > name_w:
                name = ("..." + name[-(name_w - 3) :]) if name_w > 3 else name[:name_w]
            if isinstance(value, int):
                val_str = f"{value:,}"
                if color and value > 0:
                    pad = " " * max(0, bytes_w - len(val_str))
                    val_display = f"{pad}\033[32m{val_str}\033[0m"
                else:
                    val_display = f"{val_str:>{bytes_w}}"
            else:
                val_display = f"{value:>{bytes_w}}"
            return f"{idx_part}  {name:<{name_w}}  {val_display}"

        def handle_result(result: StripFileResult, idx: int) -> None:
            stats.files += 1
            if result.skipped:
                stats.skipped += 1
                LOGGER.warning("{}: skipped: {}", result.path.name, result.skipped)
                line = format_line(idx, result.path.name, "SKIP")
            elif result.error:
                stats.errors += 1
                LOGGER.error("{}: {}\n{}", result.path, result.error, result.traceback)
                line = format_line(idx, result.path.name, "ERR")
            else:
                stats.changed += result.modified
                stats.bytes_reduced += result.saved
                if result.diff:
                    print(colorize_diff(result.diff, color), end="")
                line = format_line(idx, result.path.name, result.saved)
                LOGGER.debug(
                    "{}: modified={}, saved={}, seconds={}",
                    result.path,
                    result.modified,
                    result.saved,
                    result.elapsed,
                )
            if args.time:
                line += f"  {result.elapsed:.4f}s"
            print(line)
            if report is not None:
                report.add(result)

        if args.mp:
            from functools import partial
            import multiprocessing as mp

            worker = partial(
                process_file_with_config,
                config=stripper_config,
                dryrun=args.dryrun,
                backup=args.backup,
                show_diff=args.diff,
                limits=limits,
                remove_blank_lines_flag=remove_blank_lines_flag,
                force_utf8=force_utf8,
            )
            with mp.Pool(MAX_WORKERS) as pool:
                for idx, result in enumerate(pool.imap_unordered(worker, paths), 1):
                    handle_result(result, idx)
        elif args.cf:
            import concurrent.futures as cf
            from functools import partial

            worker = partial(
                process_file_with_config,
                config=stripper_config,
                dryrun=args.dryrun,
                backup=args.backup,
                show_diff=args.diff,
                limits=limits,
                remove_blank_lines_flag=remove_blank_lines_flag,
                force_utf8=force_utf8,
            )
            with cf.ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(worker, path) for path in paths]
                for idx, future in enumerate(cf.as_completed(futures), 1):
                    result = future.result()
                    handle_result(result, idx)
        else:
            stripper = create_stripper(stripper_config)
            for idx, path in enumerate(paths, 1):
                result = _process_file(
                    path,
                    stripper,
                    dryrun=args.dryrun,
                    backup=args.backup,
                    show_diff=args.diff,
                    limits=limits,
                    remove_blank_lines_flag=remove_blank_lines_flag,
                    force_utf8=force_utf8,
                )
                handle_result(result, idx)
    except KeyboardInterrupt:
        interrupted = True
        stats.errors += 1
        LOGGER.error("Interrupted; previously committed source changes are not rolled back")
    except Exception:
        stats.errors += 1
        LOGGER.exception("Termux run failed")
    finally:
        if report is not None:
            try:
                report.finish(stats, args.dryrun, perf_counter() - started)
            except Exception:
                stats.errors += 1
                LOGGER.exception("Cannot finalize JSON report")
            finally:
                report.close()
    label = (
        "Potential source bytes reduced"
        if args.dryrun
        else "Total source bytes reduced"
        if args.backup
        else "Total space freed"
    )
    print(f"\n{label}: {stats.bytes_reduced:,} bytes")
    print(f"Files: {stats.files}; changed: {stats.changed}; skipped: {stats.skipped}; errors: {stats.errors}")
    if args.dryrun:
        print("Dry run: no source files or backups written; logs/reports may be written.")
    if args.backup and not args.dryrun:
        print("Backups consume extra space; totals count source files only.")
    if args.time:
        print(f"Total elapsed: {perf_counter() - started:.4f}s")
    return 130 if interrupted else 1 if stats.errors else 3 if stats.skipped else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        LOGGER.exception("Fatal error")
        raise SystemExit(1) from None
