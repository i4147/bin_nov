#!/data/data/com.termux/files/usr/bin/env python
"""Render a web page to PDF with WeasyPrint.
Passes the URL straight to ``weasyprint.HTML`` and writes the PDF; the entry point uses the fixed ``https://example.com`` demo values, so import :func:`convert_url_to_pdf` to convert another page.
Usage: python url2pdf.py Examples: $ python url2pdf.py Runs as a script (``python url2pdf.py``) in the directory it should act on; the module can also be imported."""

import sys

from weasyprint import HTML


def convert_url_to_pdf(url: str, output_filename: str):
    print(f"Fetching and converting: {url}...")
    try:
        HTML(url).write_pdf(output_filename)
        print(f"Success! PDF saved as '{output_filename}'")
    except Exception as e:
        print(f"An error occurred during conversion: {e}", file=sys.stderr)


if __name__ == "__main__":
    target_url = "https://example.com"
    output_pdf = "website_snapshot.pdf"
    convert_url_to_pdf(target_url, output_pdf)
