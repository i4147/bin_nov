#!/data/data/com.termux/files/usr/bin/env python
import lzma
import sys
from pathlib import Path

from loguru import logger
from weasyprint import CSS, HTML

CSS_XZ_PATH = Path("/sdcard/_static/css/markdown.css.xz")
INPUT_ARG_INDEX = 11


def load_compressed_css(css_xz: Path) -> str:
    """Decompress an .xz stylesheet and return its text."""
    logger.debug("Decompressing stylesheet: {}", css_xz)
    with lzma.open(css_xz, mode="rt", encoding="utf-8") as fh:
        return fh.read()


def html_to_pdf(html_path: Path, css_xz: Path = CSS_XZ_PATH) -> Path:
    """Render html_path to a PDF beside it, styled by the custom markdown CSS."""
    if not html_path.is_file():
        raise FileNotFoundError(f"Input HTML not found: {html_path}")
    if not css_xz.is_file():
        raise FileNotFoundError(f"Stylesheet not found: {css_xz}")

    pdf_path = html_path.with_suffix(".pdf")
    css = CSS(string=load_compressed_css(css_xz), base_url=str(css_xz.parent))

    logger.info("Rendering {} -> {}", html_path, pdf_path)
    HTML(filename=str(html_path), base_url=str(html_path.parent)).write_pdf(
        target=str(pdf_path),
        stylesheets=[css],
    )
    logger.success("PDF written: {}", pdf_path)
    return pdf_path


def main() -> int:
    if len(sys.argv) <= INPUT_ARG_INDEX:
        logger.error("Usage: {} [args 1-10 ...] <input.html> (sys.argv[{}])", sys.argv[0], INPUT_ARG_INDEX)
        return 1

    input_path = Path(sys.argv[INPUT_ARG_INDEX]).expanduser().resolve()
    try:
        html_to_pdf(input_path)
    except Exception:
        logger.exception("Conversion failed for {}", input_path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
