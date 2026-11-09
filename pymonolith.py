#!/data/data/com.termux/files/usr/bin/env python
"""Save a web page as one HTML file with the CSS, JS, images and fonts embedded.
Fetches the URL (or reads the local file), inlines the stylesheets and scripts, rewrites the ``url(...)`` references as data URIs, keeps the fonts and images inside the page and writes the archive to ``-o`` (with ``-t`` timeout and ``-e`` for the ignored errors).
Usage: pymonolith SOURCE [-o OUT.html] [-t TIMEOUT] Positional arguments: source URL or local file path of the page to archive Options: -o, --output Output file path (default: stdout) -t, --timeout HTTP request timeout in seconds -e, --ignore-errors Ignore unreachable sub-resources and continue -i, --no-images Strip all <img> elements from the output --encoding Text encoding used for output and local files --css-mode How to embed <link rel=stylesheet>: 'inline' -> <style> block (monolithei); 'data-uri' -> href=data:...
(pymonolith) --prettify Pretty-print the resulting HTML (pymonolith default) --no-meta-charset Do NOT inject/overwrite <meta charset=...> (pymonolith default) --user-agent User-Agent header to send with requests Examples: $ pymonolith https://example.com -o example.html Command-line script: run ``python pymonolith.py`` or the ``pymonolith`` command.
The module is importable as well."""

import argparse
import base64
from pathlib import Path
import re
import sys
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests


DEFAULT_MIME_TYPES: dict[str, str] = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
DEFAULT_USER_AGENT: str = "Mozilla/5.0 (X11; Linux x86_64) Monolith/1.0"
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^)'"]+?)['"]?\s*\)""")


class Monolith:
    def __init__(
        self,
        *,
        timeout: int = 10,
        encoding: str = "utf-8",
        ignore_errors: bool = False,
        no_images: bool = False,
        css_mode: str = "inline",
        prettify: bool = False,
        inject_meta_charset: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        mime_types: Optional[dict[str, str]] = None,
    ) -> None:
        self.timeout = timeout
        self.encoding = encoding
        self.ignore_errors = ignore_errors
        self.no_images = no_images
        if css_mode not in ("inline", "data-uri"):
            msg = f"Invalid css_mode: {css_mode!r}"
            raise ValueError(msg)
        self.css_mode = css_mode
        self.prettify = prettify
        self.inject_meta_charset = inject_meta_charset
        self.mime_types = dict(mime_types or DEFAULT_MIME_TYPES)
        self.base_url: str = ""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._cache: dict[str, bytes] = {}

    def fetch(self, url: str) -> bytes:
        if url in self._cache:
            return self._cache[url]
        try:
            if url.startswith("file://"):
                parsed = urlparse(url)
                data = Path(parsed.path).read_bytes()
            else:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                resp.raise_for_status()
                data = resp.content
            self._cache[url] = data
            return data
        except Exception as exc:
            if not self.ignore_errors:
                raise
            print(f"⚠ Skipping unreachable resource: {url} ({exc})", file=sys.stderr)
            return b""

    def to_data_uri(self, data: bytes, mime: str = "application/octet-stream") -> str:
        if not data:
            return ""
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def guess_mime(self, url: str, default: str = "application/octet-stream") -> str:
        ext = Path(urlparse(url).path).suffix.lower()
        return self.mime_types.get(ext, default)

    def resolve_url(self, url: str) -> str:
        if url.startswith(("data:", "#")):
            return url
        if url.startswith("//"):
            scheme = urlparse(self.base_url).scheme or "http"
            return f"{scheme}:{url}"
        return urljoin(self.base_url, url)

    def replace_css_urls(self, css: str, base_url: str) -> str:
        def repl(match: "re.Match[str]") -> str:
            raw = match.group(1).strip()
            if raw.startswith(("data:", "#")):
                return match.group(0)
            resolved = urljoin(base_url, raw)
            try:
                data = self.fetch(resolved)
                if not data:
                    return match.group(0)
                mime = self.guess_mime(resolved, "application/octet-stream")
                uri = self.to_data_uri(data, mime)
                return f"url('{uri}')" if uri else match.group(0)
            except Exception:
                if not self.ignore_errors:
                    raise
                return match.group(0)

        return CSS_URL_RE.sub(repl, css)

    def inline_stylesheets(self, soup: BeautifulSoup) -> None:
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href")
            if not href:
                continue
            url = self.resolve_url(href)
            try:
                data = self.fetch(url)
                if not data:
                    continue
                if self.css_mode == "inline":
                    css = data.decode("utf-8", errors="ignore")
                    css = self.replace_css_urls(css, url)
                    style = soup.new_tag("style")
                    style["type"] = "text/css"
                    style.string = css
                    link.replace_with(style)
                else:
                    uri = self.to_data_uri(data, "text/css")
                    if uri:
                        link["href"] = uri
            except Exception as exc:
                if not self.ignore_errors:
                    raise
                print(f"⚠ Skipping CSS: {url} ({exc})", file=sys.stderr)

    def inline_scripts(self, soup: BeautifulSoup) -> None:
        for script in soup.find_all("script", src=True):
            src = script.get("src")
            if not src:
                continue
            url = self.resolve_url(src)
            try:
                data = self.fetch(url)
                if not data:
                    continue
                del script["src"]
                script.string = data.decode("utf-8", errors="ignore")
            except Exception as exc:
                if not self.ignore_errors:
                    raise
                print(f"⚠ Skipping script: {url} ({exc})", file=sys.stderr)

    def inline_images(self, soup: BeautifulSoup) -> None:
        if self.no_images:
            for img in soup.find_all("img"):
                img.decompose()
            return
        for img in soup.find_all("img"):
            src = img.get("src")
            if not src or src.startswith("data:"):
                continue
            url = self.resolve_url(src)
            try:
                data = self.fetch(url)
                if not data:
                    continue
                mime = self.guess_mime(url, "image/png")
                uri = self.to_data_uri(data, mime)
                if uri:
                    img["src"] = uri
            except Exception as exc:
                if not self.ignore_errors:
                    raise
                print(f"⚠ Skipping image: {url} ({exc})", file=sys.stderr)
        for tag in soup.find_all(srcset=True):
            entries = []
            for item in tag.get("srcset", "").split(","):
                item = item.strip()
                if not item:
                    continue
                pieces = item.split()
                raw_url = pieces[0]
                descriptor = " ".join(pieces[1:]) if len(pieces) > 1 else ""
                if raw_url.startswith("data:"):
                    entries.append(item)
                    continue
                url = self.resolve_url(raw_url)
                try:
                    data = self.fetch(url)
                    if data:
                        mime = self.guess_mime(url, "image/png")
                        uri = self.to_data_uri(data, mime)
                        if uri:
                            entries.append(f"{uri} {descriptor}".strip())
                            continue
                except Exception:
                    if not self.ignore_errors:
                        raise
                entries.append(item)
            if entries:
                tag["srcset"] = ", ".join(entries)

    def process_style_tags(self, soup: BeautifulSoup) -> None:
        for style in soup.find_all("style"):
            if not style.string:
                continue
            style.string = self.replace_css_urls(style.string, self.base_url)

    def ensure_meta_charset(self, soup: BeautifulSoup) -> None:
        meta = soup.find("meta", charset=True)
        if meta is not None:
            meta["charset"] = self.encoding
            return
        new_meta = soup.new_tag("meta", charset=self.encoding)
        head = soup.find("head")
        if head is not None:
            head.insert(0, new_meta)
        else:
            new_head = soup.new_tag("head")
            new_head.append(new_meta)
            html = soup.find("html")
            if html is not None:
                html.insert(0, new_head)

    def process_html(self, html: str, base_url: str) -> str:
        self.base_url = base_url
        soup = BeautifulSoup(html, "html.parser")
        if self.inject_meta_charset:
            self.ensure_meta_charset(soup)
        self.inline_stylesheets(soup)
        self.inline_scripts(soup)
        self.inline_images(soup)
        self.process_style_tags(soup)
        return soup.prettify() if self.prettify else str(soup)

    def from_url(self, url: str) -> str:
        print(f"📥 Fetching {url}...", file=sys.stderr)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        if not resp.encoding:
            resp.encoding = self.encoding
        return self.process_html(resp.text, url)

    def from_file(self, path: str) -> str:
        resolved = Path(path).resolve()
        print(f"📂 Loading {resolved}...", file=sys.stderr)
        html = resolved.read_text(encoding=self.encoding, errors="ignore")
        return self.process_html(html, resolved.as_uri())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monolith",
        description=(
            "Save a webpage (URL or local file) as a single HTML file with embedded CSS / JS / images / fonts."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source", help="URL or local file path of the page to archive")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=10,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "-e",
        "--ignore-errors",
        action="store_true",
        help="Ignore unreachable sub-resources and continue",
    )
    parser.add_argument(
        "-i",
        "--no-images",
        action="store_true",
        help="Strip all <img> elements from the output",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding used for output and local files",
    )
    parser.add_argument(
        "--css-mode",
        choices=("inline", "data-uri"),
        default="inline",
        help=(
            "How to embed <link rel=stylesheet>: "
            "'inline' -> <style> block (monolithei); "
            "'data-uri' -> href=data:... (pymonolith)"
        ),
    )
    parser.add_argument(
        "--prettify",
        action="store_true",
        help="Pretty-print the resulting HTML (pymonolith default)",
    )
    parser.add_argument(
        "--no-meta-charset",
        action="store_true",
        help="Do NOT inject/overwrite <meta charset=...> (pymonolith default)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header to send with requests",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    archiver = Monolith(
        timeout=args.timeout,
        encoding=args.encoding,
        ignore_errors=args.ignore_errors,
        no_images=args.no_images,
        css_mode=args.css_mode,
        prettify=args.prettify,
        inject_meta_charset=not args.no_meta_charset,
        user_agent=args.user_agent,
    )
    try:
        if args.source.startswith(("http://", "https://")):
            html = archiver.from_url(args.source)
        else:
            html = archiver.from_file(args.source)
        if args.output:
            out_path = Path(args.output)
            print(f"💾 Writing to {out_path}...", file=sys.stderr)
            out_path.write_text(html, encoding=args.encoding)
            print(f"✅ Done! Saved to {out_path}", file=sys.stderr)
        else:
            print(html)
        return 0
    except requests.RequestException as exc:
        print(f"❌ Network error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"❌ File not found: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌ Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
