#!/data/data/com.termux/files/usr/bin/env python
"""Convert reStructuredText files to Markdown.
Renders the RST with docutils to HTML and then converts that HTML with ``html_to_markdown``, writing the ``.md`` file next to the source and deleting the original.
Files are converted in parallel.
Usage: rst2md [files ...] Examples: $ rst2md docs/ The original ``.rst`` file is removed after a successful conversion."""

from pathlib import Path
import sys

from dh import get_files, mpf
from docutils.core import publish_parts
from html_to_markdown import markdownify


def rst_to_html(content: str) -> str:
    try:
        parts = publish_parts(
            source=content,
            writer_name="html",
            settings_overrides={
                "initial_header_level": 2,
                "warning_stream": None,
                "report_level": 5,
            },
        )
        html_content = parts["html_body"]
        return html_content
    except Exception as e:
        print(f"Conversion error details: {e}")
        raise


def process_file(path):
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    html_content = rst_to_html(content)
    md_content = markdownify(html_content)
    md_path = path.with_suffix(".md")
    md_path.write_text(md_content, encoding="utf-8")
    path.unlink()


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = list([Path(p) for p in args] if args else get_files(cwd, ext=[".rst"]))
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
