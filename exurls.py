#!/data/data/com.termux/files/usr/bin/env python
"""Extract the links of a web page and save them to files.
Fetches the URL with a browser-like session, parses the ``href``/``src`` attributes with BeautifulSoup, splits the internal and the external links and writes both lists to the current directory.
Usage: exurls URL Positional arguments: url Target URL Examples: $ exurls https://example.com Command-line script: run ``python exurls.py`` or the ``exurls`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import sys
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests
from requests.sessions import Session


def create_session() -> Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    })
    return session


def extract_links(url: str, session: requests.Session):
    resp = session.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    links = set()
    for tag in soup.find_all("a", href=True):
        href = tag.get("href").strip()
        if href and not href.startswith("#") and not href.startswith("javascript:"):
            abs_url = urljoin(url, href)
            parsed = urlparse(abs_url)
            if parsed.scheme in {"http", "https"}:
                links.add(abs_url)
    for tag in soup.find_all(["video", "source", "iframe"], src=True):
        src = tag.get("src").strip()
        if src:
            abs_url = urljoin(url, src)
            parsed = urlparse(abs_url)
            if parsed.scheme in {"http", "https"}:
                links.add(abs_url)
    return sorted(links)


def split_internal_external(base_url, links):
    base_domain = urlparse(base_url).netloc
    internal = []
    external = []
    for link in links:
        if urlparse(link).netloc == base_domain:
            internal.append(link)
        else:
            external.append(link)
    return internal, external


def save_links(name: str, links) -> None:
    path = Path(name)
    content = "\n".join(links)
    path.write_text(content, encoding="utf-8")
    print(f"Saved {len(links)} links to {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract and save all URLs from a webpage")
    parser.add_argument("url", nargs="?", help="Target URL")
    args = parser.parse_args()
    url = args.url or input("Enter URL: ").strip()
    if not url.startswith(("http://", "https://")):
        print("Error: URL must start with http:// or https://", file=sys.stderr)
        sys.exit(1)
    session = create_session()
    try:
        links = extract_links(url, session)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            print(
                "Access forbidden (403). The site may be blocking automated access.",
                file=sys.stderr,
            )
        else:
            print(f"HTTP error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Failed to fetch or parse URL: {e}", file=sys.stderr)
        sys.exit(1)
    internal, external = split_internal_external(url, links)
    save_links("all_links.txt", links)
    if internal:
        save_links("internal.txt", internal)
    if external:
        save_links("external.txt", external)
    print(f"Total links: {len(links)} (Internal: {len(internal)}, External: {len(external)})")


if __name__ == "__main__":
    raise SystemExit(main())
