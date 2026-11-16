#!/data/data/com.termux/files/usr/bin/env python
"""CLI tool to generate a new Typer + Loguru Python script."""

import argparse
from datetime import datetime
from pathlib import Path
import stat
import sys

TEMPLATE_CONTENT = '''#!/data/data/com.termux/files/usr/bin/env python

"""
{description}

date: {date}
"""

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import sys
from typing import Annotated, TypeVar

from loguru import logger
import typer

T = TypeVar("T")

app = typer.Typer(
    help="{description}",
    add_completion=False,
    no_args_is_help=True,
)


def setup_logging(verbose: bool = False) -> None:
    """Configure Loguru console logging."""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
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
) -> list[T]:
    """Execute a callable across an iterable of files in parallel."""
    workers = max_workers or os.cpu_count() or 4
    executor_cls = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
    results: list[T] = []

    with executor_cls(max_workers=workers) as executor:
        future_to_file = {{executor.submit(func, file_path): file_path for file_path in files}}
        for future in as_completed(future_to_file):
            file_path = future_to_file[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.exception(f"Failed processing {{file_path}}: {{exc}}")

    return results


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
        typer.Option("--workers", "-w", help="Number of parallel workers."),
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
    processed = parallel_runner(
        process_file,
        files,
        max_workers=workers,
        use_threads=threads,
    )

    logger.success(f"Completed processing {{len(processed)}} file(s).")


if __name__ == "__main__":
    app()
'''


def create_script(
    filepath: Path, description: str, force: bool = False
) -> None:
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
    filepath.chmod(
        current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    print(f"Created Typer + Loguru template script: {filepath}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a new Typer + Loguru Python script from a template."
    )
    parser.add_argument(
        "filename", type=Path, help="Name or path of the new script"
    )
    parser.add_argument(
        "-d",
        "--description",
        default="A parallel file-processing CLI built with Typer and Loguru.",
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
