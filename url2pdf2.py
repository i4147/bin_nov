#!/data/data/com.termux/files/usr/bin/env python
"""Save a web page as PDF with pdfkit, named after the site.
Takes the URL on the command line, derives a safe file name from the host (``www.`` removed) and writes ``<host>.pdf`` through ``pdfkit.from_url``.
Usage: url2pdf2 URL Examples: $ url2pdf2 https://example.com/docs Runs as a script (``python url2pdf2.py``) in the directory it should act on; the module can also be imported."""

import re
import sys
from urllib.parse import urlparse

import pdfkit


def get_site_name(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    host = urlparse(url).hostname or "website"
    host = re.sub(r"^www\.", "", host)
    return re.sub(r"[^A-Za-z0-9_-]", "_", host)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python script.py <url>")
        sys.exit(1)
    target_url = sys.argv[1]
    output_pdf = f"{get_site_name(target_url)}.pdf"
    pdfkit.from_url(target_url, output_pdf)
    print(f"Saved: {output_pdf}")


if __name__ == "__main__":
    main()
