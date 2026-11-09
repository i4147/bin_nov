#!/data/data/com.termux/files/usr/bin/env python
"""Extract the text of PDF files with a choice of backends.
``--backend`` selects fitz (PyMuPDF), pypdf, pdfplumber or pdfminer; pages can be joined or written one file per page with ``--perpage``, spaces are normalised, and ``--password`` opens protected documents.
Usage: pdftotext FILE.pdf [more.pdf ...] [--backend fitz|pypdf|pdfplumber|pdfminer] [--perpage] Positional arguments: pdffiles ...
Options: -p, --perpage -b, --backend --password Examples: $ pdftotext report.pdf --backend pypdf $ pdftotext book.pdf --perpage Command-line script: run ``python pdftotext.py`` or the ``pdftotext`` command.
The module is importable as well."""

import argparse
from io import StringIO
from pathlib import Path
import re


class Extractor(object):
    def __init__(
        self,
        backend="fitz",
        password="",
        encoding="utf-8",
        normalize_spaces=True,
    ) -> None:
        self.backend = backend.lower()
        self.password = password
        self.encoding = encoding
        self.normalize_spaces = normalize_spaces

    def extract_pages(self, pdf_path: Path) -> list[str]:
        pdf_path = Path(pdf_path)
        pages = []
        if self.backend == "fitz":
            import fitz

            doc = fitz.open(pdf_path)
            if self.password:
                doc.authenticate(self.password)
            for page in doc:
                pages.append(page.get_text())
            doc.close()
        elif self.backend == "pypdf":
            from pypdf import PdfReader

            reader = PdfReader(pdf_path, password=self.password) if self.password else PdfReader(pdf_path)
            for page in reader.pages:
                pages.append(page.extract_text() or "")
        elif self.backend == "pdfplumber":
            import pdfplumber

            kwargs = {"password": self.password} if self.password else {}
            with pdfplumber.open(pdf_path, **kwargs) as pdf:
                for page in pdf.pages:
                    pages.append(page.extract_text() or "")
        elif self.backend == "pdfminer":
            from pdfminer.converter import TextConverter
            from pdfminer.layout import LAParams
            from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
            from pdfminer.pdfpage import PDFPage

            rsrcmgr = PDFResourceManager(caching=True)
            laparams = LAParams(detect_vertical=True, char_margin=1.0, line_margin=0.3, word_margin=0.3)
            with open(pdf_path, "rb") as fp:
                for page in PDFPage.get_pages(fp, password=self.password, caching=True, check_extractable=True):
                    outfp = StringIO()
                    device = TextConverter(rsrcmgr, outfp, codec=self.encoding, laparams=laparams)
                    interpreter = PDFPageInterpreter(rsrcmgr, device)
                    interpreter.process_page(page)
                    pages.append(outfp.getvalue())
                    device.close()
                    outfp.close()
        else:
            msg = f"Unsupported backend: {self.backend}"
            raise ValueError(msg)
        if self.normalize_spaces:
            pages = [re.sub(r" +", " ", page) for page in pages]
        return pages

    def __call__(self, pdf_path: Path) -> str:
        return "\n".join(self.extract_pages(pdf_path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdffiles", nargs="+")
    parser.add_argument("-p", "--perpage", action="store_true")
    parser.add_argument(
        "-b",
        "--backend",
        default="fitz",
        choices=["fitz", "pypdf", "pdfplumber", "pdfminer"],
    )
    parser.add_argument("--password", default="")
    args = parser.parse_args()
    extractor = Extractor(
        backend=args.backend,
        password=args.password,
    )
    for fname in args.pdffiles:
        pdf_path = Path(fname)
        if not pdf_path.exists():
            continue
        pages = extractor.extract_pages(pdf_path)
        if args.perpage:
            out_dir = pdf_path.parent / pdf_path.stem
            out_dir.mkdir(parents=True, exist_ok=True)
            for idx, page_text in enumerate(pages, start=1):
                out_file = out_dir / f"page_{idx}.txt"
                out_file.write_text(page_text, encoding="utf-8")
        else:
            print("\n".join(pages))


if __name__ == "__main__":
    main()
