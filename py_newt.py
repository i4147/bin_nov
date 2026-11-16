#!/data/data/com.termux/files/usr/bin/env python
"""CLI tool to generate a new Typer + Loguru + Rich Python script."""

import argparse
from datetime import datetime
from pathlib import Path
import stat
import sys

TEMPLATE_CONTENT = '''#!/usr/bin/env python3
"""
{description}

Created on: {date}
"""

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from itertools import islice
import os
from pathlib import Path
from typing import Annotated, TypeVar

from loguru import logger
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
import typer

T = TypeVar("T")

console = Console(stderr=True)

app = typer.Typer(
    help="{description}",
    add_completion=False,
    no_args_is_help=True,
)


def setup_logging(verbose: bool = False) -> None:
    """Configure Loguru to route through Rich Console without breaking progress bars."""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        lambda msg: console.print(msg, end=""),
        level=level,
        colorize=True,
        format="<green>{{time:YYYY-MM-DD HH:mm:ss}}</green> | <level>{{level: <8}}</level> | <cyan>{{message}}</cyan>",
    )


def walk_files(
    root: Path,
    pattern: str = "*",
    recursive: bool = True,
) -> Iterator[Path]:
    """Lazily yield matching file paths under a root directory."""
    if root.is_file():
        yield root
        return

    walker = root.rglob(pattern) if recursive else root.glob(pattern)
    for path in walker:
        if path.is_file():
            yield path


def parallel_runner(
    func: Callable[[Path], T],
    files: Iterable[Path],
    max_workers: int | None = None,
    use_threads: bool = False,
    max_in_flight: int | None = None,
) -> Iterator[T]:
    """Stream results lazily via yield with bounded futures, progress bar, and Ctrl+C handling."""
    workers = max_workers or os.cpu_count() or 4
    buffer_limit = max(1, max_in_flight if max_in_flight is not None else workers * 2)
    executor_cls = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
    file_iter = iter(files)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{{task.description}}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    executor = executor_cls(max_workers=workers)
    in_flight: dict[Future[T], Path] = {{}}

    try:
        with progress:
            task_id = progress.add_task("Processing files", total=None)
            submitted = 0

            # Seed the initial bounded window of futures
            for file_path in islice(file_iter, buffer_limit):
                in_flight[executor.submit(func, file_path)] = file_path
                submitted += 1

            while in_flight:
                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)

                for future in done:
                    file_path = in_flight.pop(future)
                    progress.advance(task_id)

                    # Refill the window before yielding so workers stay busy
                    next_file = next(file_iter, None)
                    if next_file is not None:
                        in_flight[executor.submit(func, next_file)] = next_file
                        submitted += 1

                    try:
                        yield future.result()
                    except Exception as exc:
                        logger.exception(f"Failed processing {{file_path}}: {{exc}}")

                # Set exact total once the lazy file iterator is exhausted
                if len(in_flight) < buffer_limit:
                    progress.update(task_id, total=submitted)

    except KeyboardInterrupt:
        logger.warning("Interrupt received (Ctrl+C). Cancelling pending tasks...")
        for future in in_flight:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise typer.Exit(code=130)
    finally:
        executor.shutdown(wait=True)


def process_file(file_path: Path) -> Path:
    """Example worker function for a single file."""
    logger.debug(f"Processing {{file_path}}")
    # TODO: Add per-file processing logic here
    return file_path


@app.command()
def run(
    target: Annotated[
        Path,
        typer.Argument(
            exists=True,
            help="Target file or directory to process.",
        ),
    ],
    pattern: Annotated[
        str,
        typer.Option("--pattern", "-p", help="Glob pattern to filter files."),
    ] = "*",
    workers: Annotated[
        int | None,
        typer.Option("--workers", "-w", min=1, help="Number of parallel workers."),
    ] = None,
    in_flight: Annotated[
        int | None,
        typer.Option(
            "--in-flight",
            "-b",
            min=1,
            help="Max number of queued/running tasks in flight (defaults to 2x workers).",
        ),
    ] = None,
    threads: Annotated[
        bool,
        typer.Option(
            "--threads",
            help="Use ThreadPoolExecutor instead of ProcessPoolExecutor.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging."),
    ] = False,
) -> None:
    """Walk target files and process them in parallel."""
    setup_logging(verbose)
    logger.info(f"Scanning {{target}} with pattern '{{pattern}}'")

    files = walk_files(target, pattern=pattern)
    results_stream = parallel_runner(
        process_file,
        files,
        max_workers=workers,
        use_threads=threads,
        max_in_flight=in_flight,
    )

    processed_count = 0
    for result in results_stream:
        processed_count += 1
        logger.debug(f"Yielded result: {{result}}")

    logger.success(f"Completed processing {{processed_count}} file(s).")


if __name__ == "__main__":
    app()
'''


def create_script(filepath: Path, description: str, force: bool = False) -> None:
    """Write the template content to the target file."""
    if not filepath.suffix:
        filepath = filepath.with_suffix(".py")

    if filepath.exists() and not force:
        print(
            f"Error: '{filepath}' already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    safe_description = description.replace('"', '\\"')

    content = TEMPLATE_CONTENT.format(
        description=safe_description,
        date=datetime.now().strftime("%Y-%m-%d"),
    )

    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")

    current_permissions = filepath.stat().st_mode
    filepath.chmod(current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Created Typer + Loguru + Rich template script: {filepath}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a new Typer + Loguru + Rich Python script from a template.")
    parser.add_argument("filename", type=Path, help="Name or path of the new script")
    parser.add_argument(
        "-d",
        "--description",
        default="A parallel file-processing CLI built with Typer, Loguru, and Rich.",
        help="Docstring and CLI help description for the new script",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists",
    )

    args = parser.parse_args()
    create_script(args.filename, args.description, args.force)


if __name__ == "__main__":
    main()
