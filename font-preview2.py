#!/data/data/com.termux/files/usr/bin/env python
"""Generate font_preview.html previewing every font below the current directory.

Scans for .ttf/.otf/.woff/.woff2 files, writes a self-contained HTML page with one card per font (relative URLs, inline CSS/JS filter) and reports the count.
"""

from __future__ import annotations
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote


OUTPUT: Path = Path("font_preview.html")
FONT_FORMATS: dict[str, tuple[Any, ...]] = {
    ".ttf": ("font/ttf", "truetype"),
    ".otf": ("font/otf", "opentype"),
    ".woff": ("font/woff", "woff"),
    ".woff2": ("font/woff2", "woff2"),
}


def main() -> None:
    root = Path.cwd()
    fonts = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in FONT_FORMATS
            and path.resolve() != OUTPUT.resolve()
        ),
        key=lambda path: str(path).lower(),
    )

    font_faces = []
    cards = []

    for index, path in enumerate(fonts):
        family = f"PreviewFont{index}"
        _mime_type, css_format = FONT_FORMATS[path.suffix.lower()]
        relative_path = path.relative_to(root).as_posix()
        font_url = quote(relative_path, safe="/")

        font_faces.append(
            f"""
            @font-face {{
                font-family: '{family}';
                src: url("{font_url}") format("{css_format}");
                font-style: normal;
                font-weight: 400;
            }}
            """
        )
        cards.append(
            f"""
            <section class="font-card">
                <div class="font-name">{escape(relative_path)}</div>
                <div class="sample" style="font-family: '{family}', sans-serif">
                    The quick brown fox jumps over the lazy dog.<br>
                    0123456789 &mdash; Aa Bb Cc
                </div>
            </section>
            """
        )

    if not fonts:
        print(f"No supported fonts found under {root}")
        return

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Font Preview</title>
<style>
{"".join(font_faces)}
* {{ box-sizing: border-box; }}
body {{
    margin: 0;
    padding: 24px;
    color: #222;
    background: #f5f5f5;
    font: 16px system-ui, sans-serif;
}}
h1 {{ margin-top: 0; }}
.controls {{
    position: sticky;
    top: 0;
    padding: 12px 0;
    background: #f5f5f5;
}}
input {{ width: min(100%, 360px); padding: 8px; }}
.font-card {{
    margin: 12px 0;
    padding: 16px;
    border: 1px solid #ddd;
    border-radius: 8px;
    background: white;
}}
.font-name {{ margin-bottom: 12px; color: #555; overflow-wrap: anywhere; }}
.sample {{ font-size: 32px; line-height: 1.5; overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>Font Preview</h1>
<div class="controls">
    <label for="filter">Filter fonts: </label>
    <input id="filter" type="search" placeholder="Type a filename or folder">
</div>
{"".join(cards)}
<script>
const filter = document.getElementById('filter');
filter.addEventListener('input', () => {{
    const query = filter.value.toLowerCase();
    document.querySelectorAll('.font-card').forEach(card => {{
        card.hidden = !card.querySelector('.font-name').textContent
            .toLowerCase().includes(query);
    }});
}});
</script>
</body>
</html>
"""

    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Found {len(fonts)} font(s). Created {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
