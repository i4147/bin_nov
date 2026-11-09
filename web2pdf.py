#!/data/data/com.termux/files/usr/bin/env python
"""Convert web pages or local HTML files to PDF (WeasyPrint backend)."""

import argparse
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from typing import Any

from rich.console import Console
from weasyprint import CSS, HTML


console = Console(stderr=True)
PAPER: dict[str, tuple[float, float]] = {
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "Letter": (216, 279),
    "Legal": (216, 356),
    "Tabloid": (279, 432),
}
UA: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def is_url(s: str) -> bool:
    p = urlparse(s)
    return p.scheme in {"http", "https", "file", "data"} and bool(p.netloc or p.scheme == "file")


def to_target(raw: str) -> str:
    if is_url(raw):
        return raw
    path = Path(raw).expanduser()
    if path.exists():
        return path.resolve().as_uri()
    return f"https://{raw}"


def fetch(url: str, timeout: float, user_agent: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _content(s: str) -> str:
    parts, buf, i = [], "", 0
    while i < len(s):
        if s.startswith("{page}", i):
            parts.append(f'"{_esc(buf)}"')
            parts.append("counter(page)")
            buf, i = "", i + 6
        elif s.startswith("{pages}", i):
            parts.append(f'"{_esc(buf)}"')
            parts.append("counter(pages)")
            buf, i = "", i + 7
        else:
            buf += s[i]
            i += 1
    if buf:
        parts.append(f'"{_esc(buf)}"')
    return " ".join(parts) or '""'


def build_css(
    paper: str,
    landscape: bool,
    margin_mm: float,
    header: str | None,
    footer: str | None,
    header_size: float,
    footer_size: float,
    no_background: bool,
) -> str:
    w, h = PAPER[paper]
    if landscape:
        w, h = h, w
    rules = [
        "@page {",
        f"  size: {w}mm {h}mm;",
        f"  margin: {margin_mm}mm;",
    ]
    if header:
        rules += [
            "  @top-center {",
            f"    content: {_content(header)};",
            f"    font-size: {header_size}pt;",
            "    color: #555;",
            "    font-family: sans-serif;",
            "  }",
        ]
    if footer:
        rules += [
            "  @bottom-center {",
            f"    content: {_content(footer)};",
            f"    font-size: {footer_size}pt;",
            "    color: #555;",
            "    font-family: sans-serif;",
            "  }",
        ]
    rules.append("}")
    if no_background:
        rules.append("*, *::before, *::after { background: transparent !important; }")
    rules += [
        "img, svg, video { max-width: 100% !important; height: auto !important; }",
        "pre, code { white-space: pre-wrap !important; word-wrap: break-word !important; }",
        "table { max-width: 100% !important; }",
    ]
    return "\n".join(rules)


def default_output(target: str) -> Path:
    if target.startswith("file://"):
        return Path(urlparse(target).path).with_suffix(".pdf")
    host = urlparse(target).netloc or urlparse(target).path
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in host).strip("_")
    return Path(f"{safe or 'output'}.pdf")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web2pdf",
        description="Convert web pages or local HTML files to PDF (WeasyPrint backend).",
    )
    p.add_argument("input", help="URL, local .html file, or bare domain")
    p.add_argument("-o", "--output", type=Path, help="Output PDF path")
    p.add_argument("-p", "--paper", choices=sorted(PAPER), default="A4")
    p.add_argument("-L", "--landscape", action="store_true")
    p.add_argument("-m", "--margin", type=float, default=12.0, help="Margin in mm (default 12)")
    p.add_argument("--no-background", action="store_true", help="Drop CSS backgrounds")
    p.add_argument("--header", help="Header text. Supports {page} and {pages}.")
    p.add_argument("--footer", help="Footer text. Supports {page} and {pages}.")
    p.add_argument("--header-size", type=float, default=9.0)
    p.add_argument("--footer-size", type=float, default=9.0)
    p.add_argument("--user-agent", default=UA)
    p.add_argument("--timeout", type=float, default=30.0, help="Fetch timeout in seconds")
    p.add_argument("--css", type=Path, action="append", default=[], help="Extra CSS file(s)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.margin < 0:
        console.print("[red]error:[/red] --margin must be ≥ 0")
        return 2
    target = to_target(args.input)
    output = (args.output or default_output(target)).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    page_css = build_css(
        args.paper,
        args.landscape,
        args.margin,
        args.header,
        args.footer,
        args.header_size,
        args.footer_size,
        args.no_background,
    )
    stylesheets = [CSS(string=page_css)]
    for css_path in args.css:
        stylesheets.append(CSS(filename=str(css_path)))
    try:
        console.print(f"[cyan]→[/cyan] loading {target}")
        if target.startswith(("http://", "https://")):
            html = fetch(target, args.timeout, args.user_agent)
            doc = HTML(string=html, base_url=target)
        else:
            doc = HTML(filename=(urlparse(target).path if target.startswith("file://") else target))
        console.print(f"[cyan]→[/cyan] rendering with WeasyPrint")
        doc.write_pdf(target=str(output), stylesheets=stylesheets)
    except KeyboardInterrupt:
        console.print("[yellow]aborted[/yellow]")
        return 130
    except Exception as e:
        console.print(f"[red]render failed:[/red] {type(e).__name__}: {e}")
        return 1
    size_kb = output.stat().st_size / 1024
    console.print(f"[green]✓[/green] {output}  [dim]({size_kb:.1f} KB)[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
