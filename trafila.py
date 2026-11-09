#!/data/data/com.termux/files/usr/bin/env python
"""Extract the main article of HTML pages as Markdown with trafilatura.
Runs ``trafilatura.extract`` in Markdown mode with links, images and tables, falls back to ``html_to_markdown`` on the cleaned soup when nothing is extracted and writes the ``.md`` file next to the source; ``-r`` also removes the original HTML.
Usage: trafila [-r] [html files ...] Examples: $ trafila -r pages/ Runs as a script (``python trafila.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import get_files, mpf
import trafilatura


remove_orig = "-r" in sys.argv


def process_file(path: str | Path) -> tuple[Path, bool]:
    path = Path(path)
    md_file = path.with_suffix(".md")
    if md_file.exists():
        return md_file, True
    try:
        html_content = path.read_text(encoding="utf-8")
        markdown = trafilatura.extract(
            html_content,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
            no_fallback=False,
        )
        if not markdown:
            from bs4 import BeautifulSoup
            from html_to_markdown import markdownify

            soup = BeautifulSoup(html_content, "html.parser")
            markdown = markdownify(str(soup))
        if markdown and markdown.strip():
            md_file.write_text(markdown, encoding="utf-8")
            print(f"✓ Converted: {path.name} -> {md_file.name}")
            if remove_orig:
                path.unlink()
            return md_file, True
        print(f"✗ No content extracted from {path.name}")
        return path, False
    except Exception as e:
        print(f"✗ Error processing {path.name}: {e}")
        return path, False


if __name__ == "__main__":
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = list(
        [Path(p) for p in args if p.strip() != "-r"]
        if args and len(args) > 1
        else get_files(cwd, [".html", ".htm", ".xhtml", ".xhtm"])
    )
    numf = len(files)
    if numf == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)
