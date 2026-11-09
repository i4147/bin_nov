#!/data/data/com.termux/files/usr/bin/env python
"""Generate a structural HTML skeleton template from a URL or a local page.
Drops the scripts, styles, media and SVGs, keeps the ``class``/``id``/``role`` attributes up to ``--max-depth``, preserves the head CSS for the visual fidelity and writes the template to ``-o`` or prints it.
Usage: mkt SOURCE [-o OUTDIR] [-d DEPTH] [--print] Positional arguments: source Website URL or path to a local HTML file Options: -o, --out-dir -n, --name Base filename (no extension) -d, --max-depth --print Print to stdout instead of writing a file Examples: $ mkt https://example.com -o skeletons Command-line script: run ``python mkt.py`` or the ``mkt`` command.
The module is importable as well."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
import requests


TEMPLATES_DIR: str = "templates"
DEFAULT_MAX_DEPTH: int = 6

DROP_TAGS: set[str] = {
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "video",
    "audio",
    "source",
    "track",
    "embed",
    "object",
    "template",
    "map",
    "math",
    "picture",
    "br",
    "hr",
}

ATTRS_TO_KEEP: tuple[str, ...] = ("class", "id", "role")


def fetch_url(url: str, timeout: int = 15) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def read_local_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def copy_attrs(src: Tag, dst: Tag) -> None:
    for attr in ATTRS_TO_KEEP:
        if src.has_attr(attr):
            val = src[attr]
            if isinstance(val, list):
                val = " ".join(val)
            dst[attr] = val


def clone_tag(tag: Tag, soup: BeautifulSoup) -> Tag:
    new_tag = soup.new_tag(tag.name)
    for k, v in tag.attrs.items():
        new_tag[k] = " ".join(v) if isinstance(v, list) else v
    return new_tag


def skeletonize(
    node: Tag | NavigableString,
    soup: BeautifulSoup,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Optional[Tag]:
    if isinstance(node, Comment):
        return None
    if not isinstance(node, Tag):
        return None
    if node.name in DROP_TAGS:
        return None

    new_tag = soup.new_tag(node.name)
    copy_attrs(node, new_tag)

    if node.name == "a" and node.has_attr("href"):
        new_tag["href"] = node["href"]
    if node.name == "form":
        for a in ("action", "method"):
            if node.has_attr(a):
                new_tag[a] = node[a]

    if node.name == "img":
        new_tag["src"] = "{{ image_url }}"
        new_tag["alt"] = node.get("alt", "")
        return new_tag

    if node.name in ("input", "textarea", "select"):
        for a in ("type", "name", "placeholder"):
            if node.has_attr(a):
                new_tag[a] = node[a]
        return new_tag

    if node.name == "button":
        for a in ("type", "name"):
            if node.has_attr(a):
                new_tag[a] = node[a]
        new_tag.string = "{{ button_text }}"
        return new_tag

    if depth >= max_depth:
        return new_tag

    child_count = 0
    for child in node.children:
        sk = skeletonize(child, soup, depth + 1, max_depth)
        if sk is not None:
            new_tag.append(sk)
            child_count += 1

    if child_count == 0 and node.get_text(strip=True):
        new_tag.string = "{{ " + node.name + "_content }}"

    return new_tag


def build_template(
    html: str,
    source_label: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    src = BeautifulSoup(html, "html.parser")
    out = BeautifulSoup("", "html.parser")

    html_tag = out.new_tag("html")
    if src.html:
        for a in ("lang", "dir", "class"):
            if src.html.has_attr(a):
                v = src.html[a]
                html_tag[a] = " ".join(v) if isinstance(v, list) else v
    if "lang" not in html_tag.attrs:
        html_tag["lang"] = "en"

    head = out.new_tag("head")
    charset = out.new_tag("meta")
    charset["charset"] = "utf-8"
    head.append(charset)

    viewport = out.new_tag("meta")
    viewport["name"] = "viewport"
    viewport["content"] = "width=device-width, initial-scale=1"
    head.append(viewport)

    title = out.new_tag("title")
    title.string = "{{ page_title }}"
    head.append(title)

    if src.head:
        for base in src.head.find_all("base"):
            head.append(clone_tag(base, out))

        for link in src.head.find_all("link"):
            rel = link.get("rel") or []
            if isinstance(rel, str):
                rel = [rel]
            rel_lower = [r.lower() for r in rel]
            if "stylesheet" not in rel_lower:
                continue
            head.append(clone_tag(link, out))

        for style in src.head.find_all("style"):
            st = out.new_tag("style")
            for k, v in style.attrs.items():
                st[k] = " ".join(v) if isinstance(v, list) else v
            st.string = style.get_text()
            head.append(st)

    html_tag.append(head)

    body = out.new_tag("body")
    if src.body:
        copy_attrs(src.body, body)
        for child in src.body.children:
            sk = skeletonize(child, out, depth=1, max_depth=max_depth)
            if sk is not None:
                body.append(sk)

    html_tag.append(body)
    out.append(html_tag)

    header = f"<!-- Skeleton generated from {source_label} on {datetime.now(timezone.utc).isoformat()} -->\n"
    return "<!DOCTYPE html>\n" + header + out.prettify()


def slugify_url(url: str) -> str:
    p = urlparse(url)
    host = p.netloc or "site"
    path = p.path.strip("/") or "index"
    slug = f"{host}_{path}".replace("/", "_")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "template"


def slugify_path(path: Path) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", path.stem)
    return slug.strip("_") or "template"


def unique_path(directory: Path, base_name: str, ext: str = ".html") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / (base_name + ext)
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = directory / f"{base_name}_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


def resolve_source(source: str) -> tuple[str, str, str]:
    if re.match(r"^https?://", source):
        return "url", source, slugify_url(source)
    p = Path(source)
    if p.is_file():
        return "file", str(p.resolve()), slugify_path(p)
    if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/|$)", source):
        url = "https://" + source
        return "url", url, slugify_url(url)
    msg = f"Neither a URL nor an existing file: {source}"
    raise ValueError(msg)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Generate a structural HTML skeleton template from a URL "
            "or a local HTML file, preserving head CSS for visual fidelity."
        )
    )
    ap.add_argument("source", help="Website URL or path to a local HTML file")
    ap.add_argument(
        "-o",
        "--out-dir",
        default=TEMPLATES_DIR,
        help=f"Output directory (default: {TEMPLATES_DIR})",
    )
    ap.add_argument("-n", "--name", help="Base filename (no extension)")
    ap.add_argument(
        "-d",
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"Max DOM depth to preserve (default: {DEFAULT_MAX_DEPTH})",
    )
    ap.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print to stdout instead of writing a file",
    )
    args = ap.parse_args()

    try:
        kind, label, base_name = resolve_source(args.source)
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    try:
        if kind == "url":
            print(f"[+] Fetching {label}", file=sys.stderr)
            html = fetch_url(label)
        else:
            print(f"[+] Reading {label}", file=sys.stderr)
            html = read_local_file(Path(label))
    except Exception as e:
        print(f"[!] Load failed: {e}", file=sys.stderr)
        return 1

    template = build_template(html, label, max_depth=args.max_depth)

    if args.print_only:
        print(template)
        return 0

    final_base = args.name or base_name
    path = unique_path(Path(args.out_dir), final_base)
    path.write_text(template, encoding="utf-8")
    print(f"[+] Template saved: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
