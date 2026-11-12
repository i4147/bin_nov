#!/data/data/com.termux/files/usr/bin/env python
"""Convert HTML files to Markdown with readability.
Extracts the main article with ``readability``, converts it with ``markdownify`` and writes the ``.md`` next to the source, deleting the original HTML on success.
Existing ``.md`` files are left alone and the tree is processed in parallel.
Usage: tomd [html files ...] Examples: $ tomd saved_pages/ Runs as a script (``python tomd.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import get_files, mpf
from markdownify import markdownify as md
from readability import Document

remove_orig: bool = True


def process_file(path: Path) -> tuple[Path, bool]:
    path = Path(path)
    md_file = path.with_suffix(".md")
    if md_file.exists():
        return (md_file, True)
    try:
        html_content = path.read_text(encoding="utf-8", errors="ignore")
        doc = Document(html_content)
        main_content = doc.summary()
        markdown = md(main_content)
        if markdown and markdown.strip():
            md_file.write_text(markdown, encoding="utf-8")
            print(f"✓ Converted: {path.name} -> {md_file.name}")
            if remove_orig:
                path.unlink()
            return (md_file, True)
        print(f"✗ No content extracted from {path.name}")
        return (path, False)
    except Exception as e:
        print(f"✗ Error: {e}")
        return (path, False)


if __name__ == "__main__":
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".html", ".htm", ".xhtml", ".xhtm"])
    if len(files) == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)
