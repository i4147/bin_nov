#!/data/data/com.termux/files/usr/bin/env python
"""Save a web page as PDF with pdfkit, reporting the errors.
Normalises the URL, names the output after the sanitised domain, converts it with pdfkit and returns a non-zero exit status when the conversion fails.
Usage: url2pdf3 URL Examples: $ url2pdf3 example.com Runs as a script (``python url2pdf3.py``) in the directory it should act on; the module can also be imported.
"""

import sys
from urllib.parse import urlparse


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"http://{url}"
        parsed = urlparse(url)
    domain = parsed.netloc.lower()
    domain = domain.removeprefix("www.")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in domain)
    return f"{safe}.pdf"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: python3 script.py <url>\n")
        return 1
    target_url = argv[1]
    output_pdf = url_to_filename(target_url)
    try:
        import pdfkit

        pdfkit.from_url(target_url, output_pdf)
        print(f"Saved PDF to {output_pdf}")
    except Exception as exc:
        sys.stderr.write(f"Error creating PDF: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
