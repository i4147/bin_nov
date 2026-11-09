#!/data/data/com.termux/files/usr/bin/env python
"""Extract shell function definitions from scripts into individual files."""


import argparse
from collections.abc import Iterable, Iterator
import contextlib
from multiprocessing import Pool
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Final, Protocol

from dh import MAX_WORKERS
from loguru import logger


EXCLUDED_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".py",
    ".h",
    ".c",
    ".js",
    ".ts",
    ".hpp",
    ".cpp",
    ".pyx",
    ".jsx",
    ".lua",
    ".tsx",
    ".pl",
    ".am",
    ".pm",
    ".syntax",
    ".so",
    ".rmeta",
})
SHELL_SHEBANG_TOKENS: Final[tuple[str, ...]] = (
    "bash",
    "sh",
    "dash",
    "ksh",
    "zsh",
    "ash",
    "shell",
)
FUNCTION_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:function\s+)?(\w[\w\-]*)\s*(?:\(\))?\s*\{")
UNSAFE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\-]")
DEFAULT_MAX_FILE_SIZE: Final[int] = 1_000_000
DEFAULT_OUTPUT_DIR: Final[Path] = Path("extracted_functions")
IS_TERMUX: Final[bool] = "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", "")


class _LoggerProtocol(Protocol):
    def debug(self, msg: str, *args: object) -> None: ...
    def info(self, msg: str, *args: object) -> None: ...
    def warning(self, msg: str, *args: object) -> None: ...
    def error(self, msg: str, *args: object) -> None: ...


class _PrintLogger:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def _emit(self, level: str, msg: str, args: tuple[object, ...]) -> None:
        text = msg.format(*args) if args else msg
        print(f"[{level}] {text}", file=sys.stderr)

    def debug(self, msg: str, *args: object) -> None:
        if self.verbose:
            self._emit("debug", msg, args)

    def info(self, msg: str, *args: object) -> None:
        self._emit("info", msg, args)

    def warning(self, msg: str, *args: object) -> None:
        self._emit("warn", msg, args)

    def error(self, msg: str, *args: object) -> None:
        self._emit("error", msg, args)


def make_logger(use_loguru: bool, verbose: bool) -> _LoggerProtocol:
    if use_loguru:
        try:
            from loguru import logger
        except ImportError:
            print(
                "loguru is not installed; falling back to stdlib logger",
                file=sys.stderr,
            )
        else:
            logger.remove()
            logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
            return logger
    return _PrintLogger(verbose)


def is_shell_script(
    path: Path,
    *,
    include_extensionless: bool = True,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
) -> bool:
    if not path.is_file():
        return False
    if path.suffix == ".sh":
        return True
    if not include_extensionless:
        return False
    try:
        if path.stat().st_size > max_size:
            return False
    except OSError:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(2)
            if b"\x00" in head:
                return False
            fh.seek(0)
            first_line = fh.readline().decode("utf-8", errors="ignore").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not first_line.startswith("#!"):
        return False
    lowered = first_line.lower()
    return any(tok in lowered for tok in SHELL_SHEBANG_TOKENS)


def _iter_directory_scandir(
    directory: Path,
    *,
    include_extensionless: bool,
    skip_hidden: bool,
    max_size: int,
    log: _LoggerProtocol,
) -> Iterator[Path]:
    try:
        with os.scandir(directory) as it:
            for entry in it:
                entry_path = Path(entry.path)
                if skip_hidden and entry_path.name.startswith("."):
                    continue
                try:
                    if entry.is_file():
                        if entry_path.suffix in EXCLUDED_SUFFIXES:
                            continue
                        if is_shell_script(
                            entry_path,
                            include_extensionless=include_extensionless,
                            max_size=max_size,
                        ):
                            yield entry_path.resolve()
                    elif entry.is_dir():
                        yield from _iter_directory_scandir(
                            entry_path,
                            include_extensionless=include_extensionless,
                            skip_hidden=skip_hidden,
                            max_size=max_size,
                            log=log,
                        )
                except OSError:
                    continue
    except PermissionError:
        log.warning("Permission denied accessing {}", directory)
    except OSError as exc:
        log.warning("OS error accessing {}: {}", directory, exc)


def collect_scripts(
    inputs: Iterable[Path],
    *,
    include_extensionless: bool,
    skip_hidden: bool,
    max_size: int,
    log: _LoggerProtocol,
) -> Iterator[Path]:
    seen: set[Path] = set()
    for p in inputs:
        if not p.exists():
            log.warning("{} does not exist, skipping...", p)
            continue
        if p.is_file():
            if is_shell_script(p, include_extensionless=include_extensionless, max_size=max_size):
                r = p.resolve()
                if r not in seen:
                    seen.add(r)
                    yield r
        elif p.is_dir():
            for s in _iter_directory_scandir(
                p,
                include_extensionless=include_extensionless,
                skip_hidden=skip_hidden,
                max_size=max_size,
                log=log,
            ):
                if s not in seen:
                    seen.add(s)
                    yield s
        else:
            log.warning("{} is not a file or directory, skipping...", p)


def extract_functions(path: Path, log: _LoggerProtocol) -> Iterator[tuple[str, str]]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError as exc:
        log.error("Error reading {}: {}", path, exc)
        return
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FUNCTION_RE.match(line)
        if not m:
            i += 1
            continue
        name = m.group(1)
        block = [line]
        depth = line.count("{") - line.count("}")
        j = i + 1
        while j < len(lines) and depth > 0:
            nxt = lines[j]
            block.append(nxt)
            depth += nxt.count("{") - nxt.count("}")
            j += 1
        if depth == 0:
            yield name, "\n".join(block)
        else:
            log.warning(
                "Could not find matching closing brace for function '{}' in {}",
                name,
                path,
            )
        i = j


def write_function(
    name: str,
    body: str,
    source_path: Path,
    output_dir: Path,
    *,
    use_extension: bool,
    add_header: bool,
    log: _LoggerProtocol,
) -> Path | None:
    safe_name = UNSAFE_NAME_RE.sub("_", name)
    try:
        rel = source_path.relative_to(Path.cwd())
    except ValueError:
        rel = source_path
    target_dir = output_dir / rel.parent
    filename = f"{safe_name}.sh" if use_extension else safe_name
    target = target_dir / filename
    target_dir.mkdir(parents=True, exist_ok=True)
    header = ""
    if add_header:
        header = (
            "#!/bin/bash\n"
            f"# Function: {name}\n"
            f"# Extracted from: {source_path}\n"
            f"# Original file: {source_path.name}\n"
            f"# Environment: {'Termux' if IS_TERMUX else 'Standard'}\n\n"
        )
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(header)
            fh.write(body)
            fh.write("\n")
    except OSError as exc:
        log.error("Error writing function '{}' to {}: {}", name, target, exc)
        return None
    return target


def _process_one(
    task: tuple[Path, Path, bool, bool, bool, bool],
) -> tuple[Path, list[Path]]:
    path, output_dir, use_extension, add_header, chmod_files, verbose = task
    log = _PrintLogger(verbose)
    written: list[Path] = []
    for name, body in extract_functions(path, log):
        target = write_function(
            name,
            body,
            path,
            output_dir,
            use_extension=use_extension,
            add_header=add_header,
            log=log,
        )
        if target is None:
            continue
        if chmod_files:
            with contextlib.suppress(OSError):
                target.chmod(target.stat().st_mode | 0o111)
        written.append(target)
    return path, written


def _process_scripts(scripts: list[Path], args: argparse.Namespace, log: _LoggerProtocol) -> int:
    use_extension = not args.no_extension
    add_header = not args.no_header
    chmod_files = not args.no_chmod
    total = 0
    if args.parallel and len(scripts) > 1:
        print(f"Processing files in parallel with {MAX_WORKERS} workers...")
        tasks = [(p, args.output, use_extension, add_header, chmod_files, args.verbose) for p in scripts]
        with Pool(processes=MAX_WORKERS) as pool:
            for path, written in pool.imap_unordered(_process_one, tasks):
                total += len(written)
                if args.verbose or written:
                    print(f"  {path}: extracted {len(written)} function(s)")
    else:
        print("Processing files sequentially...")
        for p in sorted(scripts):
            _, written = _process_one((p, args.output, use_extension, add_header, chmod_files, args.verbose))
            total += len(written)
            if args.verbose or written:
                print(f"  {p}: extracted {len(written)} function(s)")
    return total


def run(args: argparse.Namespace) -> int:
    log = make_logger(args.use_loguru, args.verbose)
    if IS_TERMUX:
        print(f"Running in Termux environment (workers={MAX_WORKERS})")
    inputs: list[Path] = args.inputs or [Path()]
    include_extensionless = not args.sh_only
    print("Searching for shell scripts...")
    scripts = list(
        collect_scripts(
            inputs,
            include_extensionless=include_extensionless,
            skip_hidden=args.skip_hidden,
            max_size=args.max_size,
            log=log,
        )
    )
    if not scripts:
        print("No shell scripts found to process.")
        if not args.sh_only:
            print("Tip: Use --sh-only to only process .sh files")
        return 0
    print(f"Found {len(scripts)} shell script(s) to process.")
    if args.verbose:
        for s in sorted(scripts):
            log.debug("  - {}", s)
    if args.dry_run:
        print(f"\nDry run — would extract to: {args.output.absolute()}")
        return 0
    try:
        args.output.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        log.error("Cannot create output directory '{}'. Check permissions.", args.output)
        return 1
    total = _process_scripts(scripts, args, log)
    print(f"\nDone! Extracted {total} function(s) to '{args.output.absolute()}'")
    if IS_TERMUX:
        with contextlib.suppress(BaseException):
            args.output.chmod(args.output.stat().st_mode | 0o755)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xb_extract",
        description=(
            "Extract shell function definitions from .sh files and extensionless "
            "shell scripts into individual files. Merged behavior of "
            "xbash_functions.py and xbash_functions2.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original script equivalents:\n"
            "  xbash_functions.py   ->  python xb_extract.py "
            "--parallel --no-header --no-chmod\n"
            "  xbash_functions2.py  ->  python xb_extract.py\n\n"
            "Examples:\n"
            "  # Process all shell scripts in the current directory (defaults)\n"
            "  %(prog)s\n\n"
            "  # Specific files/directories with a custom output dir\n"
            "  %(prog)s script1.sh myscript dir1/ dir2/ -o out_functions\n\n"
            "  # Only .sh files, skip hidden, dry run\n"
            "  %(prog)s --sh-only --skip-hidden --dry-run\n\n"
            "  # Parallel extraction (script1 behavior)\n"
            "  %(prog)s --parallel --no-header --no-chmod\n"
        ),
    )
    p.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Files and/or directories to process. If none provided, the current directory is used recursively.",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for extracted functions (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--sh-only",
        action="store_true",
        help="Only process files with a .sh extension (ignore extensionless scripts).",
    )
    p.add_argument(
        "--no-extension",
        action="store_true",
        help="Write extracted functions without a .sh extension.",
    )
    p.add_argument(
        "--no-header",
        action="store_true",
        help="Do NOT prepend the '#!/bin/bash' + metadata header (xbash_functions.py behavior).",
    )
    p.add_argument(
        "--no-chmod",
        action="store_true",
        help="Do NOT make output files executable (xbash_functions.py does not chmod files).",
    )
    p.add_argument(
        "--skip-hidden",
        action="store_true",
        help="Skip hidden files and directories (names starting with '.').",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List scripts that would be processed and exit.",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="Use multiprocessing.Pool for file processing (xbash_functions.py behavior). Default is sequential.",
    )
    p.add_argument(
        "--max-size",
        type=int,
        default=DEFAULT_MAX_FILE_SIZE,
        help=f"Maximum size (bytes) for extensionless-script detection (default: {DEFAULT_MAX_FILE_SIZE}).",
    )
    p.add_argument(
        "--use-loguru",
        action="store_true",
        help="Use loguru for logging if installed (xbash_functions.py behavior). "
        "Falls back to stdlib logging otherwise.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output (debug level, per-file details).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting...", file=sys.stderr)
        sys.exit(1)
