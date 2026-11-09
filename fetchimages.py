#!/data/data/com.termux/files/usr/bin/env python
"""Crawl a website and download its images.
Breadth-first crawls the pages of the same host (``--all-hosts`` opens it up), bounded by ``--max-pages`` and ``--max-depth``, and saves every image into ``downloads`` (``-o``) under a sanitised name with dedup by content hash.
Usage: fetchimages URL [-o DIR] [--max-pages N] [--max-depth N] Positional arguments: url Starting URL, e.g.
https://example.com/ Options: -o, --output Output directory (default: ./downloads) --max-pages Maximum number of HTML pages to crawl --max-depth Maximum link-following depth --all-hosts Follow links to external hosts too --timeout Per-request timeout in seconds Examples: $ fetchimages https://example.com --max-pages 50 Command-line script: run ``python fetchimages.py`` or the ``fetchimages`` command.
The module is importable as well."""

import argparse
from collections import deque
import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urljoin, urlparse

import pycurl


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".ico", ".avif"}
USER_AGENT = "Mozilla/5.0 (compatible; ImageCrawler/1.0)"
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.images: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = a["href"]
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            for key in ("src", "data-src", "data-original", "data-lazy"):
                if a.get(key):
                    self.images.append(a[key])
            if a.get("srcset"):
                for part in a["srcset"].split(","):
                    url = part.strip().split(" ")[0]
                    if url:
                        self.images.append(url)
        elif tag == "source" and a.get("srcset"):
            for part in a["srcset"].split(","):
                url = part.strip().split(" ")[0]
                if url:
                    self.images.append(url)


def fetch(url: str, timeout: int = 30):
    body = io.BytesIO()
    headers = io.BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEDATA, body)
    c.setopt(c.HEADERFUNCTION, headers.write)
    c.setopt(c.FOLLOWLOCATION, True)
    c.setopt(c.MAXREDIRS, 10)
    c.setopt(c.TIMEOUT, timeout)
    c.setopt(c.CONNECTTIMEOUT, timeout)
    c.setopt(c.USERAGENT, USER_AGENT)
    c.setopt(c.ACCEPT_ENCODING, "")
    try:
        c.perform()
        status = c.getinfo(c.RESPONSE_CODE)
        ctype = c.getinfo(c.CONTENT_TYPE) or ""
    except pycurl.error as exc:
        print(f"  ! pycurl error on {url}: {exc}", file=sys.stderr)
        return None, None, None
    finally:
        c.close()
    return status, ctype.lower(), body.getvalue()


def sanitize(segment: str) -> str:
    return SAFE_RE.sub("_", segment) or "_"


def save_image(url: str, data: bytes, out_dir: Path) -> Path:
    p = urlparse(url)
    segments = [s for s in unquote(p.path).split("/") if s]
    if not segments:
        segments = ["index"]
    sub = out_dir / sanitize(p.netloc)
    for seg in segments[:-1]:
        sub = sub / sanitize(seg)
    sub.mkdir(parents=True, exist_ok=True)
    fname = sanitize(segments[-1])
    if "." not in fname:
        fname += ".bin"
    target = sub / fname
    if target.exists():
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        target = target.with_name(f"{target.stem}_{digest}{target.suffix}")
    target.write_bytes(data)
    return target


def crawl(
    start_url: str, out_dir: Path, max_pages: int = 200, max_depth: int = 3, same_host: bool = True, timeout: int = 30
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_host = urlparse(start_url).netloc
    visited_pages: set[str] = set()
    seen_images: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    while queue:
        url, depth = queue.popleft()
        url = url.split("#", 1)[0]
        if url in visited_pages:
            continue
        if len(visited_pages) >= max_pages:
            print("Reached max_pages limit.")
            break
        visited_pages.add(url)
        print(f"[{depth}] page  {url}")
        status, ctype, body = fetch(url, timeout=timeout)
        if status != 200 or body is None:
            print(f"  ! skipped ({status})", file=sys.stderr)
            continue
        if ctype.startswith("image/"):
            if url not in seen_images:
                seen_images.add(url)
                path = save_image(url, body, out_dir)
                print(f"  -> saved {path}")
            continue
        if "html" not in ctype:
            continue
        html = body.decode("utf-8", errors="replace")
        parser = LinkParser()
        try:
            parser.feed(html)
        except Exception as exc:
            print(f"  ! parse error: {exc}", file=sys.stderr)
        base = urljoin(url, parser.base) if parser.base else url
        for raw in parser.images:
            img_url = urljoin(base, raw).split("#", 1)[0]
            if not img_url.lower().startswith(("http://", "https://")):
                continue
            if img_url in seen_images:
                continue
            seen_images.add(img_url)
            st, ct, data = fetch(img_url, timeout=timeout)
            if st == 200 and data:
                is_img = ct.startswith("image/") or Path(urlparse(img_url).path).suffix.lower() in IMAGE_EXTS
                if is_img:
                    path = save_image(img_url, data, out_dir)
                    print(f"  -> saved {path}")
                else:
                    print(f"  - not an image ({ct}) {img_url}")
        if depth >= max_depth:
            continue
        for raw in parser.links:
            nxt = urljoin(base, raw).split("#", 1)[0]
            nxt_p = urlparse(nxt)
            if nxt_p.scheme not in ("http", "https"):
                continue
            if same_host and nxt_p.netloc != start_host:
                continue
            if nxt not in visited_pages:
                queue.append((nxt, depth + 1))
    print(f"\nDone. Pages crawled: {len(visited_pages)}  Images downloaded: {len(seen_images)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Recursively download images from a website.")
    ap.add_argument("url", help="Starting URL, e.g. https://example.com/")
    ap.add_argument("-o", "--output", default="downloads", help="Output directory (default: ./downloads)")
    ap.add_argument("--max-pages", type=int, default=200, help="Maximum number of HTML pages to crawl")
    ap.add_argument("--max-depth", type=int, default=3, help="Maximum link-following depth")
    ap.add_argument("--all-hosts", action="store_true", help="Follow links to external hosts too")
    ap.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds")
    args = ap.parse_args()
    crawl(
        start_url=args.url,
        out_dir=Path(args.output),
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        same_host=not args.all_hosts,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
