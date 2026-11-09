#!/data/data/com.termux/files/usr/bin/env python
"""Extract the text of PDF files with PyPDF2 or pdfplumber.
The ``concat`` subcommand writes the whole document to one ``.txt``, ``split`` one file per page into a folder named after the PDF; the engine, the encoding, the page-number padding and the quiet mode are configurable.
Usage: pdf2text {concat,split} PDF [--engine pypdf2|pdfplumber] [-o OUT] Positional arguments: pdf Path to the input PDF file.
Subcommands: concat Extract all pages into a single text file.
split Extract each page into its own text file.
Options: --encoding Text encoding used when writing output files.
Default: utf-8 --quiet Suppress progress messages.
--engine PDF extraction engine.
Defaults: pypdf2 for concat, pdfplumber for split.
-o, --output Output .txt file.
Default: input path with .pdf replaced by .txt.
-d, --output-dir Directory for per-page text files.
Default: directory named after the PDF stem.
--pad-width Minimum page-number width.
Default: 3 (001, 002, ...).
--parents Create parent directories for --output-dir if needed.
Examples: $ pdf2text concat book.pdf -o book.txt $ pdf2text split book.pdf Command-line script: run ``python pdf2text.py`` or the ``pdf2text`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import sys
from typing import Iterator, Optional


def _extract_pages_pypdf2(pdf_path: Path) -> Iterator[str]:
    import PyPDF2

    with pdf_path.open("rb") as fh:
        reader = PyPDF2.PdfReader(fh)
        for page in reader.pages:
            text = page.extract_text()
            yield text or ""


def _extract_pages_pdfplumber(pdf_path: Path, encoding: str) -> Iterator[str]:
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text(encoding=encoding)
            except TypeError:
                text = page.extract_text()
            yield text or ""


def extract_pages(pdf_path: Path, engine: str, encoding: str) -> Iterator[str]:
    if engine == "pypdf2":
        yield from _extract_pages_pypdf2(pdf_path)
    elif engine == "pdfplumber":
        yield from _extract_pages_pdfplumber(pdf_path, encoding)
    else:
        msg = f"Unsupported engine: {engine}"
        raise ValueError(msg)


def write_text(path: Path, text: str, encoding: str) -> None:
    path.write_text(text, encoding=encoding)


def default_concat_output(pdf_path: Path) -> Path:
    return Path(str(pdf_path).replace(".pdf", ".txt"))


def default_split_output_dir(pdf_path: Path) -> Path:
    return Path(pdf_path.stem)


def concat_text(
    pdf_path: Path,
    output: Optional[Path],
    engine: str,
    encoding: str,
    quiet: bool,
) -> int:
    if output is None:
        output = default_concat_output(pdf_path)
    text = "".join(extract_pages(pdf_path, engine, encoding))
    write_text(output, text, encoding)
    if not quiet:
        print(f"Text extracted and saved to {output}")
    return 0


def split_text(
    pdf_path: Path,
    output_dir: Optional[Path],
    engine: str,
    encoding: str,
    pad_width: int,
    parents: bool,
    quiet: bool,
) -> int:
    if pad_width < 1:
        msg = "--pad-width must be at least 1"
        raise ValueError(msg)
    if output_dir is None:
        output_dir = default_split_output_dir(pdf_path)
    output_dir.mkdir(parents=parents, exist_ok=True)
    for page_number, text in enumerate(
        extract_pages(pdf_path, engine, encoding),
        start=1,
    ):
        page_str = f"{page_number:0{pad_width}d}"
        out_path = output_dir / f"{pdf_path.stem}{page_str}.txt"
        write_text(out_path, text, encoding)
        if not quiet:
            print(f"{out_path} created")
    return 0


def run_concat(args: argparse.Namespace) -> int:
    return concat_text(
        pdf_path=args.pdf,
        output=args.output,
        engine=args.engine,
        encoding=args.encoding,
        quiet=args.quiet,
    )


def run_split(args: argparse.Namespace) -> int:
    return split_text(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        engine=args.engine,
        encoding=args.encoding,
        pad_width=args.pad_width,
        parents=args.parents,
        quiet=args.quiet,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf_text_extractor.py",
        description="Extract text from PDF files using PyPDF2 or pdfplumber.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Extraction mode",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "pdf",
        type=Path,
        help="Path to the input PDF file.",
    )
    common.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding used when writing output files. Default: utf-8",
    )
    common.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages.",
    )
    common.add_argument(
        "--engine",
        choices=("pypdf2", "pdfplumber"),
        help=("PDF extraction engine. Defaults: pypdf2 for concat, pdfplumber for split."),
    )
    concat = subparsers.add_parser(
        "concat",
        parents=[common],
        help="Extract all pages into a single text file.",
    )
    concat.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=("Output .txt file. Default: input path with .pdf replaced by .txt."),
    )
    concat.set_defaults(func=run_concat, engine="pypdf2")
    split = subparsers.add_parser(
        "split",
        parents=[common],
        help="Extract each page into its own text file.",
    )
    split.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        default=None,
        help=("Directory for per-page text files. Default: directory named after the PDF stem."),
    )
    split.add_argument(
        "--pad-width",
        type=int,
        default=3,
        help="Minimum page-number width. Default: 3 (001, 002, ...).",
    )
    split.add_argument(
        "--parents",
        action="store_true",
        help="Create parent directories for --output-dir if needed.",
    )
    split.set_defaults(func=run_split, engine="pdfplumber")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.pdf.is_file():
        parser.error(f"PDF file not found: {args.pdf}")
    try:
        return args.func(args)
    except ImportError as exc:
        print(
            f"Missing dependency: {exc}. Install with: pip install PyPDF2 pdfplumber",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
