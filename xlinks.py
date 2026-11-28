#!/data/data/com.termux/files/usr/bin/env python
"""
Extract URLs from a remote web page and save unique ones to urls.txt.

Usage:
    python script.py https://example.com/page

"""

from __future__ import annotations
import re
import sys
from typing import Any, Iterable, Sequence
import urllib.error
from urllib.parse import urljoin, urlparse
import urllib.request


def fetch_html(url: str, timeout: int = 15) -> str:
    """Fetch HTML content from a URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_urls(html: str, base_url: str):
    """
    Extract URLs from HTML content.
    - Absolute URLs (http://, https://) are kept as-is.
    - Relative URLs (href/src) are resolved against the base URL.
    - Fragment-only links (#...) are skipped.
    """
    urls = set()

    # 1) Absolute URLs anywhere in the page
    absolute_pattern = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)
    for match in absolute_pattern.findall(html):
        # Clean trailing punctuation that isn't part of the URL
        cleaned = match.rstrip(".,;:!?)\\]'\"")
        urls.add(cleaned)

    # 2) href/src attributes (may be relative)
    attr_pattern = re.compile(
        r'(?:href|src)\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for raw in attr_pattern.findall(html):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        # Resolve relative URLs against the base URL
        resolved = urljoin(base_url, raw)
        urls.add(resolved)

    return urls


def save_urls(urls: Iterable[Any], filename: str = "urls.txt") -> None:
    """Save a sorted list of URLs to a file."""
    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(url + "\n" for url in sorted(urls))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python script.py <URL>")
        sys.exit(1)

    target_url = sys.argv[1]

    # Basic validation
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print(f"Invalid URL: {target_url}")
        sys.exit(1)

    print(f"Fetching: {target_url}")
    try:
        html = fetch_html(target_url)
    except urllib.error.HTTPError as e:
        print(f"HTTP error {e.code}: {e.reason}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URL error: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Downloaded {len(html)} characters of HTML.")
    urls = extract_urls(html, target_url)
    print(f"Found {len(urls)} unique URLs.")

    save_urls(urls, "urls.txt")
    print("Saved to urls.txt")


if __name__ == "__main__":
    raise SystemExit(main())
