#!/data/data/com.termux/files/usr/bin/env python
"""Repair relative links in Markdown and HTML files against a static directory.
Walks the current directory, and for every ``href`` whose target does not exist locally but does exist under ``/sdcard/_static`` rewrites the link to the absolute path.
The original file is kept as ``.bak``.
Usage: fix_html_md_broken_links Examples: $ fix_html_md_broken_links Runs as a script (``python fix_html_md_broken_links.py``) in the directory it should act on; the module can also be imported.
"""

import os
from pathlib import Path
import re

static_dir: str = "/sdcard/_static"


def fix_links(path: Path) -> None:
    content: str = path.read_text(encoding="utf-8", errors="replace")
    links = re.findall(r"href=[\'\"]?([^\'\" >]+)", content)
    for link in links:
        if not Path(link).exists():
            static_file = static_dir / link
            if static_file.exists():
                content = content.replace(link, str(static_file.resolve()))
    backup_path = path.with_suffix(".bak")
    Path(path).replace(backup_path)
    Path(path).write_text(content, encoding="utf-8")


def main() -> None:
    for root, _dirs, files in os.walk("."):
        for file in files:
            if file.endswith((".md", ".html")):
                path = Path(root) / file
                fix_links(path)


if __name__ == "__main__":
    raise SystemExit(main())
