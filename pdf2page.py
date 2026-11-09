#!/data/data/com.termux/files/usr/bin/env python
"""Extract every PDF page into its own text file.
Chooses between pdfminer, pypdf, PyMuPDF and pdfplumber (``-b``), writes one file per page into ``<pdf_dir>/<stem>/`` with a configurable name template, uses threads for the fitz/plumber backends and processes the rest, with ``--skip-existing`` for the resumes.
Usage: pdf2page [inputs ...] [-b pdfminer|fitz|pypdf|pdfplumber] [--name-template T] Positional arguments: inputs ...
PDF files and/or directories (default: cwd).
Options: -b, --backend PDF library backend (default: pdfminer).
-o, --output-dir Base output directory (default: <pdf_dir>/<stem>/).
-P, --parallel Parallelism mode (default: auto — thread for fitz/plumber, process for pypdf/pypdf2/pdfminer).
--name-template Output filename template.
Placeholders: {stem} {page} {total} {w}.
Default: page_{page:0{w}d}.txt -e, --encoding Output file encoding (default: utf-8).
--skip-existing Skip pages whose output file already exists.
--no-recursive Do not descend into subdirectories.
--sort Sort text blocks top-to-bottom/left-to-right (fitz backend only).
--password Password for encrypted PDFs (pdfminer backend).
-m, --maxpages Maximum pages per PDF (0 = all).
...
plus 9 more; run ``pdf2page --help`` for the full list.
Examples: $ pdf2page book.pdf -b fitz Command-line script: run ``python pdf2page.py`` or the ``pdf2page`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
import sys
from typing import Sequence

from dh import MAX_WORKERS


def _extract_plumber(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import pdfplumber

    out: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for idx in page_indices:
            try:
                text = pdf.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_fitz(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import fitz

    sort = bool(opts.get("sort", False))
    out: list[tuple[int, str]] = []
    doc = fitz.open(pdf_path)
    try:
        for idx in page_indices:
            try:
                page = doc[idx]
                text = page.get_text("text", sort=True) if sort else page.get_text()
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    finally:
        doc.close()
    return out


def _extract_pypdf(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    out: list[tuple[int, str]] = []
    with open(pdf_path, "rb") as fh:
        reader = PdfReader(fh)
        for idx in page_indices:
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_pypdf2(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import PyPDF2

    out: list[tuple[int, str]] = []
    with open(pdf_path, "rb") as fh:
        reader = PyPDF2.PdfReader(fh)
        for idx in page_indices:
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_pdfminer(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams

    laparams = None
    if not opts.get("no_laparams", False):
        kw: dict = {
            "detect_vertical": bool(opts.get("detect_vertical", False)),
            "all_texts": bool(opts.get("all_texts", False)),
        }
        for key in (
            "line_overlap",
            "char_margin",
            "line_margin",
            "word_margin",
            "boxes_flow",
        ):
            val = opts.get(key)
            if val is not None:
                kw[key] = val
        laparams = LAParams(**kw)
    password = opts.get("password") or ""
    out: list[tuple[int, str]] = []
    for idx in page_indices:
        try:
            text = extract_text(pdf_path, page_numbers=[idx], password=password, laparams=laparams) or ""
        except Exception as exc:
            print(
                f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                file=sys.stderr,
            )
            text = ""
        out.append((idx + 1, text))
    return out


_BACKENDS = {
    "plumber": _extract_plumber,
    "fitz": _extract_fitz,
    "pypdf": _extract_pypdf,
    "pypdf2": _extract_pypdf2,
    "pdfminer": _extract_pdfminer,
}
_THREAD_BACKENDS = {"fitz", "plumber"}


def _extract_chunk(pdf_path: str, page_indices: Sequence[int], backend: str, opts: dict) -> list[tuple[int, str]]:
    return _BACKENDS[backend](pdf_path, page_indices, opts)


def _page_count(pdf_path: str, backend: str, password: str = "") -> int:
    if backend == "plumber":
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    if backend == "fitz":
        import fitz

        doc = fitz.open(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()
    if backend == "pypdf":
        from pypdf import PdfReader

        with open(pdf_path, "rb") as fh:
            return len(PdfReader(fh).pages)
    if backend == "pypdf2":
        import PyPDF2

        with open(pdf_path, "rb") as fh:
            return len(PyPDF2.PdfReader(fh).pages)
    if backend == "pdfminer":
        from pdfminer.pdfpage import PDFPage

        with open(pdf_path, "rb") as fh:
            return sum(1 for _ in PDFPage.get_pages(fh, password=password or ""))
    msg = f"Unknown backend: {backend}"
    raise ValueError(msg)


def _collect_pdfs(inputs: Sequence[str], recursive: bool = True) -> list[Path]:
    if not inputs:
        inputs = ["."]
    seen: set = set()
    result: list[Path] = []
    for item in inputs:
        path = Path(item).expanduser()
        if path.is_file():
            if path.suffix.lower() == ".pdf":
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)
            else:
                print(f"Warning: {path} is not a PDF file", file=sys.stderr)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            for candidate in sorted(iterator):
                if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                    resolved = candidate.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        result.append(resolved)
        else:
            print(f"Warning: {path} does not exist", file=sys.stderr)
    return result


def _build_out_dir(pdf: Path, args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser() / pdf.stem
    return pdf.parent / pdf.stem


def _format_name(template: str, stem: str, page: int, total: int) -> str:
    width = max(3, len(str(total)))
    return template.format(stem=stem, page=page, total=total, w=width)


def _build_opts(args: argparse.Namespace) -> dict:
    return {
        "sort": args.sort,
        "password": args.password or "",
        "no_laparams": args.no_laparams,
        "line_overlap": args.line_overlap,
        "char_margin": args.char_margin,
        "line_margin": args.line_margin,
        "word_margin": args.word_margin,
        "boxes_flow": args.boxes_flow,
        "detect_vertical": args.detect_vertical,
        "all_texts": args.all_texts,
    }


def process_pdf(pdf: Path, args: argparse.Namespace) -> int:
    try:
        total = _page_count(str(pdf), args.backend, args.password or "")
    except Exception as exc:
        print(f"Error opening PDF {pdf}: {exc}", file=sys.stderr)
        return 0
    if total == 0:
        print(f"Skipping empty PDF: {pdf}")
        return 0
    if args.page_numbers:
        page_indices = [p - 1 for p in args.page_numbers if 1 <= p <= total]
    else:
        page_indices = list(range(total))
    if args.maxpages and args.maxpages > 0:
        page_indices = page_indices[: args.maxpages]
    if not page_indices:
        print(f"Nothing to extract from {pdf.name}")
        return 0
    out_dir = _build_out_dir(pdf, args)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Processing {pdf.name} ({len(page_indices)}/{total} pages) -> {out_dir}")
    to_process: list[int] = []
    skipped = 0
    for idx in page_indices:
        name = _format_name(args.name_template, pdf.stem, idx + 1, total)
        if args.skip_existing and (out_dir / name).exists():
            skipped += 1
            continue
        to_process.append(idx)
    if skipped:
        print(f"  Skipped {skipped} existing page file(s)")
    if not to_process:
        return 0
    workers = max(1, MAX_WORKERS)
    if len(to_process) <= workers:
        chunks: list[list[int]] = [[idx] for idx in to_process]
    else:
        chunk_size = max(1, (len(to_process) + workers - 1) // workers)
        chunks = [to_process[i : i + chunk_size] for i in range(0, len(to_process), chunk_size)]
    opts = _build_opts(args)
    parallel = args.parallel
    if parallel == "auto":
        parallel = "thread" if args.backend in _THREAD_BACKENDS else "process"
    results: list[tuple[int, str]] = []
    try:
        if parallel == "none" or workers == 1 or len(chunks) == 1:
            for chunk in chunks:
                results.extend(_extract_chunk(str(pdf), chunk, args.backend, opts))
        elif parallel == "thread":
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_extract_chunk, str(pdf), c, args.backend, opts) for c in chunks]
                for fut in as_completed(futures):
                    results.extend(fut.result())
        elif parallel == "process":
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_extract_chunk, str(pdf), c, args.backend, opts) for c in chunks]
                for fut in as_completed(futures):
                    results.extend(fut.result())
        else:
            msg = f"Unknown parallel mode: {parallel}"
            raise ValueError(msg)
    except Exception as exc:
        print(f"Error processing {pdf}: {exc}", file=sys.stderr)
        if not results:
            return 0
    written = 0
    for page_1based, text in sorted(results):
        name = _format_name(args.name_template, pdf.stem, page_1based, total)
        (out_dir / name).write_text(text, encoding=args.encoding)
        written += 1
    print(f"  Wrote {written} page file(s)")
    return written


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_pages_to_txt.py",
        description="Extract every PDF page into its own .txt file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            __doc__.split("Original script equivalents", 1)[-1] if "Original script equivalents" in __doc__ else None
        ),
    )
    p.add_argument("inputs", nargs="*", help="PDF files and/or directories (default: cwd).")
    p.add_argument(
        "-b",
        "--backend",
        choices=sorted(_BACKENDS.keys()),
        default="pdfminer",
        help="PDF library backend (default: pdfminer).",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Base output directory (default: <pdf_dir>/<stem>/).",
    )
    p.add_argument(
        "-P",
        "--parallel",
        choices=["auto", "thread", "process", "none"],
        default="auto",
        help="Parallelism mode (default: auto — thread for fitz/plumber, process for pypdf/pypdf2/pdfminer).",
    )
    p.add_argument(
        "--name-template",
        default="page_{page:0{w}d}.txt",
        help="Output filename template. Placeholders: {stem} {page} {total} {w}. Default: page_{page:0{w}d}.txt",
    )
    p.add_argument(
        "-e",
        "--encoding",
        default="utf-8",
        help="Output file encoding (default: utf-8).",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip pages whose output file already exists.",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories.",
    )
    p.add_argument(
        "--sort",
        action="store_true",
        help="Sort text blocks top-to-bottom/left-to-right (fitz backend only).",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Password for encrypted PDFs (pdfminer backend).",
    )
    p.add_argument("-m", "--maxpages", type=int, default=0, help="Maximum pages per PDF (0 = all).")
    p.add_argument(
        "-p",
        "--page-numbers",
        type=int,
        nargs="+",
        default=None,
        help="Specific 1-based page numbers to extract.",
    )
    p.add_argument("--no-laparams", action="store_true", help="Disable pdfminer layout analysis.")
    p.add_argument(
        "--line-overlap",
        type=float,
        default=0.5,
        help="pdfminer LAParams.line_overlap (default: 0.5).",
    )
    p.add_argument(
        "--char-margin",
        type=float,
        default=2.0,
        help="pdfminer LAParams.char_margin (default: 2.0).",
    )
    p.add_argument(
        "--line-margin",
        type=float,
        default=0.5,
        help="pdfminer LAParams.line_margin (default: 0.5).",
    )
    p.add_argument(
        "--word-margin",
        type=float,
        default=0.1,
        help="pdfminer LAParams.word_margin (default: 0.1).",
    )
    p.add_argument(
        "--boxes-flow",
        type=float,
        default=0.5,
        help="pdfminer LAParams.boxes_flow (default: 0.5).",
    )
    p.add_argument(
        "--detect-vertical",
        action="store_true",
        help="pdfminer LAParams.detect_vertical.",
    )
    p.add_argument("--all-texts", action="store_true", help="pdfminer LAParams.all_texts.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if MAX_WORKERS < 1:
        parser.error("--workers must be >= 1")
    pdfs = _collect_pdfs(args.inputs, recursive=not args.no_recursive)
    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1
    print(f"Found {len(pdfs)} PDF file(s) to process.")
    total_written = 0
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}")
        total_written += process_pdf(pdf, args)
    print(f"Done. Total page files written: {total_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
