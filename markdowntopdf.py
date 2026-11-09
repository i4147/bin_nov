#!/data/data/com.termux/files/usr/bin/env python
"""Convert Markdown files to PDF with WeasyPrint.
Renders the Markdown (tables, magic links, better emphasis, fenced code; extras and a JSON config are accepted), styles it with the given CSS (``.xz`` supported, default ``/sdcard/_static/css/markdown.css.xz``) and writes one PDF per input, in parallel.
Usage: markdowntopdf FILE.md [more.md ...] [-o OUT.pdf] [--css STYLE.css] Positional arguments: md ...
Markdown input file(s) Options: -o, --output PDF output file (only with single input) --css CSS file to style the PDF (supports .xz) --extras Extra markdown extensions --config JSON configuration for extensions Examples: $ markdowntopdf notes.md -o notes.pdf Command-line script: run ``python markdowntopdf.py`` or the ``markdowntopdf`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import lzma
from pathlib import Path
import sys
from time import time
from typing import Any, Sequence

from dh import MAX_WORKERS
from loguru import logger
from markdown import markdown
from weasyprint import CSS, HTML


MARKDOWN_BASE_EXTENSIONS: list[str] = [
    "markdown.extensions.tables",
    "pymdownx.magiclink",
    "pymdownx.betterem",
    "pymdownx.superfences",
]
DEFAULT_CSS: Path = Path("/sdcard/_static/css/markdown.css.xz")


class ValidationError(Exception):
    pass


def md2pdf(pdf: Any, raw = None, md = None, css = None, base_url = None, extras = None, extras_config = None) -> None:
    extras_config = extras_config or {}
    extras = extras if extras and len(extras) else []
    if md:
        logger.debug("Reading markdown content from file {}", md)
        raw = md.read_text()
    if raw is None or not len(raw):
        msg = "No markdown content to process (empty file or raw string)"
        raise ValidationError(msg)
    extensions = MARKDOWN_BASE_EXTENSIONS + extras
    raw_html = markdown(raw, extensions=extensions, extension_configs=extras_config)
    if base_url is None:
        base_url = Path.cwd()
    html = HTML(string=raw_html, base_url=str(base_url))
    styles = []
    if css:
        css_path = Path(css)
        if css_path.suffix == ".xz":
            with lzma.open(css_path, "rt", encoding="utf-8") as f:
                css_text = f.read()
            styles.append(CSS(string=css_text, base_url=str(css_path.parent)))
        else:
            styles.append(CSS(filename=str(css_path)))
    html.write_pdf(pdf, stylesheets=styles)


def parse_config(config: dict[str, Any]):
    try:
        parsed = json.loads(config)
    except json.decoder.JSONDecodeError as err:
        msg = "Invalid input configuration string (should be valid JSON)"
        raise ValidationError(msg) from err
    return parsed


def _convert_one(md_path: Path, pdf_path: Path, css = None, extras = None, extras_config = None) -> None:
    md2pdf(
        pdf_path,
        md=md_path,
        css=css,
        base_url=Path.cwd(),
        extras=extras or None,
        extras_config=extras_config,
    )


def convert_batch(workers: int, md_files: Sequence[Path], pdf = None, css = None, extras = None, extras_config = None) -> None:
    started_at = time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for md_path in md_files:
            pdf_path = md_path.with_suffix(".pdf") if pdf is None else pdf
            futures.append(pool.submit(_convert_one, md_path, pdf_path, css, extras, extras_config))
        for f in futures:
            f.result()
    print(f"🚀 Output files generated in {(time() - started_at):.3f}s")


def main() -> None:
    parser = argparse.ArgumentParser(prog="md2pdf", usage="python md2pdf.py readme.md")
    parser.add_argument("md", nargs="*", help="Markdown input file(s)")
    parser.add_argument("-o", "--output", help="PDF output file (only with single input)")
    parser.add_argument("--css", help="CSS file to style the PDF (supports .xz)")
    parser.add_argument("--extras", nargs="*", help="Extra markdown extensions")
    parser.add_argument("--config", help="JSON configuration for extensions")
    args = parser.parse_args()
    if not args.md:
        print("🤷‍♂️ No markdown input file. See `--help`")
        sys.exit(2)
    md_files = [Path(f) for f in args.md]
    pdf = Path(args.output) if args.output else None
    if pdf is not None and len(md_files) > 1:
        print("❌ PDF output option `--output/-o` cannot be used with multiple input.")
        sys.exit(2)
    if args.css:
        css = Path(args.css)
    else:
        css = DEFAULT_CSS if DEFAULT_CSS.exists() else None
    if css:
        print(f"💅 CSS file: {css}")
    extras = args.extras or None
    if extras:
        print(f"🔧 Extras: {extras}")
    extras_config = None
    if args.config:
        extras_config = parse_config(args.config)
        print(f"🔧 Configuration: {extras_config}")
    convert_batch(MAX_WORKERS, md_files, pdf, css, extras, extras_config)


if __name__ == "__main__":
    main()
