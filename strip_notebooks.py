#!/data/data/com.termux/files/usr/bin/env python
"""Strip outputs from Jupyter notebook (.ipynb) files."""

import argparse
import json
from multiprocessing import Pool
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from dh import MAX_WORKERS
from loguru import logger

if TYPE_CHECKING:
    from multiprocessing.pool import AsyncResult
StripResult = tuple[Path, bool, str]


def find_notebook_files(paths: list[Path]) -> set[Path]:
    notebook_files: set[Path] = set()
    for path in paths:
        if not path.exists():
            logger.warning(f"{path} does not exist, skipping.")
            continue
        if path.is_file():
            if path.suffix == ".ipynb":
                notebook_files.add(path.resolve())
            else:
                logger.warning(f"{path} is not a .ipynb file, skipping.")
        elif path.is_dir():
            for nb_file in path.rglob("*.ipynb"):
                if ".ipynb_checkpoints" not in str(nb_file):
                    notebook_files.add(nb_file.resolve())
    return notebook_files


def strip_notebook_output(notebook_path: Path) -> StripResult:
    try:
        with notebook_path.open("r", encoding="utf-8") as f:
            notebook: dict[str, object] = json.load(f)
        if "cells" not in notebook:
            return (notebook_path, False, "Not a valid notebook (no 'cells' key)")
        cells: object = notebook["cells"]
        if not isinstance(cells, list):
            return (
                notebook_path,
                False,
                "Not a valid notebook ('cells' is not a list)",
            )
        modified: bool = False
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            if cell.get("cell_type") == "code":
                if cell.get("outputs"):
                    cell["outputs"] = []
                    modified = True
                if "execution_count" in cell and cell["execution_count"] is not None:
                    cell["execution_count"] = None
                    modified = True
        if modified:
            with notebook_path.open("w", encoding="utf-8") as f:
                json.dump(notebook, f, indent=1, ensure_ascii=False)
                f.write("\n")
            return (notebook_path, True, "Outputs stripped")
        return (notebook_path, True, "No outputs to strip")
    except json.JSONDecodeError as exc:
        return (notebook_path, False, f"Invalid JSON: {exc}")
    except Exception as exc:
        return (notebook_path, False, f"Error: {exc}")


def process_notebooks(paths: list[Path]) -> None:
    notebook_files: set[Path] = find_notebook_files(paths)
    if not notebook_files:
        print("No .ipynb files found to process.")
        return
    print(f"Found {len(notebook_files)} notebook(s) to process...")
    ordered: list[Path] = sorted(notebook_files)
    results: list[StripResult] = []
    with Pool(processes=MAX_WORKERS) as pool:
        async_results: list[AsyncResult[StripResult]] = [
            pool.apply_async(strip_notebook_output, (path,)) for path in ordered
        ]
        for async_res in async_results:
            path, success, message = async_res.get()
            results.append((path, success, message))
            status: str = "✓" if success else "✗"
            relative_path: Path = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path
            print(f"{status} {relative_path}: {message}")
    successful: int = sum(1 for _, success, _ in results if success)
    failed: int = len(results) - successful
    if failed > 0:
        logger.warning(f"Processed: {successful} succeeded, {failed} failed")
    else:
        print(f"Successfully processed {successful} notebook(s)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strip outputs from Jupyter notebook (.ipynb) files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s notebook.ipynb
  %(prog)s dir1/ dir2/
  %(prog)s *.ipynb
  %(prog)s .
        """,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: current directory)",
    )
    args: argparse.Namespace = parser.parse_args()
    paths: list[Path] = [Path(p) for p in args.paths]
    try:
        process_notebooks(paths)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
