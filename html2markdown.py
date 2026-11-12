#!/data/data/com.termux/files/usr/bin/env python
"""Convert HTML files to Markdown or plain text.
Subcommands: ``to-md`` converts with an external ``htm2md``, ``html_to_markdown``, pandoc or ``readability`` and can remove the originals, ``to-text`` strips the tags with the stdlib parser and ``fetch`` downloads a URL as text (optionally rendering the JS with pyppeteer and taking a screenshot).
Usage: html2markdown {to-md,to-text,fetch} [options] [paths ...] Positional arguments: paths ...
Files or dirs (default: .) url Subcommands: to-md Convert HTML files to Markdown to-text Strip HTML tags to plain text (stdlib) fetch Fetch a URL as text Options: --engine --command External converter (htm2md default) --main-content Readability-extract first (needs readability-lxml) --keep-forms Keep <form> (html_to_markdown engine) -r, --remove-original Delete HTML after success --no-recursive -o, --out-dir Output dir (default: beside source) -o, --output --render Render JS with pyppeteer --screenshot With --render, save a PNG here Examples: $ html2markdown to-md --main-content pages/ $ html2markdown fetch https://example.com -o page.txt Command-line script: run ``python html2markdown.py`` or the ``html2markdown`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import html.parser
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

from dh import MAX_WORKERS

HTML_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".xhtml", ".xhtm"})
SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "noscript", "template", "head"})
BLOCK_TAGS: frozenset[str] = frozenset({
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
})
BLANK_RUN: re.Pattern[str] = re.compile(r"\n{3,}")


class TextExtractor(html.parser.HTMLParser):
    def __init__(self, markdown: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._markdown: bool = markdown
        self._skip_depth: int = 0
        self._heading: int = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if self._markdown and len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._heading = int(tag[1])
            self._parts.append("\n\n" + "#" * self._heading + " ")
        elif self._markdown and tag == "li":
            self._parts.append("\n- ")
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS and not self._skip_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._heading and tag == f"h{self._heading}":
            self._heading = 0
            self._parts.append("\n")
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._parts.append(data)

    def get_text(self) -> str:
        text: str = "".join(self._parts)
        lines: list[str] = [" ".join(line.split()) for line in text.splitlines()]
        joined: str = BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
        return joined + "\n" if joined else ""


def html_to_text(source: str, markdown: bool = False) -> str:
    parser = TextExtractor(markdown=markdown)
    parser.feed(source)
    parser.close()
    return parser.get_text()


def clean_with_soup(source: str, keep_forms: bool) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(source, "html.parser")
    doomed: list[str] = ["script", "style", "nav", "footer", "aside", "iframe", "noscript"]
    if not keep_forms:
        doomed.append("form")
    for tag in soup.find_all(doomed):
        tag.decompose()
    return str(soup)


def main_content(source: str) -> str:
    from readability import Document

    return str(Document(source).summary())


def convert_engine(source: str, engine: str, keep_forms: bool, only_main: bool, command: str, source_path: Path) -> str:
    body: str = main_content(source) if only_main else source
    if engine == "trafilatura":
        import trafilatura

        result: str | None = trafilatura.extract(
            body,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
            no_fallback=False,
        )
        return result or convert_engine(body, "markdownify", keep_forms, False, command, source_path)
    if engine == "html_to_markdown":
        from html_to_markdown import Options, convert

        cleaned: str = clean_with_soup(body, keep_forms)
        options = Options(
            extract_headers=True,
            extract_links=True,
            extract_images=True,
            extract_structured_data=False,
            github_flavored=True,
        )
        return BLANK_RUN.sub("\n\n", str(convert(cleaned, options=options)))
    if engine == "html2text":
        import html2text

        handler = html2text.HTML2Text()
        handler.ignore_links = False
        handler.ignore_images = False
        handler.ignore_tables = False
        handler.body_width = 0
        return handler.handle(body)
    if engine == "markdownify":
        from markdownify import markdownify

        return str(markdownify(body))
    if engine == "command":
        proc = subprocess.run([command, str(source_path)], capture_output=True, text=True, check=True)
        return proc.stdout
    if engine == "text":
        return html_to_text(body, markdown=True)
    msg = f"unhandled engine {engine}"
    raise AssertionError(msg)


def pick_engine(requested: str) -> str:
    if requested != "auto":
        return requested
    candidates: list[tuple[str, str]] = [
        ("trafilatura", "trafilatura"),
        ("html_to_markdown", "html_to_markdown"),
        ("html2text", "html2text"),
        ("markdownify", "markdownify"),
    ]
    import importlib.util

    for module, engine in candidates:
        if importlib.util.find_spec(module) is not None:
            return engine
    return "text"


def convert_one(job: tuple[Path, str, bool, bool, str, bool]) -> tuple[Path, bool, str]:
    path, engine, keep_forms, only_main, command, remove_original = job
    dest: Path = path.with_suffix(".md")
    if dest.exists():
        return path, True, "skipped (md exists)"
    try:
        markdown: str = convert_engine(
            path.read_text(encoding="utf-8", errors="ignore"), engine, keep_forms, only_main, command, path
        )
    except (ImportError, subprocess.CalledProcessError, OSError) as exc:
        return path, False, f"{type(exc).__name__}: {exc}"
    if not markdown.strip():
        return path, False, "no content extracted"
    dest.write_text(markdown if markdown.endswith("\n") else markdown + "\n", encoding="utf-8")
    if remove_original:
        path.unlink()
    return path, True, f"-> {dest.name}"


def find_html(inputs: Sequence[Path], recursive: bool) -> list[Path]:
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            found.add(root)
        elif root.is_dir():
            candidates = root.rglob("*") if recursive else root.iterdir()
            found.update(p for p in candidates if p.is_file() and p.suffix.lower() in HTML_SUFFIXES)
    return sorted(found)


def run_batch(files: list[Path], engine: str, args: argparse.Namespace) -> int:
    jobs: list[tuple[Path, str, bool, bool, str, bool]] = [
        (p, engine, args.keep_forms, args.main_content, args.command, args.remove_original) for p in files
    ]
    ok: int = 0
    with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        for path, success, note in pool.map(convert_one, jobs):
            ok += 1 if success else 0
            print(f"{'OK ' if success else 'FAIL'} {path.name} {note}")
    print(f"Conversion complete: {ok}/{len(files)} (engine={engine})")
    return 0 if ok == len(files) else 1


def cmd_to_md(args: argparse.Namespace) -> int:
    files: list[Path] = find_html(args.paths, args.recursive)
    if not files:
        print("No HTML files found.")
        return 0
    return run_batch(files, pick_engine(args.engine), args)


def cmd_to_text(args: argparse.Namespace) -> int:
    files: list[Path] = find_html(args.paths, args.recursive)
    count: int = 0
    for path in files:
        text: str = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            continue
        dest: Path = (args.out_dir / f"{path.stem}.txt") if args.out_dir else path.with_suffix(".txt")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"{path.name} -> {dest}")
        count += 1
        if args.remove_original:
            path.unlink()
    print(f"Converted {count} file(s) to text.")
    return 0


def fetch_plain(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "html-to-markdown-tool"})
    with urllib.request.urlopen(request, timeout=30) as response:
        source: str = response.read().decode("utf-8", errors="replace")
    return html_to_text(source)


def cmd_fetch(args: argparse.Namespace) -> int:
    if args.render:
        try:
            import asyncio

            from pyppeteer import launch
        except ImportError:
            print(
                "Error: --render needs pyppeteer (pip install pyppeteer). Falling back to plain fetch.", file=sys.stderr
            )
        else:

            async def render() -> str:
                browser = await launch(args=["--no-sandbox"])
                page = await browser.newPage()
                await page.goto(args.url)
                if args.screenshot is not None:
                    await page.screenshot({"path": str(args.screenshot)})
                text: str = str(await page.evaluate("document.body.textContent", force_expr=True))
                await browser.close()
                return text

            write_fetch(args, asyncio.run(render()))
            return 0
    write_fetch(args, fetch_plain(args.url))
    return 0


def write_fetch(args: argparse.Namespace, text: str) -> None:
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output} ({len(text)} chars)")
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HTML -> Markdown/text conversion.")
    sub = parser.add_subparsers(dest="command", required=True)
    md = sub.add_parser("to-md", help="Convert HTML files to Markdown")
    md.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: .)")
    md.add_argument(
        "--engine",
        choices=("auto", "trafilatura", "html_to_markdown", "html2text", "markdownify", "command", "text"),
        default="auto",
    )
    md.add_argument("--command", default="rhtml2md", help="External converter (htm2md default)")
    md.add_argument("--main-content", action="store_true", help="Readability-extract first (needs readability-lxml)")
    md.add_argument("--keep-forms", action="store_true", help="Keep <form> (html_to_markdown engine)")
    md.add_argument("-r", "--remove-original", action="store_true", help="Delete HTML after success")
    md.add_argument("--no-recursive", dest="recursive", action="store_false")
    md.set_defaults(handler=cmd_to_md, recursive=True)
    tx = sub.add_parser("to-text", help="Strip HTML tags to plain text (stdlib)")
    tx.add_argument("paths", nargs="*", type=Path)
    tx.add_argument("-o", "--out-dir", type=Path, default=None, help="Output dir (default: beside source)")
    tx.add_argument("-r", "--remove-original", action="store_true")
    tx.add_argument("--no-recursive", dest="recursive", action="store_false")
    tx.set_defaults(handler=cmd_to_text, recursive=True)
    fe = sub.add_parser("fetch", help="Fetch a URL as text")
    fe.add_argument("url")
    fe.add_argument("-o", "--output", type=Path, default=None)
    fe.add_argument("--render", action="store_true", help="Render JS with pyppeteer")
    fe.add_argument("--screenshot", type=Path, default=None, help="With --render, save a PNG here")
    fe.set_defaults(handler=cmd_fetch)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
