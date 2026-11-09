#!/data/data/com.termux/files/usr/bin/env python
"""Split PDFs into one file per page or extract the text page by page.
Processes the PDFs given on the command line (or found below the current directory) with pypdf in a worker pool.
``--text`` writes ``.txt`` files per page instead of single-page PDFs, and ``--output-dir`` selects where the results go (``./output`` by default).
Usage: perpage [options] [pdfs ...] Positional arguments: paths ...
PDF files or directories to scan; defaults to CWD.
Options: -o, --output-dir Directory to write outputs into (default: ./output).
-t, --text Extract per-page text to .txt files instead of splitting PDF pages.
--pool-method Multiprocessing pool method to use for processing.
Examples: $ perpage book.pdf -o pages/ $ perpage book.pdf --text Command-line script: run ``python perpage.py`` or the ``perpage`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from multiprocessing.pool import AsyncResult, Pool
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeAlias

from dh import MAX_WORKERS
from loguru import logger
from pypdf import PdfReader, PdfWriter


POOL_METHODS: Final[tuple[str, ...]] = (
    "map",
    "starmap",
    "imap_unordered",
    "apply_async",
)
DEFAULT_OUTPUT_DIR: Final[str] = "output"
SplitTask: TypeAlias = tuple[Path, Path, bool]
SplitResult: TypeAlias = tuple[Path, int]


def split_pdf_by_page(pdf_path: Path, output_dir: Path) -> int:
    reader = PdfReader(pdf_path)
    stem = pdf_path.stem
    total_pages = len(reader.pages)
    padding = len(str(total_pages))
    for page_num, page in enumerate(reader.pages, 1):
        writer = PdfWriter()
        writer.add_page(page)
        padded_num = str(page_num).zfill(padding)
        output_path = output_dir / f"{stem}_{padded_num}.pdf"
        with output_path.open("wb") as f:
            writer.write(f)
    return total_pages


def extract_text_by_page(pdf_path: Path, output_dir: Path) -> int:
    reader = PdfReader(pdf_path)
    stem = pdf_path.stem
    total_pages = len(reader.pages)
    padding = len(str(total_pages))
    for page_num, page in enumerate(reader.pages, 1):
        padded_num = str(page_num).zfill(padding)
        output_path = output_dir / f"{stem}_{padded_num}.txt"
        text = page.extract_text() or ""
        output_path.write_text(text, encoding="utf-8")
    return total_pages


def _process_pdf(task: SplitTask) -> SplitResult:
    pdf_path, output_dir, text_mode = task
    try:
        if text_mode:
            count = extract_text_by_page(pdf_path, output_dir)
        else:
            count = split_pdf_by_page(pdf_path, output_dir)
        return (pdf_path, count)
    except Exception as exc:
        logger.error(f"Error processing {pdf_path}: {exc}")
        return (pdf_path, 0)


def _process_pdf_tuple(item: tuple[SplitTask]) -> SplitResult:
    return _process_pdf(item[0])


def _run_pool(tasks: Sequence[SplitTask], method: str) -> list[SplitResult]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "map":
            return pool.map(_process_pdf_tuple, [(task,) for task in tasks])
        if method == "starmap":
            return pool.starmap(_process_pdf, [(task,) for task in tasks])
        if method == "imap_unordered":
            return list(pool.imap_unordered(_process_pdf_tuple, [(task,) for task in tasks]))
        if method == "apply_async":
            async_results: list[AsyncResult[SplitResult]] = [pool.apply_async(_process_pdf, (task,)) for task in tasks]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def collect_pdfs(input_paths: Sequence[str]) -> list[Path]:
    if not input_paths:
        return [p for p in Path.cwd().rglob("*.pdf") if p.is_file()]
    pdf_files: list[Path] = []
    for raw in input_paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() == ".pdf":
            pdf_files.append(p)
        elif p.is_dir():
            pdf_files.extend(x for x in p.rglob("*.pdf") if x.is_file())
    return pdf_files


def process_pdfs(
    input_paths: Sequence[str] | None,
    output_dir: Path | None,
    pool_method: str,
    text_mode: bool,
) -> int:
    if output_dir is None:
        output_dir = Path.cwd() / DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_files = collect_pdfs(list(input_paths) if input_paths else [])
    if not pdf_files:
        logger.warning("No PDF files found.")
        return 0
    tasks: list[SplitTask] = [(pdf, output_dir, text_mode) for pdf in pdf_files]
    results = _run_pool(tasks, pool_method)
    for pdf_path, page_count in results:
        if page_count:
            logger.info(f"{pdf_path}: processed {page_count} pages")
    logger.info(f"Processing complete. Output files in: {output_dir}")
    return len(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="PDF files or directories to scan; defaults to CWD.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory to write outputs into (default: ./output).",
    )
    parser.add_argument(
        "-t",
        "--text",
        action="store_true",
        help="Extract per-page text to .txt files instead of splitting PDF pages.",
    )
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="map",
        help="Multiprocessing pool method to use for processing.",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    pool_method: str = args.pool_method
    text_mode: bool = bool(args.text)
    output_dir: Path | None = Path(args.output_dir) if args.output_dir is not None else None
    paths: list[str] = list(args.paths)
    process_pdfs(
        input_paths=paths or None,
        output_dir=output_dir,
        pool_method=pool_method,
        text_mode=text_mode,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
