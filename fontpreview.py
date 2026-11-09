#!/data/data/com.termux/files/usr/bin/env python
"""Generate HTML pages that preview the fonts of a directory.
The ``simple`` subcommand writes one block per font at the given sizes, ``rich`` a page with editable sample areas and a dark mode; the sample text and the sizes come from the ``--preset`` (Persian by default) or from ``--text``/``--sizes``.
Usage: fontpreview {simple,rich} [paths ...] [--text TEXT] [--sizes 14,22] Positional arguments: paths ...
Files/directories to scan (default: cwd) Subcommands: simple Simple one-font-per-block preview (fafontpreview.py / fontpreview.py) rich Rich interactive preview with textareas + dark mode (fontpre.py) Options: --preset Preset text/sizes/output (default: fa) --text Override the sample text (defaults to preset's) --sizes Override font sizes in px (defaults to preset's) -o, --output Output HTML file (defaults to preset's) -v, --verbose --max-fonts Examples: $ fontpreview rich ~/fonts -o preview.html Command-line script: run ``python fontpreview.py`` or the ``fontpreview`` command.
The module is importable as well."""

import argparse
from collections import namedtuple
from datetime import datetime
from functools import lru_cache
import html
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from loguru import logger


LOG = logger
FONT_EXTS: frozenset[str] = frozenset({
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    ".svg",
})
PRESETS: dict[str, dict] = {
    "fa": {
        "text": "هنر برتز از گوهر آمد پدید",
        "sizes": [14, 22],
        "output": "fa_fonts_preview.html",
    },
    "en": {
        "text": "LIFE IS A DREAM,we are dreaming.",
        "sizes": [16, 22, 28],
        "output": "fonts_preview.html",
    },
}
RICH_TEXT_DEFAULT: str = "Lorem ipsum dolor sit amet\nهنر برتر از گوهر آمد پدید"
RICH_OUTPUT_DEFAULT: str = "fontpreview.html"
RICH_MAX_FONTS_DEFAULT: int = 10_000
_FONT_FORMATS: dict[str, str] = {
    ".ttf": "TrueType",
    ".otf": "OpenType",
    ".woff": "WOFF",
    ".woff2": "WOFF2",
    ".eot": "Embedded OpenType",
    ".svg": "SVG Font",
}


def find_fonts(
    paths: Sequence[Path],
    *,
    max_fonts: Optional[int] = None,
) -> list[Path]:
    if not paths:
        paths = [Path.cwd()]
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, ValueError) as e:
            LOG.warning("Invalid path {}: {}", raw, e)
            continue
        if not p.exists():
            LOG.warning("Path does not exist: {}", p)
            continue
        if p.is_file():
            if p.suffix.lower() in FONT_EXTS:
                found.append(p)
            else:
                LOG.debug("Skipping non-font file: {}", p)
            continue
        for f in p.rglob("*"):
            if f in seen:
                continue
            seen.add(f)
            if not f.is_file():
                continue
            if f.suffix.lower() not in FONT_EXTS:
                continue
            found.append(f)
            if max_fonts is not None and len(found) >= max_fonts:
                LOG.warning("Reached maximum font limit ({})", max_fonts)
                return found
    return sorted(found, key=lambda x: (x.parent, x.name))


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


@lru_cache(maxsize=128)
def font_format(ext: str) -> str:
    return _FONT_FORMATS.get(ext.lower(), ext.upper().lstrip("."))


def _parse_paths(raw: Sequence[str]) -> list[Path]:
    if not raw:
        return [Path.cwd()]
    out: list[Path] = []
    for r in raw:
        try:
            p = Path(r).expanduser().resolve()
            if p.exists():
                out.append(p)
            else:
                LOG.warning("Path does not exist: {}", r)
        except (OSError, ValueError) as e:
            LOG.warning("Invalid path {}: {}", r, e)
    return out or [Path.cwd()]


def _generate_simple_html(fonts: Sequence[Path], text: str, sizes: Sequence[int]) -> str:
    esc_text = html.escape(text)
    parts: list[str] = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<title>Font Preview</title>",
        "<link rel=stylesheet src='/sdcard/_static/fontello.css'></link></head>",
        "<body>",
        "<h1>Font Preview</h1>",
    ]
    for p in fonts:
        name = p.name
        parts.append("<div class='font-preview'>")
        parts.append("<style>")
        parts.append(f"@font-face {{ font-family:'{name}'; src:url('{p}'); }}")
        parts.append("</style>")
        for size in sizes:
            parts.append(f"<div style='font-family:\"{name}\"; font-size:{size}px;'>{esc_text}</div>")
        parts.append(f"<div style='font-family:\"{name}\"; font-size:14px;'>{name}</div><hr><br/>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def cmd_simple(args: argparse.Namespace) -> int:
    preset = PRESETS[args.preset]
    text: str = args.text if args.text is not None else preset["text"]
    sizes: list[int] = list(args.sizes) if args.sizes else list(preset["sizes"])
    output: Path = Path(args.output) if args.output else Path(preset["output"])
    paths = _parse_paths(args.paths)
    LOG.info("Searching for fonts in {} location(s)...", len(paths))
    fonts = find_fonts(paths)
    if not fonts:
        LOG.warning("No font files found. Supported: {}", ", ".join(sorted(FONT_EXTS)))
        return 1
    LOG.info("Found {} font(s)", len(fonts))
    doc = _generate_simple_html(fonts, text, sizes)
    output.write_text(doc, encoding="utf-8")
    LOG.info("Wrote {} ({})", output, format_size(output.stat().st_size))
    return 0


FontInfo = namedtuple("FontInfo", ["path", "index", "size", "format"])


def _collect_font_infos(
    paths: Sequence[Path],
    *,
    max_fonts: int = RICH_MAX_FONTS_DEFAULT,
) -> list[FontInfo]:
    infos: list[FontInfo] = []
    for i, p in enumerate(find_fonts(paths, max_fonts=max_fonts), 1):
        try:
            st = p.stat()
        except OSError as e:
            LOG.debug("Skipping {}: {}", p, e)
            continue
        infos.append(FontInfo(p, i, st.st_size, font_format(p.suffix)))
    infos.sort(key=lambda f: (f.path.parent, f.path.name))
    return infos


def _rich_font_face(info: FontInfo, root: Path) -> str:
    try:
        rel = info.path.relative_to(root).as_posix()
    except ValueError:
        rel = info.path.as_posix()
        if not rel.startswith("/"):
            rel = "/" + rel
    family = f"font_{info.index:04d}"
    return (
        "@font-face {\n"
        f"  font-family:'{family}';\n"
        f"  src:url('{rel}');\n"
        "  font-display:swap;\n"
        "  font-weight:normal;\n"
        "  font-style:normal;\n"
        "}"
    )


def _rich_section(info: FontInfo, root: Path, text: str) -> str:
    family = f"font_{info.index:04d}"
    fname = html.escape(info.path.name)
    try:
        rel = info.path.relative_to(root)
    except ValueError:
        rel = info.path
    rel_s = html.escape(str(rel))
    body = html.escape(text)
    size_s = format_size(info.size)
    return (
        "\n<section>\n"
        f"<h1 style=\"font-family:'{family}',serif;\">\n"
        f"    {fname}\n"
        f"<small>({info.format})</small>\n"
        "</h1>\n"
        "<textarea\n"
        f"    style=\"font-family:'{family}',serif; font-size:22px;\"\n"
        '    spellcheck="false"\n'
        '    placeholder="Type to test font..."\n'
        f">{body}</textarea>\n"
        '<div class="metadata">\n'
        '<div class="metadata-item">\n'
        '<span class="metadata-label">Path:</span>\n'
        f"<code>{rel_s}</code>\n"
        "</div>\n"
        '<div class="metadata-item">\n'
        '<span class="metadata-label">Size:</span>\n'
        f"<code>{size_s}</code>\n"
        "</div>\n"
        '<div class="metadata-item">\n'
        '<span class="metadata-label">Format:</span>\n'
        f"<code>{info.format}</code>\n"
        "</div>\n"
        "</div>\n"
        "</section>"
    )


_RICH_CSS = """
:root {
  --bg: #ffffff;
  --text: #1a1a1a;
  --border: #e0e0e0;
  --accent: #0066cc;
  --input-bg: #f9f9f9;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1a1a;
    --text: #e8e8e8;
    --border: #333333;
    --accent: #66b3ff;
    --input-bg: #262626;
  }
}
* { box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, sans-serif;
  margin: 0 auto;
  padding: 20px;
  max-width: 960px;
  transition: background 0.3s, color 0.3s;
}
h1 {
  margin-top: 40px;
  margin-bottom: 0.5em;
  font-size: 1.6em;
  border-bottom: 2px solid var(--border);
  padding-bottom: 0.3em;
  color: var(--accent);
  word-break: break-word;
}
h1 small {
  font-size: 0.6em;
  opacity: 0.7;
  display: inline-block;
  margin-left: 0.5em;
}
textarea {
  width: 100%;
  min-height: 100px;
  padding: 16px;
  margin-top: 6px;
  border-radius: 8px;
  border: 2px solid var(--border);
  font-size: clamp(1em, 2vw, 1.5em);
  resize: vertical;
  white-space: pre-wrap;
  background: var(--input-bg);
  color: var(--text);
  transition: border-color 0.3s;
  line-height: 1.6;
  overflow-wrap: break-word;
}
textarea:focus { outline: none; border-color: var(--accent); }
section {
  margin-top: 30px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
}
section:last-of-type { border-bottom: none; }
.note {
  color: var(--text);
  margin-top: 8px;
  font-size: 0.85em;
  font-family: monospace;
  word-break: break-all;
  opacity: 0.75;
}
footer {
  margin-top: 40px;
  text-align: center;
  color: var(--text);
  font-size: 0.9em;
  opacity: 0.6;
}
.metadata {
  display: flex;
  gap: 16px;
  font-size: 0.9em;
  margin-top: 8px;
  flex-wrap: wrap;
}
.metadata-item { display: flex; align-items: center; gap: 4px; }
.metadata-label { opacity: 0.7; font-weight: 500; }
""".strip()


def _generate_rich_html(infos: Sequence[FontInfo], root: Path, text: str) -> str:
    timestamp = datetime.now().isoformat(timespec="seconds")
    head = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        "<title>Font Preview</title>\n"
        "<style>\n"
        f"{_RICH_CSS}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Font Preview Generator</h1>\n"
        f'<p class="note">Generated: {timestamp}</p>\n'
        "<style>\n"
    )
    if not infos:
        return head + "\n</style>\n" + _rich_footer()
    faces = "\n".join(_rich_font_face(f, root) for f in infos)
    sections = "\n".join(_rich_section(f, root, text) for f in infos)
    return head + faces + "\n</style>\n" + sections + _rich_footer()


def _rich_footer() -> str:
    return (
        "\n<footer>\n"
        "Generated by Font Preview Generator | Python 3.12+ | Pathlib optimized\n"
        "</footer>\n"
        "</body>\n"
        "</html>\n"
    )


def _atomic_write(text: str, dest: Path) -> bool:
    try:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)
        return True
    except OSError as e:
        LOG.error("Failed to write {}: {}", dest, e)
        return False


def cmd_rich(args: argparse.Namespace) -> int:
    root = Path.cwd()
    paths = _parse_paths(args.paths)
    LOG.info("Searching for fonts in {} location(s)...", len(paths))
    infos = _collect_font_infos(paths, max_fonts=args.max_fonts)
    if not infos:
        LOG.warning("No font files found. Supported formats: {}", ", ".join(sorted(FONT_EXTS)))
        return 1
    LOG.info("Found {} font(s)", len(infos))
    LOG.info("Generating preview HTML...")
    doc = _generate_rich_html(infos, root, RICH_TEXT_DEFAULT)
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    LOG.info("Writing to {}...", output)
    if _atomic_write(doc, output):
        LOG.info("Successfully generated {}", output)
        LOG.info("  File size: {}", format_size(output.stat().st_size))
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fontpreview.py",
        description="Generate HTML previews for font files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples", 1)[-1] if "Examples" in __doc__ else None,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "simple",
        help="Simple one-font-per-block preview (fafontpreview.py / fontpreview.py)",
    )
    p.add_argument("paths", nargs="*", help="Files/directories to scan (default: cwd)")
    p.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="fa",
        help="Preset text/sizes/output (default: fa)",
    )
    p.add_argument("--text", default=None, help="Override the sample text (defaults to preset's)")
    p.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=None,
        help="Override font sizes in px (defaults to preset's)",
    )
    p.add_argument("-o", "--output", default=None, help="Output HTML file (defaults to preset's)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_simple)
    p = sub.add_parser(
        "rich",
        help="Rich interactive preview with textareas + dark mode (fontpre.py)",
    )
    p.add_argument("paths", nargs="*", help="Files/directories to scan (default: cwd)")
    p.add_argument(
        "-o",
        "--output",
        default=RICH_OUTPUT_DEFAULT,
        help=f"Output HTML file (default: {RICH_OUTPUT_DEFAULT})",
    )
    p.add_argument(
        "--max-fonts",
        type=int,
        default=RICH_MAX_FONTS_DEFAULT,
        help=f"Maximum fonts to include (default: {RICH_MAX_FONTS_DEFAULT})",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_rich)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        LOG.warning("Operation cancelled by user")
        return 130
    except Exception as e:
        LOG.opt(exception=True).error(
            "Unexpected error: {}",
            e,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
