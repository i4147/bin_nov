#!/data/data/com.termux/files/usr/bin/env python
"""PDF text extraction, split, merge and resource extraction with PyMuPDF.
``-p`` writes the per-page text, ``-s`` splits the pages into single-page PDFs, ``-m`` merges the inputs into ``merged.pdf`` and ``-x`` extracts the embedded images, fonts and attachments into ``<stem>/images``, ``fonts`` and ``<stem>/attachments``; the work runs in parallel.
Usage: pdftools INPUT.pdf [...] [-p] [-s] [-m] [-x] [-o OUTDIR] Positional arguments: inputs ...
One or more input PDF files Options: -p, --per-page Extract the text of every page into separate .txt files in a folder named after the input stem -s, --split Split every input into one PDF per page, in a folder named after the input stem -m, --merge -x, --extract Extract embedded images, fonts and attached files into <stem>/images, fonts and attachments -o, --output-dir Base output directory Examples: $ pdftools book.pdf -p -x Command-line script: run ``python pdftools.py`` or the ``pdftools`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING

from dh import MAX_WORKERS
import pymupdf


MERGED_NAME: str = "merged.pdf"
UNSAFE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")


def count_pages(pdf_path: Path) -> int:
    with pymupdf.open(pdf_path) as document:
        return len(document)


def extract_one_page(job: tuple[str, int]) -> str:
    pdf_name, page_number = job
    with pymupdf.open(pdf_name) as document:
        page = document.load_page(page_number)
        return page.get_text("text").rstrip("\f")


def safe_name(value: str, fallback: str) -> str:
    cleaned = UNSAFE.sub("_", value).strip("._")
    return cleaned or fallback


def is_readable(path: Path) -> bool:
    try:
        with pymupdf.open(path) as document:
            return not document.needs_pass and len(document) > 0
    except Exception:
        return False


def chunked(items: Sequence[int], parts: int) -> list[list[int]]:
    size = max(1, -(-len(items) // max(1, parts)))
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def split_one_page(job: tuple[str, int, str]) -> str:
    pdf_name, page_number, target = job
    with pymupdf.open(pdf_name) as source, pymupdf.open() as output:
        output.insert_pdf(source, from_page=page_number, to_page=page_number)
        output.save(target, garbage=3, deflate=True)
    return target


def scan_resources(job: tuple[str, int, int]) -> tuple[list[int], list[int]]:
    pdf_name, start, end = job
    images: set[int] = set()
    fonts: set[int] = set()
    with pymupdf.open(pdf_name) as document:
        for number in range(start, end):
            images.update(item[0] for item in document.get_page_images(number, full=True))
            fonts.update(item[0] for item in document.get_page_fonts(number, full=True))
    images.discard(0)
    fonts.discard(0)
    return sorted(images), sorted(fonts)


def extract_images(job: tuple[str, list[int], str]) -> int:
    pdf_name, xrefs, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for xref in xrefs:
            try:
                info = document.extract_image(xref)
            except (RuntimeError, ValueError):
                continue
            if not info or not info.get("image"):
                continue
            ext = str(info.get("ext") or "bin")
            (folder / f"image_{xref:06d}.{ext}").write_bytes(info["image"])
            saved += 1
    return saved


def extract_fonts(job: tuple[str, list[int], str]) -> int:
    pdf_name, xrefs, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for xref in xrefs:
            try:
                name, ext, _kind, content = document.extract_font(xref)
            except (RuntimeError, ValueError):
                continue
            if not content or ext == "n/a":
                continue
            (folder / f"{safe_name(str(name), 'font')}_{xref:06d}.{ext}").write_bytes(content)
            saved += 1
    return saved


def extract_annotation_files(job: tuple[str, int, int, str]) -> int:
    pdf_name, start, end, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for number in range(start, end):
            page = document.load_page(number)
            for index, annot in enumerate(page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT])):
                try:
                    info = annot.file_info
                    content = annot.get_file()
                except (RuntimeError, ValueError):
                    continue
                filename = safe_name(str(info.get("filename") or ""), "attachment")
                (folder / f"page{number + 1:04d}_{index:02d}_{filename}").write_bytes(content)
                saved += 1
    return saved


def extract_embedded_files(pdf: Path, target: Path) -> int:
    saved = 0
    with pymupdf.open(pdf) as document:
        for index, name in enumerate(document.embfile_names()):
            info = document.embfile_info(name)
            raw_name = str(info.get("ufilename") or info.get("filename") or name)
            filename = safe_name(raw_name, f"embedded_{index}")
            (target / f"embedded_{index:03d}_{filename}").write_bytes(document.embfile_get(name))
            saved += 1
    return saved


def run_per_page(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> int:
    total = count_pages(pdf)
    width = len(str(total))
    folder = out_root / pdf.stem
    folder.mkdir(parents=True, exist_ok=True)
    tasks = [(str(pdf), number) for number in range(total)]
    chunksize = max(1, total // (jobs * 4))
    results = pool.map(extract_one_page, tasks, chunksize=chunksize)
    for number, text in enumerate(results, start=1):
        (folder / f"{pdf.stem}_page_{number:0{width}d}.txt").write_text(text, encoding="utf-8")
    return total


def run_split(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> int:
    total = count_pages(pdf)
    width = len(str(total))
    folder = out_root / pdf.stem
    folder.mkdir(parents=True, exist_ok=True)
    tasks = [(str(pdf), number, str(folder / f"{pdf.stem}_page_{number + 1:0{width}d}.pdf")) for number in range(total)]
    chunksize = max(1, total // (jobs * 4))
    for _ in pool.map(split_one_page, tasks, chunksize=chunksize):
        pass
    return total


def run_extract(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> dict[str, int]:
    total = count_pages(pdf)
    base = out_root / pdf.stem
    folders: dict[str, Path] = {name: base / name for name in ("images", "fonts", "attachments")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    ranges = [(part[0], part[-1] + 1) for part in chunked(range(total), jobs * 2)]
    image_xrefs: set[int] = set()
    font_xrefs: set[int] = set()
    for images, fonts in pool.map(scan_resources, [(str(pdf), start, end) for start, end in ranges]):
        image_xrefs.update(images)
        font_xrefs.update(fonts)
    image_jobs = [(str(pdf), part, str(folders["images"])) for part in chunked(sorted(image_xrefs), jobs)]
    font_jobs = [(str(pdf), part, str(folders["fonts"])) for part in chunked(sorted(font_xrefs), jobs)]
    annot_jobs = [(str(pdf), start, end, str(folders["attachments"])) for start, end in ranges]
    counts: dict[str, int] = {
        "images": sum(pool.map(extract_images, image_jobs)),
        "fonts": sum(pool.map(extract_fonts, font_jobs)),
        "attachments": extract_embedded_files(pdf, folders["attachments"])
        + sum(pool.map(extract_annotation_files, annot_jobs)),
    }
    for folder in folders.values():
        if not any(folder.iterdir()):
            folder.rmdir()
    if not any(base.iterdir()):
        base.rmdir()
    return counts


def merge_pdfs(pdfs: Sequence[Path], target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as merged:
        for pdf in pdfs:
            with pymupdf.open(pdf) as source:
                merged.insert_pdf(source)
        merged.save(target, garbage=3, deflate=True)
        return len(merged)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pdftools",
        description="PDF utilities built on PyMuPDF: text extraction, split, merge and object extraction.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, metavar="PDF", help="one or more input PDF files")
    parser.add_argument(
        "-p",
        "--per-page",
        action="store_true",
        help="extract the text of every page into separate .txt files in a folder named after the input stem",
    )
    parser.add_argument(
        "-s",
        "--split",
        action="store_true",
        help="split every input into one PDF per page, in a folder named after the input stem",
    )
    parser.add_argument(
        "-m",
        "--merge",
        action="store_true",
        help=f"merge all inputs, in the order given, into {MERGED_NAME}",
    )
    parser.add_argument(
        "-x",
        "--extract",
        action="store_true",
        help="extract embedded images, fonts and attached files into <stem>/images, fonts and attachments",
    )
    parser.add_argument("-o", "--output-dir", type=Path, default=Path(), help="base output directory")
    args = parser.parse_args(argv)
    if not (args.per_page or args.split or args.merge or args.extract):
        parser.error("choose at least one of -p, -s, -m, -x")
    if MAX_WORKERS < 1:
        parser.error("--jobs must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    failed = False
    pdfs: list[Path] = []
    for path in args.inputs:
        if path.is_file() and is_readable(path):
            pdfs.append(path)
        else:
            print(f"skipped (missing, unreadable, empty or encrypted): {path}", file=sys.stderr)
            failed = True
    if not pdfs:
        return 1
    out_root: Path = args.output_dir
    out_root.mkdir(parents=True, exist_ok=True)
    jobs: int = MAX_WORKERS
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for pdf in pdfs:
            try:
                if args.per_page:
                    pages = run_per_page(pdf, out_root, pool, jobs)
                    print(f"{pdf.name}: text of {pages} pages -> {out_root / pdf.stem}")
                if args.split:
                    pages = run_split(pdf, out_root, pool, jobs)
                    print(f"{pdf.name}: split into {pages} pdf files -> {out_root / pdf.stem}")
                if args.extract:
                    counts = run_extract(pdf, out_root, pool, jobs)
                    summary = ", ".join(f"{name}={count}" for name, count in counts.items())
                    print(f"{pdf.name}: extracted {summary} -> {out_root / pdf.stem}")
            except Exception as error:
                print(f"{pdf.name}: failed: {error}", file=sys.stderr)
                failed = True
    if args.merge:
        try:
            target = out_root / MERGED_NAME
            pages = merge_pdfs(pdfs, target)
            print(f"merged {len(pdfs)} files ({pages} pages) -> {target}")
        except Exception as error:
            print(f"merge failed: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
