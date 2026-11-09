#!/data/data/com.termux/files/usr/bin/env python
"""Unified document-to-PDF converter (HTML, Markdown, CHM, SVG, dictionaries)."""

import argparse
import base64
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Optional, Sequence


def die(msg: str, code: int = 1) -> "None":
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(msg)


def require_input(path: str | Path, *, check_suffix: Optional[str] = None) -> Path:
    p = Path(path)
    if not p.exists():
        die(f"input file '{p}' does not exist")
    if check_suffix and p.suffix.lower() != check_suffix:
        die(f"input file '{p}' is not {check_suffix}")
    return p


def default_pdf_path(input_path: Path) -> Path:
    return input_path.with_suffix(".pdf")


def resolve_output(input_path: Path, output: Optional[str | Path]) -> Path:
    return Path(output) if output else default_pdf_path(Path(input_path))


def render_pdf_from_html(
    html_text: str,
    output: Path,
    *,
    css_files: Sequence[str | Path] = (),
    css_strings: Sequence[str] = (),
    base_url: Optional[str] = None,
) -> None:
    from weasyprint import CSS, HTML

    sheets: list[Any] = []
    for f in css_files:
        sheets.append(CSS(filename=str(f)))
    for s in css_strings:
        sheets.append(CSS(string=s))
    doc = HTML(string=html_text, base_url=base_url)
    doc.write_pdf(str(output), stylesheets=sheets)


_INLINE_CSS_TEMPLATE: str = """
@page {
    size: A4;
    margin: 20mm;
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: {font_sans};
        font-size: 9pt;
        color: #666;
    }
}
h1,h2,h3,h4,h5,h6 { page-break-after: avoid; break-after: avoid; }
blockquote,pre,table,figure { page-break-inside: avoid; break-inside: avoid; }
ul,ol { page-break-inside: auto; }
li { page-break-inside: avoid; break-inside: avoid; }
html,body {
    font-family: {font_sans};
    font-size: 11pt;
    line-height: 1.6;
    color: #222;
}
p { margin-top: 0; margin-bottom: 1.2em; text-align: justify; }
h1 { font-size: 24pt; margin: 0 0 15pt 0; color: #111;
     border-bottom: 2px solid #ddd; padding-bottom: 5pt; }
h2 { font-size: 18pt; margin: 24pt 0 12pt 0; color: #222;
     border-bottom: 1px solid #eee; padding-bottom: 3pt; }
h3 { font-size: 14pt; margin: 18pt 0 8pt 0; color: #333; }
a { color: #0066cc; text-decoration: none; }
a[href^="http"]:after {
    content: " (" attr(href) ")"; font-size: 9pt; color: #888;
}
strong { color: #000; }
code {
    font-family: {font_mono};
    font-size: 10pt;
    background-color: #f5f5f5;
    padding: 2px 4px;
    border-radius: 3px;
    color: #c7254e;
}
blockquote {
    margin: 1.5em 0;
    padding: 0.5em 15px;
    border-left: 4px solid #ddd;
    color: #555;
    background-color: #f9f9f9;
    font-style: italic;
}
pre {
    background-color: #f5f5f5;
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 12px;
    margin: 1.5em 0;
    overflow: hidden;
}
pre code {
    background-color: transparent; padding: 0; border-radius: 0;
    color: #333; font-size: 9.5pt; white-space: pre-wrap;
}
table { width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 10.5pt; }
th,td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
th { background-color: #f0f0f0; font-weight: bold; color: #222; }
tr:nth-child(even) { background-color: #fafafa; }
ul,ol { margin-top: 0; margin-bottom: 1.5em; padding-left: 24px; }
li { margin-bottom: 0.4em; }
img { max-width: 100%; height: auto; display: block;
      margin: 20px auto; border-radius: 4px; }
"""
_LOCAL_FONT_FACES: str = """\
@font-face {
    font-family: "LocalInter";
    src: url("fonts/Inter-Regular.ttf");
    font-weight: normal; font-style: normal;
}
@font-face {
    font-family: "LocalInter";
    src: url("fonts/Inter-Bold.ttf");
    font-weight: bold; font-style: normal;
}
@font-face {
    font-family: "LocalMono";
    src: url("fonts/JetBrainsMono-Regular.ttf");
    font-weight: normal; font-style: normal;
}
"""


def inline_css(mode: str) -> str:
    if mode == "local-fonts":
        return _LOCAL_FONT_FACES + _INLINE_CSS_TEMPLATE.format(
            font_sans='"LocalInter",sans-serif',
            font_mono='"LocalMono",monospace',
        )
    return _INLINE_CSS_TEMPLATE.format(
        font_sans='"Helvetica Neue",Helvetica,Arial,sans-serif',
        font_mono='"Courier New",Courier,monospace',
    )


def cmd_svg(args: argparse.Namespace) -> int:
    inp = require_input(args.input, check_suffix=".svg")
    out = resolve_output(inp, args.output)
    try:
        import cairosvg
    except ImportError:
        die("cairosvg is not installed (pip install cairosvg)")
    try:
        cairosvg.svg2pdf(url=str(inp), write_to=str(out))
    except Exception as exc:
        die(f"cairosvg failed: {exc}")
    info(f"PDF created: {out}")
    return 0


class _CHMHtmlExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "meta", "link", "iframe"}

    def __init__(self) -> None:
        super().__init__()
        self.content: list[str] = []
        self.in_body = False
        self.current_skip_tag: Optional[str] = None

    def _attrs(self, attrs: Sequence[tuple[str, Optional[str]]]) -> str:
        return "".join(f' {k}="{v}"' for k, v in attrs if k not in ("href", "src"))

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP_TAGS:
            self.current_skip_tag = tag
        elif tag == "body":
            self.in_body = True
        elif not self.current_skip_tag and self.in_body:
            self.content.append(f"<{tag}{self._attrs(attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == self.current_skip_tag:
            self.current_skip_tag = None
        elif tag == "body":
            self.in_body = False
        elif not self.current_skip_tag and self.in_body:
            self.content.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.current_skip_tag and self.in_body:
            self.content.append(data)

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag not in self.SKIP_TAGS and self.in_body:
            self.content.append(f"<{tag}{self._attrs(attrs)}/>")

    def get_content(self) -> str:
        return "".join(self.content)


_CHM_STYLE_HEADER: str = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    "<style>"
    "body { font-family:Arial,sans-serif; line-height:1.6; margin:2em; }"
    "img { max-width:100%; }"
    "h1,h2,h3,h4 { color:#333; }"
    "pre { background-color:#f5f5f5; padding:1em; border-radius:4px; }"
    "code { background-color:#f5f5f5; padding:0.2em 0.4em; border-radius:3px; }"
    "</style></head><body>"
)


def _clean_topic_html(raw: str) -> str:
    if not raw:
        return ""
    raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    ext = _CHMHtmlExtractor()
    try:
        ext.feed(raw)
        out = ext.get_content()
        out = re.sub(r"\n\s*\n", "\n\n", out)
        return out.strip()
    except Exception as exc:
        warn(f"HTML parsing failed: {exc}")
        return raw


def _chm_weasyprint_extract(chm_path: Path) -> str:
    try:
        import chm.chm as pychm
    except ImportError:
        die("pychm is required (pip install pychm); provides chm.chm")
    cf = pychm.CHMFile()
    if not cf.LoadCHM(str(chm_path)):
        msg = f"failed to load CHM file: {chm_path}"
        raise RuntimeError(msg)
    try:
        tree = cf.GetTopicsTree()
        if not tree:
            default = cf.GetDefaultTopic()
            if default:
                return _render_one_topic(cf, default)
            files = cf.GetAllFiles()
            htmls = [f for f in files if f.lower().endswith((".html", ".htm"))]
            if htmls:
                return _render_multiple_topics(cf, htmls)
            msg = "no HTML content found in CHM file"
            raise RuntimeError(msg)
        parts = [_CHM_STYLE_HEADER]

        def walk(node: Any, depth: int = 0) -> None:
            if hasattr(node, "GetTitle") and hasattr(node, "GetLocal"):
                title = node.GetTitle()
                local = node.GetLocal()
                if title and local:
                    lvl = min(depth + 1, 6)
                    parts.append(f"<h{lvl}>{_escape(title)}</h{lvl}>")
                    try:
                        raw = cf.RetrieveObject(cf.ResolveObject(local))
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="ignore")
                        body = _clean_topic_html(raw)
                        if body:
                            parts.append(body)
                    except Exception as exc:
                        warn(f"could not extract topic {local}: {exc}")
                    parts.append('<hr style="border:1px solid #ccc; margin:20px 0;">')
            if hasattr(node, "GetChildren"):
                for child in node.GetChildren():
                    walk(child, depth + 1)

        if isinstance(tree, list):
            for node in tree:
                walk(node)
        else:
            walk(tree)
        parts.append("</body></html>")
        return "".join(parts)
    finally:
        cf.CloseCHM()


def _render_one_topic(cf: Any, topic: str) -> str:
    try:
        raw = cf.RetrieveObject(cf.ResolveObject(topic))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return _CHM_STYLE_HEADER + _clean_topic_html(raw) + "</body></html>"
    except Exception as exc:
        warn(f"could not extract topic {topic}: {exc}")
        return ""


def _render_multiple_topics(cf: Any, topics: Iterable[str]) -> str:
    parts = [_CHM_STYLE_HEADER]
    for t in topics:
        try:
            raw = cf.RetrieveObject(cf.ResolveObject(t))
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            body = _clean_topic_html(raw)
            if body:
                parts.append(body)
                parts.append('<hr style="border:1px solid #ccc; margin:20px 0;">')
        except Exception as exc:
            warn(f"could not extract topic {t}: {exc}")
    parts.append("</body></html>")
    return "".join(parts)


def _escape(s: str) -> str:
    from html import escape

    return escape(s)


def _strip_html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    s = raw_html
    s = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = s.replace("</p>", "\n\n").replace("<p>", "")
    for lvl in ("h1", "h2", "h3", "h4"):
        s = s.replace(f"</{lvl}>", "\n\n").replace(f"<{lvl}>", "")
    s = s.replace("</li>", "\n").replace("<li>", "• ")
    s = s.replace("</div>", "\n").replace("<div>", "")
    s = s.replace("</span>", "").replace("<span>", "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n\s*\n", "\n\n", s)
    return s.strip()


def _chm_reportlab_convert(chm_path: Path, output: Path) -> None:
    try:
        import chm
    except ImportError:
        die("the 'chm' package is required for --backend reportlab")
    try:
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        die("reportlab is required for --backend reportlab")
    chm_obj = chm.CHMFile(str(chm_path))
    topics: list[str] = []
    if hasattr(chm_obj, "get_toc"):
        try:
            toc = chm_obj.get_toc()
            if toc:
                topics = _flatten_toc(toc)
        except Exception as exc:
            warn(f"get_toc failed: {exc}")
    if not topics and hasattr(chm_obj, "list"):
        try:
            topics = [f for f in chm_obj.list() if str(f).lower().endswith((".html", ".htm"))]
        except Exception as exc:
            warn(f"list failed: {exc}")
    if not topics:
        die("no HTML topics found in CHM")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CHMTitle",
        parent=styles["Heading1"],
        fontSize=24,
        textColor="darkblue",
        alignment=TA_CENTER,
        spaceAfter=30,
    )
    story: list[Any] = [
        Paragraph(f"<b>{chm_path.stem}</b>", title_style),
        Spacer(1, 0.25 * inch),
    ]
    processed = 0
    for i, topic in enumerate(topics):
        try:
            raw = chm_obj.get_obj(topic)
        except Exception:
            raw = None
        if raw is None:
            continue
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", errors="ignore")
            except Exception:
                raw = "[Binary or encoded content]"
        text = _strip_html_to_text(raw)
        if not text:
            continue
        if i > 0:
            story.append(PageBreak())
        heading = Path(str(topic)).stem.replace("_", " ").replace("-", " ")
        if heading:
            story.append(Paragraph(f"<b>{heading}</b>", styles["Heading2"]))
            story.append(Spacer(1, 0.1 * inch))
        for chunk in text.split("\n\n"):
            chunk = chunk.replace("\n", " ").strip()
            if not chunk:
                continue
            try:
                story.append(Paragraph(chunk, styles["Normal"]))
            except Exception:
                story.append(Paragraph(chunk[:1000], styles["Normal"]))
            story.append(Spacer(1, 0.05 * inch))
        processed += 1
    if processed == 0:
        die("no content could be extracted from the CHM file")
    doc = SimpleDocTemplate(
        str(output),
        pagesize=letter,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=72,
    )
    doc.build(story)
    info(f"PDF created: {output}")


def _flatten_toc(toc: Any) -> list[str]:
    out: list[str] = []
    if isinstance(toc, list):
        for item in toc:
            if isinstance(item, dict):
                if "path" in item:
                    out.append(item["path"])
                if "children" in item:
                    out.extend(_flatten_toc(item["children"]))
            elif isinstance(item, str):
                out.append(item)
    return out


def cmd_chm(args: argparse.Namespace) -> int:
    inp = require_input(args.input, check_suffix=".chm")
    out = resolve_output(inp, args.output)
    if args.backend == "reportlab":
        _chm_reportlab_convert(inp, out)
        return 0
    try:
        html_text = _chm_weasyprint_extract(inp)
    except Exception as exc:
        die(f"CHM extraction failed: {exc}")
    if not html_text:
        die("no content extracted from CHM file")
    wrapped = (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        "<style>\n"
        "@page { size: A4; margin: 2cm; "
        "@bottom-center { content: counter(page); font-size:10px; } }\n"
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',"
        " Roboto, Arial, sans-serif; line-height: 1.6; font-size: 11pt; }\n"
        "h1 { font-size: 24pt; margin-top: 30px; }\n"
        "h2 { font-size: 20pt; margin-top: 25px; }\n"
        "h3 { font-size: 16pt; margin-top: 20px; }\n"
        "h4 { font-size: 14pt; margin-top: 15px; }\n"
        "img { max-width: 100%; height: auto; margin: 10px 0; }\n"
        "pre { background-color:#f5f5f5; border:1px solid #ccc; "
        "border-radius:4px; padding:15px; font-family:'Courier New',"
        "monospace; font-size:9pt; }\n"
        "code { background-color:#f5f5f5; padding:2px 4px; "
        "border-radius:3px; font-family:'Courier New',monospace; "
        "font-size:9pt; }\n"
        "table { border-collapse: collapse; width: 100%; margin: 15px 0; }\n"
        "th,td { border: 1px solid #ccc; padding: 8px; text-align: left; }\n"
        "th { background-color: #f0f0f0; font-weight: bold; }\n"
        "blockquote { border-left:4px solid #ddd; margin:15px 0; "
        "padding:10px 20px; background-color:#f9f9f9; }\n"
        "</style>\n</head>\n<body>\n"
        f"{html_text}\n</body>\n</html>"
    )
    try:
        render_pdf_from_html(wrapped, out)
    except Exception as exc:
        die(f"WeasyPrint failed: {exc}")
    info(f"PDF created: {out}")
    return 0


def cmd_html(args: argparse.Namespace) -> int:
    inp = require_input(args.input)
    out = resolve_output(inp, args.output)
    html_text = inp.read_text(encoding="utf-8")
    css_files: list[str] = list(args.css)
    try:
        render_pdf_from_html(
            html_text,
            out,
            css_files=css_files,
            base_url=str(inp.parent),
        )
    except Exception as exc:
        die(f"WeasyPrint failed: {exc}")
    info(f"PDF created: {out}")
    return 0


_MARKDOWN2_SIMPLE_EXTRAS: list[str] = ["cuddled-lists", "tables"]
_MARKDOWN2_FULL_EXTRAS: list[str] = [
    "header-ids",
    "fenced-code-blocks",
    "tables",
    "cuddled-lists",
]
_PYGMENTS_RE = re.compile(
    r'<pre><code class="language-(\w+)">(.*?)</code></pre>',
    re.DOTALL,
)
_TOC_NAV: str = '\n<nav class="toc">\n<h1>Contents</h1>\n<ul></ul>\n</nav>\n'


def _convert_markdown2(
    text: str,
    *,
    full_extras: bool,
    pygments: bool,
    toc: bool,
) -> str:
    from markdown2 import markdown

    extras = _MARKDOWN2_FULL_EXTRAS if full_extras else _MARKDOWN2_SIMPLE_EXTRAS
    html_text = markdown(text, extras=extras)
    if pygments:
        try:
            from pygments import highlight
            from pygments.formatters import HtmlFormatter
            from pygments.lexers import TextLexer, get_lexer_by_name
        except ImportError:
            warn("pygments not installed — skipping code highlighting")
            pygments = False
    if pygments:
        formatter = HtmlFormatter(cssclass="highlight")

        def repl(m: re.Match[str]) -> str:
            lang, code = m.group(1), m.group(2)
            code = code.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            try:
                lexer = get_lexer_by_name(lang)
            except Exception:
                lexer = TextLexer()
            return highlight(code, lexer, formatter)

        html_text = _PYGMENTS_RE.sub(repl, html_text)
    if toc:
        html_text = _TOC_NAV + html_text
    return html_text


def _convert_markdown(text: str) -> str:
    try:
        import markdown
    except ImportError:
        die("the 'markdown' package is required for --converter markdown")
    return markdown.markdown(text, extensions=["extra", "codehilite"])


def _wrap_md_html(body_html: str, title: str) -> str:
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        f"<title>{_escape(title)}</title>\n</head>\n<body>\n"
        f"{body_html}\n</body>\n</html>"
    )


def cmd_markdown(args: argparse.Namespace) -> int:
    inp = require_input(args.input)
    out = resolve_output(inp, args.output)
    text = inp.read_text(encoding="utf-8")
    if args.converter == "markdown":
        body_html = _convert_markdown(text)
        inline = args.inline_css or "default"
        css_strings = [inline_css(inline)] if inline != "none" else []
        css_files: list[str] = list(args.css)
    else:
        full_extras = bool(args.pygments or args.toc)
        body_html = _convert_markdown2(
            text,
            full_extras=full_extras,
            pygments=bool(args.pygments),
            toc=bool(args.toc),
        )
        css_files = list(args.css)
        css_strings = [inline_css(args.inline_css)] if args.inline_css and args.inline_css != "none" else []
    if not body_html.strip():
        die("converted markdown produced empty HTML")
    html_text = _wrap_md_html(body_html, inp.stem)
    try:
        render_pdf_from_html(
            html_text,
            out,
            css_files=css_files,
            css_strings=css_strings,
            base_url=str(inp.parent),
        )
    except Exception as exc:
        die(f"WeasyPrint failed: {exc}")
    info(f"PDF created: {out}")
    return 0


_DICT_TAG_RX = re.compile(r"</?[a-zA-Z][^>]*>")
_DICT_X_RX = re.compile(r"<x [^>]*>")
_DICT_M_RX = re.compile(r"<[A-Z]\s+M=\"[^\"]+\"\s*/?>")


def _format_dictionary_entry(line: str) -> Optional[str]:
    try:
        word, definition = line.strip().split("\t", 1)
    except ValueError:
        return None
    definition = definition.replace("<br/>", "<br>")
    definition = _DICT_TAG_RX.sub("", definition)
    definition = _DICT_X_RX.sub("<span>", definition)
    definition = definition.replace("</x>", "</span>")
    definition = _DICT_M_RX.sub("", definition)
    return (
        '\n<html>\n<body>\n<div class="entry">\n'
        f'<h1 class="word">{_escape(word)}</h1>\n'
        f'<div class="definition">{definition}</div>\n'
        "</div>\n</body>\n</html>\n"
    )


def cmd_dict(args: argparse.Namespace) -> int:
    inp = require_input(args.input)
    out = resolve_output(inp, args.output)
    font = Path(args.font)
    if not font.exists():
        warn(f"font '{font}' not found — PDF may use fallback typography")
    entries: list[str] = []
    for line in inp.read_text(encoding="utf-8").splitlines():
        block = _format_dictionary_entry(line)
        if block:
            entries.append(block)
    if not entries:
        die("no valid dictionary entries found")
    css = (
        "<style>\n"
        f"@font-face {{ font-family:'CustomFont'; src:url('{font}'); }}\n"
        "body { font-family:'CustomFont',sans-serif; font-size:16px; }\n"
        ".entry { page-break-after:always; padding:30px; }\n"
        ".word { margin-top:0; color:#111; }\n"
        ".definition { margin-top:10px; line-height:1.5; }\n"
        "</style>"
    )
    html_text = (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        f"{css}\n</head>\n<body>\n{''.join(entries)}\n</body></html>"
    )
    try:
        render_pdf_from_html(
            html_text,
            out,
            base_url=str(inp.parent),
        )
    except Exception as exc:
        die(f"WeasyPrint failed: {exc}")
    info(f"PDF created: {out}")
    return 0


_PRECISE_FONTS: list[tuple[Any, ...]] = [
    ("Inter", "normal", 400, "Inter-Regular.ttf"),
    ("Inter", "normal", 700, "Inter-Bold.ttf"),
    ("Inter", "italic", 400, "Inter-Italic.ttf"),
    ("Inter", "italic", 700, "Inter-BoldItalic.ttf"),
    ("JetBrains Mono", "normal", 400, "JetBrainsMono-Regular.ttf"),
]


def _font_face(font_dir: Path, family: str, style: str, weight: int, filename: str) -> str:
    p = font_dir / filename
    if not p.exists():
        warn(f"'{filename}' not found in {font_dir} — skipping")
        return ""
    data = base64.b64encode(p.read_bytes()).decode("utf-8")
    return (
        "@font-face {\n"
        f"    font-family: '{family}';\n"
        f"    font-style: {style};\n"
        f"    font-weight: {weight};\n"
        "    src: url(data:font/truetype;charset=utf-8;base64,"
        f"{data}) format('truetype');\n"
        "}"
    )


_PRECISE_CSS_BODY: str = """
/* Global Reset & Base Typography */
html, body {
    margin: 0; padding: 0;
    font-family: 'Inter', -apple-system, sans-serif;
    font-size: 10.5pt; font-weight: 400; font-style: normal;
    line-height: 1.6; color: #222;
    -webkit-print-color-adjust: exact;
}
h1, h2, h3, h4 {
    font-family: 'Inter', sans-serif; font-weight: 700;
    color: #111; margin-top: 0;
    page-break-after: avoid; break-after: avoid;
}
h1 { font-size: 26pt; line-height:1.15; margin-bottom:20pt; letter-spacing:-0.02em; }
h2 { font-size: 18pt; line-height:1.25; margin-top:24pt; margin-bottom:12pt;
     border-bottom: 0.75pt solid #ccc; padding-bottom: 6pt; }
h3 { font-size: 14pt; line-height:1.35; margin-top:18pt; margin-bottom:8pt; }
p  { margin-top: 0; margin-bottom: 10pt; text-align: justify; }
code, pre, kbd, samp {
    font-family: 'JetBrains Mono', monospace; font-size: 9pt;
    direction: ltr; text-align: left; white-space: pre-wrap;
}
pre {
    background-color: #f5f5f5; border: 0.5pt solid #ddd;
    border-radius: 4px; padding: 10pt 12pt; margin: 12pt 0;
    page-break-inside: avoid; break-inside: avoid;
}
p code { background-color: #f0f0f0; padding: 2pt 4pt; border-radius:3px; color:#c7254e; }
em, i { font-style: italic; font-weight: 400; }
strong, b { font-weight: 700; font-style: normal; }
strong em, em strong, b i, i b { font-weight: 700; font-style: italic; }
@page {
    size: A4 portrait; margin: 25mm 20mm 20mm 20mm;
    @top-left { content: "Official Document Title";
                font-family:'Inter',sans-serif; font-size:8.5pt; color:#666;
                border-bottom:0.5pt solid #ddd; padding-bottom:4pt; }
    @top-right { content: "Confidential";
                 font-family:'Inter',sans-serif; font-weight:700;
                 font-size:8.5pt; color:#666;
                 border-bottom:0.5pt solid #ddd; padding-bottom:4pt; }
    @bottom-left { content: "Generated Document";
                   font-family:'Inter',sans-serif; font-size:8pt; color:#666; }
    @bottom-right { content: "Page " counter(page) " of " counter(pages);
                    font-family:'Inter',sans-serif; font-size:8.5pt; color:#666; }
}
@page :first {
    margin-top: 20mm;
    @top-left  { content: normal; border-bottom: none; }
    @top-right { content: normal; border-bottom: none; }
}
.page-break { page-break-before: always; break-before: always; }
table { width: 100%; border-collapse: collapse; margin-bottom: 16pt; }
tr { page-break-inside: avoid; break-inside: avoid; }
thead { display: table-header-group; }
th { background-color: #f0f0f0; color: #111; font-weight: 700;
     text-align: left; padding: 8pt 10pt; font-size: 9.5pt;
     border-bottom: 2pt solid #ccc; }
td { padding: 8pt 10pt; border-bottom: 1px solid #eee;
     font-size: 9.5pt; vertical-align: top; }
"""


def cmd_compile_css(args: argparse.Namespace) -> int:
    font_dir = Path(args.font_dir)
    faces = [
        _font_face(font_dir, family, style, weight, filename) for family, style, weight, filename in _PRECISE_FONTS
    ]
    faces_block = "\n\n".join(f for f in faces if f)
    css = (
        "/* =====================================================================\n"
        "   1. PRECISION FONT REGISTRATION (AUTOMATICALLY INLINED)\n"
        "   ===================================================================== */\n"
        f"{faces_block}\n"
        f"{_PRECISE_CSS_BODY}"
    )
    out = Path(args.output)
    out.write_text(css, encoding="utf-8")
    info(f"CSS written to: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdfkit.py",
        description="Unified document-to-PDF converter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    a = sub.add_parser("svg", help="SVG -> PDF (cairosvg).")
    a.add_argument("input", help="Input .svg file.")
    a.add_argument("-o", "--output", help="Output PDF (default: <stem>.pdf).")
    a.set_defaults(func=cmd_svg)
    b = sub.add_parser("chm", help="CHM -> PDF.")
    b.add_argument("input", help="Input .chm file.")
    b.add_argument(
        "-b",
        "--backend",
        choices=("weasyprint", "reportlab"),
        default="weasyprint",
        help="Rendering backend (default: weasyprint).",
    )
    b.add_argument("-o", "--output", help="Output PDF (default: <stem>.pdf).")
    b.set_defaults(func=cmd_chm)
    c = sub.add_parser("html", help="HTML -> PDF (WeasyPrint).")
    c.add_argument("input", help="Input .html file.")
    c.add_argument("--css", action="append", default=[], help="Extra CSS file (repeatable).")
    c.add_argument("-o", "--output", help="Output PDF (default: <stem>.pdf).")
    c.set_defaults(func=cmd_html)
    d = sub.add_parser("md", help="Markdown -> PDF.")
    d.add_argument("input", help="Input .md file.")
    d.add_argument(
        "--converter",
        choices=("markdown2", "markdown"),
        default="markdown2",
        help="Markdown library (default: markdown2, matches md2pdf.py).",
    )
    d.add_argument("--css", action="append", default=[], help="Extra CSS file (repeatable).")
    d.add_argument(
        "--pygments",
        action="store_true",
        help="Highlight fenced code blocks with Pygments (markdown2 converter only).",
    )
    d.add_argument(
        "--toc",
        action="store_true",
        help="Prepend a TOC nav block (markdown2 converter only).",
    )
    d.add_argument(
        "--inline-css",
        choices=("none", "default", "local-fonts"),
        default="none",
        help="Embed one of the pre-baked inline stylesheets (default: none).",
    )
    d.add_argument("-o", "--output", help="Output PDF (default: <stem>.pdf).")
    d.set_defaults(func=cmd_markdown)
    e = sub.add_parser("dict", help="Dictionary .txt -> PDF (WeasyPrint).")
    e.add_argument(
        "input",
        nargs="?",
        default="dictionary.txt",
        help="Input tab-separated dictionary file.",
    )
    e.add_argument("--font", default="custom.ttf", help="Font file to embed (default: custom.ttf).")
    e.add_argument("-o", "--output", help="Output PDF (default: <stem>.pdf).")
    e.set_defaults(func=cmd_dict)
    f = sub.add_parser("compile-css", help="Write a print-style CSS with embedded fonts.")
    f.add_argument(
        "output",
        nargs="?",
        default="print-style.css",
        help="Output CSS path (default: print-style.css).",
    )
    f.add_argument("--font-dir", default=".", help="Directory holding the TTF files (default: .).")
    f.set_defaults(func=cmd_compile_css)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
