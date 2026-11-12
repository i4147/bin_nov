#!/data/data/com.termux/files/usr/bin/env python
"""Generate HTML templates: blank pages, CodePen wrapping, multi-file merge.
Analysis / deduplication ------------------------ - ``htnew.py`` ("Hello, World!" page) and ``mkhtml.py`` (page linking ``style.css``/``script.js``; it crashed without an argument because ``Path(sys.argv[1]) or ...`` evaluates argv first) are one ``new`` command with ``--style minimal|linked``.
- ``codepener.py`` wraps a CodePen fragment in a full document; its f-string was line-broken inside the braces, so the file never parsed - the ``wrap`` command implements the same template.
- ``make_template_html.py`` merges every HTML file in a tree into one ``template.html`` (shared head tags + body sections + TOC); here the same job is done with the stdlib parser instead of BeautifulSoup, and the output never feeds back into itself.
Examples -------- python3 html_template_tool.py new # ./index.html python3 html_template_tool.py new page.html --title "Hi" --style linked python3 html_template_tool.py wrap pen.html -o full.html python3 html_template_tool.py merge site/ -o template.html
"""

import argparse
from collections.abc import Sequence
import html.parser
from pathlib import Path
import sys

PAGE_CSS: str = (
    "body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; "
    "padding: 20px; background: #f4f4f4; }\n"
    ".container { max-width: 1200px; margin: 0 auto; background: #fff; "
    "padding: 20px; box-shadow: 0 0 10px rgba(0,0,0,.1); }\n"
    ".merged-content { margin-bottom: 40px; padding: 20px; "
    "border-left: 4px solid #333; background: #fafafa; }"
)


def blank_page(title: str, style: str, lang: str) -> str:
    """A minimal HTML5 page, optionally linking style.css/script.js."""
    links: str = ""
    scripts: str = ""
    body: str = "    <h1>Hello, World!</h1>\n    <!-- Your content here -->"
    if style == "linked":
        links = '    <link rel="stylesheet" href="style.css">\n'
        scripts = '    <script src="script.js"></script>\n'
        body = "    <div>\n      <h2>Heading2</h2>\n    </div>"
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{lang}">\n<head>\n'
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{title}</title>\n{links}</head>\n<body>\n{body}\n{scripts}</body>\n</html>\n"
    )


def wrap_fragment(fragment: str, title: str, charset: str, css_href: str, js_src: str) -> str:
    """Wrap a CodePen-style body fragment in a complete document."""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'    <meta charset="{charset}">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{title}</title>\n"
        f'    <link rel="stylesheet" href="{css_href}">\n'
        "</head>\n<body>\n"
        f"{fragment.rstrip()}\n"
        f'    <script src="{js_src}"></script>\n'
        "</body>\n</html>\n"
    )


class BodyExtractor(html.parser.HTMLParser):
    """Collect <meta>/<link> head tags and the inner HTML of <body>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.head_tags: list[str] = []
        self._in_head: bool = False
        self._in_body: bool = False
        self._body: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw: str = self.get_starttag_text() or f"<{tag}>"
        if tag == "head":
            self._in_head = True
        elif tag == "body":
            self._in_body = True
        elif self._in_head and tag in ("meta", "link"):
            self.head_tags.append(raw)
        elif self._in_head and tag == "script" and dict(attrs).get("src"):
            self.head_tags.append(raw + "</script>")
        elif self._in_body:
            self._body.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw: str = self.get_starttag_text() or f"<{tag}/>"
        if self._in_head and tag in ("meta", "link"):
            self.head_tags.append(raw)
        elif self._in_body:
            self._body.append(raw)

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
        elif tag == "body":
            self._in_body = False
        elif self._in_body:
            self._body.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._in_body:
            self._body.append(data)

    @property
    def body_html(self) -> str:
        """Inner body HTML collected so far."""
        return "".join(self._body).strip()


def title_from_stem(path: Path) -> str:
    """``my-page.html`` -> ``My Page``."""
    return path.stem.replace("-", " ").replace("_", " ").title()


def cmd_new(args: argparse.Namespace) -> int:
    """Create a new blank HTML page."""
    if args.output.exists() and not args.force:
        print(f"Error: {args.output} exists (use --force).", file=sys.stderr)
        return 1
    args.output.write_text(blank_page(args.title, args.style, args.lang), encoding="utf-8")
    print(f"Created {args.output} (style={args.style}, title={args.title!r})")
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    """Wrap a fragment file in a full HTML document."""
    if not args.input.is_file():
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1
    title: str = args.title or title_from_stem(args.input)
    output: Path = args.output or args.input
    fragment: str = args.input.read_text(encoding="utf-8")
    output.write_text(wrap_fragment(fragment, title, args.charset, args.css, args.js), encoding="utf-8")
    print(f"Converted: {args.input} -> {output} (title={title!r})")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Merge all HTML files under a root into one template page."""
    output: Path = args.output.resolve()
    files: list[Path] = sorted(
        p
        for p in args.root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".html", ".htm") and p.resolve() != output
    )
    if not files:
        print("No HTML files found.")
        return 0
    head_tags: list[str] = []
    seen: set[str] = set()
    sections: list[str] = []
    toc: list[str] = []
    for path in files:
        extractor = BodyExtractor()
        try:
            extractor.feed(path.read_text(encoding="utf-8", errors="replace"))
            extractor.close()
        except Exception as exc:
            print(f"warning: skipping {path}: {exc}")
            continue
        for tag in extractor.head_tags:
            if tag not in seen:
                seen.add(tag)
                head_tags.append(tag)
        anchor: str = path.stem
        rel: object = path.relative_to(args.root) if args.root in path.parents else path
        toc.append(f'      <li><a href="#{anchor}">{rel}</a></li>')
        sections.append(
            f"    <!-- Content from: {rel} -->\n"
            f'    <section class="merged-content" id="{anchor}" data-source="{path.name}">\n'
            f"{extractor.body_html}\n    </section>"
        )
    indent_head: str = "\n".join("    " + tag for tag in head_tags)
    document: str = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{args.title}</title>\n{indent_head}\n"
        f"    <style>\n{PAGE_CSS}\n    </style>\n</head>\n<body>\n"
        '  <div class="container">\n    <nav class="toc"><h2>Contents</h2><ul>\n'
        + "\n".join(toc)
        + "\n    </ul></nav>\n"
        + "\n".join(sections)
        + "\n  </div>\n</body>\n</html>\n"
    )
    args.output.write_text(document, encoding="utf-8")
    print(f"Merged {len(files)} file(s) into {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="HTML template generation.")
    sub = parser.add_subparsers(dest="command", required=True)
    nw = sub.add_parser("new", help="Create a blank HTML page")
    nw.add_argument("output", nargs="?", type=Path, default=Path("index.html"))
    nw.add_argument("--title", default="Document")
    nw.add_argument(
        "--style",
        choices=("minimal", "linked"),
        default="minimal",
        help="minimal: Hello World; linked: style.css + script.js",
    )
    nw.add_argument("--lang", default="en")
    nw.add_argument("--force", action="store_true", help="Overwrite if it exists")
    nw.set_defaults(handler=cmd_new)
    wr = sub.add_parser("wrap", help="Wrap a CodePen fragment in a full document")
    wr.add_argument("input", type=Path)
    wr.add_argument("-o", "--output", type=Path, default=None, help="Default: rewrite input")
    wr.add_argument("--title", default=None, help="Default: derived from filename")
    wr.add_argument("--charset", default="UTF-8")
    wr.add_argument("--css", default="style.css")
    wr.add_argument("--js", default="script.js")
    wr.set_defaults(handler=cmd_wrap)
    me = sub.add_parser("merge", help="Merge many pages into one template")
    me.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    me.add_argument("-o", "--output", type=Path, default=Path("template.html"))
    me.add_argument("--title", default="Merged HTML Template")
    me.set_defaults(handler=cmd_merge)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
