#!/data/data/com.termux/files/usr/bin/env python
"""Inline, bundle and stand-alone HTML assets with one CLI.
``bundle`` extracts the CSS/JS/images of a page and rebuilds a single ``single_page_local.html``, ``inline`` embeds local and remote assets directly into existing HTML/CSS, ``isolate`` writes ``<name>_standalone.html``, ``standalone`` does the same in parallel with a remote cache, ``mhtml`` unpacks ``.mhtml`` archives and ``css`` inlines ``@import`` rules and fonts.
Options control the output directories, timeouts, user agent and the maximum download size.
Usage: htmltools {bundle,inline,isolate,standalone,mhtml,css} [options] Positional arguments: paths ...
input inputs ...
One or more .mhtml files (default: *.mhtml in cwd) Subcommands: bundle Extract all assets and build single_page_local.html inline Inline local/remote assets into HTML/CSS files in place isolate Produce a <name>_standalone.html with local assets embedded standalone Multi-process standalone builder with remote cache mhtml Convert .mhtml to .html + <stem>_files/ directory css Inline @import and url() fonts in a CSS file Options: --output-dir --assets-dir --timeout --user-agent --no-remote-scripts Do NOT download remote <script src> -o, --output -v, --verbose --max-size --no-prompt Skip interactive prompt for large downloads --files-dir Output directory for extracted resources ...
plus 1 more; run ``htmltools --help`` for the full list.
Examples: $ htmltools bundle page.html --output-dir out/ $ htmltools inline index.html Command-line script: run ``python htmltools.py`` or the ``htmltools`` command.
The module is importable as well."""

import argparse
import base64
import contextlib
import hashlib
from io import BytesIO
import mimetypes
from multiprocessing import Pool
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from dh import MAX_WORKERS
from loguru import logger
import requests

try:
    from loguru import logger

    logger.remove()
    logger.add(
        sys.stderr,
        level="WARNING",
        format="<red>{level}</red> | <cyan>{message}</cyan>",
    )
except ImportError:
    pass
try:
    import pycurl
except ImportError:
    pycurl = None
IMAGE_EXTS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".avif",
    ".bmp",
    ".tiff",
    ".apng",
}
HTML_EXTS: set[str] = {".html", ".htm"}
CSS_URL_RE = re.compile(r'url\((["\']?)([^)"\']+)\1\)')
CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?["\']?([^"\')\s;]+)["\']?\s*\)?',
    re.IGNORECASE,
)
DATA_URI_RE = re.compile(r"data:(.*?);base64,(.*)", re.DOTALL)
DATA_URI_CSS_RE = re.compile(r'url\("(data:.*?)"\)')
SRC_HREF_DATA_URI_RE = re.compile(r'(src|href)=["\'](data:[^"\']+)["\']', re.IGNORECASE)
CID_RE = re.compile(r'(src|href)=["\']cid:([^"\']+)["\']', re.IGNORECASE)


def guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    if mime:
        return mime
    ext = Path(name).suffix.lower()
    return {
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".eot": "application/vnd.ms-fontobject",
    }.get(ext, "application/octet-stream")


def to_data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def decode_data_uri(uri: str) -> tuple[bytes, str] | None:
    m = DATA_URI_RE.match(uri)
    if not m:
        return None
    mime, b64 = m.groups()
    try:
        return base64.b64decode(b64), mime
    except Exception:
        return None


def is_remote(url: str) -> bool:
    return url.startswith(("http://", "https://", "//"))


def is_image_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTS


def strip_query_fragment(url: str) -> str:
    return url.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]


def safe_filename(name: str) -> str:
    name = name.strip().strip('"').strip("'")
    name = name.replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "resource"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_stem(f"{path.stem}_{i}")
        if not cand.exists():
            return cand
        i += 1


def discover_html(root: Path, recursive: bool = True) -> list[Path]:
    return [
        p for p in (root.rglob("*") if recursive else root.iterdir()) if p.is_file() and p.suffix.lower() in HTML_EXTS
    ]


def fetch_remote(url: str, timeout: int) -> bytes | None:
    if url.startswith("//"):
        url = "https:" + url
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (htmltool/1.0)"})
        r.raise_for_status()
        return r.content
    except Exception as exc:
        logger.warning(f"Failed to fetch {url}: {exc}")
        return None


def cmd_bundle(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    out_dir = cwd / args.output_dir
    assets_dir = out_dir / args.assets_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Path] = {}

    def save_asset(data: bytes, mime: str) -> Path:
        key = hashlib.sha256(data).hexdigest()
        if key in cache:
            return cache[key]
        ext = mimetypes.guess_extension(mime) or ""
        p = assets_dir / f"{key}{ext}"
        p.write_bytes(data)
        cache[key] = p
        return p

    def curl_fetch(url: str) -> Path | None:
        if pycurl is None:
            body = fetch_remote(url, args.timeout)
            return save_asset(body, "application/octet-stream") if body else None
        buf, hbuf = BytesIO(), BytesIO()
        c = pycurl.Curl()
        try:
            c.setopt(c.URL, url)
            c.setopt(c.WRITEDATA, buf)
            c.setopt(c.HEADERDATA, hbuf)
            c.setopt(c.TIMEOUT, args.timeout)
            c.setopt(c.FOLLOWLOCATION, True)
            c.setopt(c.USERAGENT, args.user_agent)
            c.setopt(c.SSL_VERIFYPEER, 1)
            c.setopt(c.SSL_VERIFYHOST, 2)
            c.perform()
            code = c.getinfo(c.RESPONSE_CODE)
            c.close()
            if code != 200:
                return None
            hdr = hbuf.getvalue().decode("iso-8859-1").lower()
            mime = "application/octet-stream"
            for line in hdr.split("\r\n"):
                if line.startswith("content-type:"):
                    mime = line.split(":", 1)[1].strip().split(";")[0]
                    break
            return save_asset(buf.getvalue(), mime)
        except Exception as exc:
            with contextlib.suppress(Exception):
                c.close()
            logger.warning(f"curl failed for {url}: {exc}")
            return None

    soups: list[BeautifulSoup] = []

    def process_html(path: Path) -> None:
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        soups.append(soup)
        for style in soup.find_all("style"):
            if not style.string:
                continue
            p = save_asset(style.string.encode("utf-8"), "text/css")
            style.replace_with(soup.new_tag("link", rel="stylesheet", href=str(p.relative_to(out_dir))))
        for script in soup.find_all("script"):
            if script.get("src"):
                src = script["src"]
                if src.startswith("http") and args.remote_scripts:
                    p = curl_fetch(src)
                    if p:
                        script["src"] = str(p.relative_to(out_dir))
                continue
            p = save_asset((script.string or "").encode("utf-8"), "application/javascript")
            script.replace_with(soup.new_tag("script", src=str(p.relative_to(out_dir))))
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src.startswith("data:"):
                dec = decode_data_uri(src)
                if dec:
                    data, mime = dec
                    img["src"] = str(save_asset(data, mime).relative_to(out_dir))
        for tag in soup.find_all(style=True):
            m = DATA_URI_CSS_RE.search(tag["style"])
            if m:
                dec = decode_data_uri(m.group(1))
                if dec:
                    data, mime = dec
                    tag["style"] = tag["style"].replace(m.group(1), str(save_asset(data, mime).relative_to(out_dir)))
        for svg in soup.find_all("svg"):
            p = save_asset(str(svg).encode("utf-8"), "image/svg+xml")
            svg.replace_with(soup.new_tag("img", src=str(p.relative_to(out_dir))))
        target = out_dir / path.relative_to(cwd)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(soup), encoding="utf-8")
        print(f"Processed: {path}")

    for path in cwd.rglob("*"):
        if path.suffix.lower() in HTML_EXTS and "output" not in path.parts:
            try:
                process_html(path)
            except Exception as exc:
                logger.error(f"{path}: {exc}")
    merged = BeautifulSoup("<html><head></head><body></body></html>", "html.parser")
    for soup in soups:
        if soup.body:
            for el in list(soup.body.contents):
                merged.body.append(el)
    for asset in list(assets_dir.iterdir()):
        uri = to_data_uri(asset.read_bytes(), guess_mime(asset.name))
        merged = BeautifulSoup(str(merged).replace(str(asset.relative_to(out_dir)), uri), "html.parser")
    for link in merged.find_all("link", rel="stylesheet"):
        href = link.get("href", "")
        if href.startswith("data:"):
            css = base64.b64decode(re.sub(r"^data:.*?;base64,", "", href)).decode("utf-8", errors="ignore")
            s = merged.new_tag("style")
            s.string = css
            link.replace_with(s)
    for script in merged.find_all("script", src=True):
        if script["src"].startswith("data:"):
            js = base64.b64decode(re.sub(r"^data:.*?;base64,", "", script["src"])).decode("utf-8", errors="ignore")
            new = merged.new_tag("script")
            new.string = js
            script.replace_with(new)
    out = out_dir / "single_page_local.html"
    out.write_text(str(merged), encoding="utf-8")
    print(f"\nCreated: {out}")
    return 0


def _inline_urls_in_css(
    css: str, base_file: Path | str | None, remote_base: str | None, timeout: int
) -> tuple[str, int, int]:
    local = remote = 0

    def repl(m: re.Match) -> str:
        nonlocal local, remote
        quote, url = m.group(1), m.group(2)
        if url.startswith("data:"):
            return m.group(0)
        if is_remote(url):
            if is_image_url(url):
                return m.group(0)
            full = urljoin(remote_base, url) if remote_base else url
            body = fetch_remote(full, timeout)
            if body:
                uri = to_data_uri(body, guess_mime(urlparse(full).path))
                remote += 1
                return f"url({quote}{uri}{quote})"
            return m.group(0)
        rel = strip_query_fragment(url)
        target = (Path(base_file).parent / rel).resolve()
        if not target.exists():
            logger.warning(f"Missing local CSS asset: {target}")
            return m.group(0)
        uri = to_data_uri(target.read_bytes(), guess_mime(str(target)))
        local += 1
        return f"url({quote}{uri}{quote})"

    return CSS_URL_RE.sub(repl, css), local, remote


def _inline_html_file(path: Path, timeout: int) -> dict[str, Any]:
    result = {
        "path": str(path),
        "local": 0,
        "remote": 0,
        "time": 0.0,
        "status": "success",
    }
    t0 = time.perf_counter()
    try:
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if not src or src.startswith("data:") or is_remote(src):
                continue
            p = (path.parent / strip_query_fragment(src)).resolve()
            if p.exists():
                img["src"] = to_data_uri(p.read_bytes(), guess_mime(str(p)))
                result["local"] += 1
            else:
                logger.warning(f"Missing image: {p}")
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href")
            if not href:
                continue
            css, base_file, remote_base = "", path, None
            if is_remote(href):
                body = fetch_remote(href, timeout)
                if body:
                    css = body.decode("utf-8", errors="ignore")
                    remote_base = href
                    result["remote"] += 1
            else:
                p = (path.parent / strip_query_fragment(href)).resolve()
                if p.exists():
                    css = p.read_text(encoding="utf-8", errors="ignore")
                    base_file = p
                    result["local"] += 1
                else:
                    logger.warning(f"Missing CSS: {p}")
            if css:
                css, l, r = _inline_urls_in_css(css, base_file, remote_base, timeout)
                result["local"] += l
                result["remote"] += r
                s = soup.new_tag("style")
                s.string = css
                link.replace_with(s)
        for script in soup.find_all("script"):
            src = script.get("src")
            if not src:
                continue
            js = ""
            if is_remote(src):
                body = fetch_remote(src, timeout)
                if body:
                    js = body.decode("utf-8", errors="ignore")
                    result["remote"] += 1
            else:
                p = (path.parent / strip_query_fragment(src)).resolve()
                if p.exists():
                    js = p.read_text(encoding="utf-8", errors="ignore")
                    result["local"] += 1
                else:
                    logger.warning(f"Missing script: {p}")
            if js:
                s = soup.new_tag("script")
                s.string = js
                script.replace_with(s)
        for tag in soup.find_all(style=True):
            css, l, r = _inline_urls_in_css(tag["style"], path, None, timeout)
            tag["style"] = css
            result["local"] += l
            result["remote"] += r
        for style in soup.find_all("style"):
            if style.string:
                css, l, r = _inline_urls_in_css(style.string, path, None, timeout)
                style.string = css
                result["local"] += l
                result["remote"] += r
        path.write_text(str(soup), encoding="utf-8")
    except Exception as exc:
        result["status"] = f"error: {exc}"
        logger.error(f"{path}: {exc}")
    result["time"] = time.perf_counter() - t0
    return result


def _inline_css_file(path: Path, timeout: int) -> dict[str, Any]:
    result = {
        "path": str(path),
        "local": 0,
        "remote": 0,
        "time": 0.0,
        "status": "success",
    }
    t0 = time.perf_counter()
    try:
        css = path.read_text(encoding="utf-8")
        css, l, r = _inline_urls_in_css(css, path, None, timeout)
        path.write_text(css, encoding="utf-8")
        result["local"], result["remote"] = l, r
    except Exception as exc:
        result["status"] = f"error: {exc}"
        logger.error(f"{path}: {exc}")
    result["time"] = time.perf_counter() - t0
    return result


def _inline_dispatch(args: tuple[Path, int]) -> dict[str, Any]:
    path, timeout = args
    if path.suffix.lower() == ".html":
        return _inline_html_file(path, timeout)
    if path.suffix.lower() == ".css":
        return _inline_css_file(path, timeout)
    return {
        "path": str(path),
        "local": 0,
        "remote": 0,
        "time": 0.0,
        "status": "skipped",
    }


def cmd_inline(args: argparse.Namespace) -> int:
    files: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() in (".html", ".css"):
            files.append(p)
        elif p.is_dir():
            files.extend(p.rglob("*.html"))
            files.extend(p.rglob("*.css"))
    files = list({p.resolve(): p for p in files}.values())
    if not files:
        logger.warning("No HTML or CSS files found.")
        return 1
    print(f"Processing {len(files)} files with {MAX_WORKERS} workers...\n")
    total_l = total_r = 0
    t0 = time.perf_counter()
    payload = [(p, args.timeout) for p in files]
    with Pool(processes=MAX_WORKERS) as pool:
        results = pool.map(_inline_dispatch, payload)
    for r in results:
        path = Path(r["path"])
        try:
            shown = path.relative_to(Path.cwd())
        except ValueError:
            shown = path
        total_l += r["local"]
        total_r += r["remote"]
        if r["status"] == "success":
            print(f"[SUCCESS] {shown} ({r['time']:.2f}s) - Embedded: {r['local']} local, {r['remote']} remote")
        elif r["status"] != "skipped":
            logger.error(f"[ERROR] {shown} - {r['status']}")
    print(f"\nBuild Complete in {time.perf_counter() - t0:.2f}s!")
    print(f"Total globally embedded resources: {total_l} local, {total_r} remote.")
    return 0


class _Isolator:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.embedded = 0
        self.warnings = 0

    def log(self, msg: str, level: str = "INFO") -> None:
        if self.verbose or level == "ERROR":
            print(f"[{level}] {msg}")

    def _find(self, ref: str, base: Path) -> Path | None:
        url_p = urlparse(ref)
        stripped = url_p.path.lstrip("/")
        for root in (
            base,
            Path("/sdcard/_static"),
            Path.cwd(),
            base.parent.parent,
            base.parent,
        ):
            root = root.resolve()
            for cand in (root / ref, root / stripped, root / Path(ref).name):
                if cand.exists():
                    return cand
        self.log(f"Resource '{ref}' not found", "WARNING")
        self.warnings += 1
        return None

    def _embed_css_urls(self, css: str, base: Path) -> str:
        if not css:
            return css

        def repl(m: re.Match) -> str:
            ref = m.group(1)
            if ref.startswith(("http://", "https://", "data:")):
                return m.group(0)
            p = self._find(ref, base)
            if not p:
                return m.group(0)
            uri = to_data_uri(p.read_bytes(), guess_mime(str(p)))
            self.embedded += 1
            self.log(f"Embedded font: {p.name}")
            return f"url('{uri}')"

        return CSS_URL_RE.sub(repl, css)

    def _img(self, tag: Tag, base: Path) -> None:
        src = tag.get("src")
        if not src or src.startswith(("http://", "https://", "data:")):
            return
        p = self._find(src, base)
        if not p:
            tag.decompose()
            return
        tag["src"] = to_data_uri(p.read_bytes(), guess_mime(str(p)))
        self.embedded += 1
        self.log(f"Embedded image: {p.name}")

    def _link(self, tag: Tag, base: Path) -> None:
        if tag.get("rel") != ["stylesheet"]:
            return
        href = tag.get("href")
        if not href or href.startswith(("http://", "https://", "data:")):
            return
        p = self._find(href, base)
        if not p:
            tag.decompose()
            return
        css = self._embed_css_urls(p.read_text(encoding="utf-8"), p.parent)
        s = BeautifulSoup("", "html.parser").new_tag("style")
        s.string = css
        tag.replace_with(s)
        self.log(f"Embedded CSS: {p.name}")

    def _script(self, tag: Tag, base: Path) -> None:
        src = tag.get("src")
        if not src:
            return
        if src.startswith(("http://", "https://")):
            self.log(f"Removing external script: {src}")
            tag.decompose()
            return
        p = self._find(src, base)
        if not p:
            tag.decompose()
            return
        tag.string = p.read_text(encoding="utf-8")
        del tag["src"]
        self.log(f"Embedded script: {p.name}")

    def _style(self, tag: Tag, base: Path) -> None:
        if tag.string:
            tag.string = self._embed_css_urls(tag.string, base)

    def isolate(self, html_path: Path) -> str | None:
        try:
            text = html_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.log(f"Read error {html_path}: {exc}", "ERROR")
            return None
        soup = BeautifulSoup(text, "html.parser")
        base = html_path.parent
        for img in soup.find_all("img"):
            self._img(img, base)
        for link in soup.find_all("link"):
            self._link(link, base)
        for script in soup.find_all("script"):
            self._script(script, base)
        for style in soup.find_all("style"):
            self._style(style, base)
        self.log(f"Embedded {self.embedded}, warnings {self.warnings}")
        return soup.prettify()

    def save(self, html_path: Path, out_path: Path | None = None) -> bool:
        html = self.isolate(html_path)
        if html is None:
            return False
        if out_path is None:
            out_path = html_path.with_name(html_path.stem + "_standalone.html")
        out_path.write_text(html, encoding="utf-8")
        self.log(f"Standalone HTML saved to: {out_path}")
        return True


def cmd_isolate(args: argparse.Namespace) -> int:
    inp = Path(args.input)
    if not inp.exists():
        print(f"Error: input file not found: {inp}")
        return 1
    out = Path(args.output) if args.output else inp.with_name(inp.stem + "_standalone.html")
    iso = _Isolator(verbose=args.verbose)
    return 0 if iso.save(inp, out) else 1


_STANDALONE_ASSET_CACHE: dict[str, tuple[bytes, str]] = {}


def _init_standalone_worker(cache: dict[str, tuple[bytes, str]]) -> None:
    global _STANDALONE_ASSET_CACHE
    _STANDALONE_ASSET_CACHE = cache


def _resolve_url(ref: str, base: Path | str) -> str:
    ref = ref.strip()
    ref, _ = urldefrag(ref)
    if ref.startswith("//"):
        return "https:" + ref
    if ref.startswith(("http://", "https://", "data:", "#")):
        return ref
    if isinstance(base, Path):
        if ref.startswith("/"):
            return str(Path(ref).resolve())
        return str((base / unquote(ref)).resolve())
    return urljoin(str(base), ref)


def _scan_css_refs(css: str, base: Path | str, urls: set[str]) -> None:
    for m in CSS_URL_RE.finditer(css):
        ref = m.group(2).strip().strip("'\"")
        if not ref or ref.startswith(("data:", "#")):
            continue
        full = _resolve_url(ref, base)
        if is_remote(full):
            urls.add(full)


def _collect_remote_urls(html_path: Path) -> set[str]:
    urls: set[str] = set()
    css_queue: list[tuple[str, Path | str]] = []
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8-sig", errors="replace"), "html.parser")
    except OSError:
        return urls
    for link in soup.find_all("link", href=True):
        rel = link.get("rel", [])
        rel = rel if isinstance(rel, list) else [rel]
        if "stylesheet" in {r.lower() for r in rel if r}:
            full = _resolve_url(link["href"], html_path.parent)
            if is_remote(full):
                urls.add(full)
                css_queue.append((full, full))
            else:
                p = Path(full)
                if p.is_file():
                    css_queue.append((str(p), p.parent))
    for tag in soup.find_all(["img", "source"]):
        if tag.get("src"):
            full = _resolve_url(tag["src"], html_path.parent)
            if is_remote(full) and not is_image_url(full):
                urls.add(full)
        if tag.get("srcset"):
            for entry in tag["srcset"].split(","):
                ref = entry.strip().split()[0] if entry.strip() else ""
                if ref:
                    full = _resolve_url(ref, html_path.parent)
                    if is_remote(full) and not is_image_url(full):
                        urls.add(full)
    for sc in soup.find_all("script", src=True):
        full = _resolve_url(sc["src"], html_path.parent)
        if is_remote(full):
            urls.add(full)
    for st in soup.find_all("style"):
        _scan_css_refs(st.get_text(), html_path.parent, urls)
    for tag in soup.find_all(style=True):
        _scan_css_refs(tag["style"], html_path.parent, urls)
    seen_css: set[str] = set()
    while css_queue:
        ref, _base = css_queue.pop()
        if ref in seen_css:
            continue
        seen_css.add(ref)
        try:
            if is_remote(ref):
                body = fetch_remote(ref, 30)
                css = body.decode("utf-8", errors="replace") if body else ""
                b = ref
            else:
                css = Path(ref).read_text(encoding="utf-8", errors="replace")
                b = Path(ref).parent
        except Exception:
            continue
        _scan_css_refs(css, b, urls)
        for m in CSS_IMPORT_RE.finditer(css):
            full = _resolve_url(m.group(1), b)
            if is_remote(full):
                urls.add(full)
                css_queue.append((full, full))
            elif isinstance(b, Path):
                p = Path(full)
                if p.is_file():
                    css_queue.append((str(p), p.parent))
    return urls


def _download_remote_batch(
    urls: Iterable[str], timeout: int, max_size: int, prompt: bool
) -> dict[str, tuple[bytes, str]]:
    cache: dict[str, tuple[bytes, str]] = {}
    for url in sorted(set(urls)):
        if is_image_url(url):
            print(f"  ⊘ skipped remote image: {url}")
            continue
        try:
            r = requests.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ⚠ failed to download {url}: {exc}")
            continue
        body = r.content
        if len(body) >= max_size and prompt:
            mb = len(body) / (1024 * 1024)
            print(f"\n⚠ Remote file is {mb:.2f} MiB:\n  {url}")
            ans = input("Download it? [y/N]: ").strip().lower()
            if ans not in {"y", "yes"}:
                print(f"  ⊘ skipped by user: {url}")
                continue
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip() or guess_mime(url)
        cache[url] = (body, mime)
        cache[str(r.url)] = (body, mime)
        print(f"  ↓ downloaded once: {url}")
    return cache


def _asset_bytes(ref: str, base: Path | str, cache: dict[str, tuple[bytes, str]]) -> tuple[bytes, str] | None:
    if not ref or ref.startswith(("data:", "#")):
        return None
    full = _resolve_url(ref, base)
    if is_remote(full):
        if is_image_url(full):
            print(f"  ⊘ skipped remote image: {ref}")
            return None
        return cache.get(full)
    p = Path(full)
    if not p.is_file():
        print(f"  ⚠ local file not found: {p}")
        return None
    return p.read_bytes(), guess_mime(str(p))


def _rewrite_css(css: str, base: Path | str, cache: dict[str, tuple[bytes, str]]) -> str:
    def import_repl(m: re.Match) -> str:
        ref = m.group(1).strip()
        got = _asset_bytes(ref, base, cache)
        if not got:
            return m.group(0)
        body, _ = got
        inner_base: Path | str = base
        if is_remote(_resolve_url(ref, base)):
            inner_base = Path()
        else:
            inner_base = Path(_resolve_url(ref, base)).parent
        return _rewrite_css(body.decode("utf-8", errors="replace"), inner_base, cache)

    css = CSS_IMPORT_RE.sub(import_repl, css)

    def url_repl(m: re.Match) -> str:
        ref = m.group(2).strip()
        if ref.startswith(("#", "data:")):
            return m.group(0)
        got = _asset_bytes(ref, base, cache)
        if not got:
            return m.group(0)
        body, mime = got
        return f'url("{to_data_uri(body, mime)}")'

    return CSS_URL_RE.sub(url_repl, css)


def _process_standalone_html(html_str: str) -> bool:
    html_path = Path(html_str).resolve()
    base = html_path.parent
    cache = _STANDALONE_ASSET_CACHE
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8-sig", errors="replace"), "html.parser")
    except OSError as exc:
        print(f"ERROR: cannot read {html_path}: {exc}")
        return False
    print(f"Processing: {html_path}")
    for link in soup.find_all("link", rel=True):
        rel = link.get("rel") or []
        rel = rel if isinstance(rel, list) else [rel]
        rel_set = {str(r).lower() for r in rel if r}
        if "stylesheet" not in rel_set:
            continue
        href = link.get("href")
        got = _asset_bytes(href, base, cache)
        if not got:
            continue
        body, _ = got
        css = _rewrite_css(body.decode("utf-8", errors="replace"), base, cache)
        s = soup.new_tag("style")
        s.string = css
        link.replace_with(s)
    for sc in soup.find_all("script", src=True):
        got = _asset_bytes(sc["src"], base, cache)
        if not got:
            continue
        js = got[0].decode("utf-8", errors="replace")
        js = re.sub(r"\n?//#\s*sourceMappingURL=.*", "", js)
        js = re.sub(r"</script", r"<\\/script", js, flags=re.IGNORECASE)
        del sc["src"]
        sc.string = js
    for img in soup.find_all("img", src=True):
        got = _asset_bytes(img["src"], base, cache)
        if got:
            img["src"] = to_data_uri(got[0], got[1])
    for tag in soup.find_all(srcset=True):
        parts = []
        for entry in tag["srcset"].split(","):
            entry = entry.strip()
            if not entry:
                continue
            bits = entry.split()
            ref, rest = bits[0], " ".join(bits[1:])
            got = _asset_bytes(ref, base, cache)
            if not got:
                parts.append(entry)
                continue
            uri = to_data_uri(got[0], got[1])
            parts.append(f"{uri} {rest}".strip())
        tag["srcset"] = ", ".join(parts)
    for st in soup.find_all("style"):
        css = st.get_text() or ""
        if css:
            css = re.sub(r"^\s*<!--\s*", "", css)
            css = re.sub(r"\s*-->\s*$", "", css)
            st.string = _rewrite_css(css, base, cache)
    for tag in soup.find_all(style=True):
        if tag["style"]:
            tag["style"] = _rewrite_css(tag["style"], base, cache)
    try:
        html_path.write_text(str(soup), encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot write {html_path}: {exc}")
        return False
    print(f"✓ Done: {html_path}")
    return True


def cmd_standalone(args: argparse.Namespace) -> int:
    paths = [Path(p).resolve() for p in (args.paths or ["."])]
    files: set[Path] = set()
    for p in paths:
        if p.is_file() and p.suffix.lower() in HTML_EXTS:
            files.add(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in HTML_EXTS:
                    files.add(f)
        else:
            print(f"⚠ input does not exist: {p}")
    files = sorted(files)
    if not files:
        print("No HTML files found.")
        return 1
    print(f"Found {len(files)} HTML file(s).")
    print("Scanning for remote assets...")
    all_urls: set[str] = set()
    for f in files:
        all_urls.update(_collect_remote_urls(f))
    print(f"Found {len(all_urls)} unique remote asset URL(s).")
    cache = _download_remote_batch(all_urls, args.timeout, args.max_size, not args.no_prompt)
    print(f"Cached {len(cache)} remote asset reference(s).")
    print(f"Processing with {MAX_WORKERS} workers...")
    with Pool(processes=MAX_WORKERS, initializer=_init_standalone_worker, initargs=(cache,)) as pool:
        results = pool.map(_process_standalone_html, [str(f) for f in files])
    ok = sum(bool(r) for r in results)
    print(f"\nProcessed {ok}/{len(files)} file(s).")
    return 0 if ok == len(files) else 1


def _decode_data_uri_to_file(uri: str, dest_dir: Path) -> str | None:
    dec = decode_data_uri(uri)
    if not dec:
        return None
    body, mime = dec
    ext = None
    m = re.match(r"^[^/]+/([^;\s]+)", mime)
    if m:
        ext = m.group(1)
    if ext == "svg+xml":
        ext = "svg"
    fname = f"data_resource_{abs(hash(uri)) % 10**8}.{ext or 'bin'}"
    (dest_dir / fname).write_bytes(body)
    return fname


def cmd_mhtml(args: argparse.Namespace) -> int:
    inputs = args.inputs or [str(p) for p in Path.cwd().rglob("*.mhtml")]
    if not inputs:
        print("No .mhtml inputs given and none found in cwd.")
        return 1
    for raw in inputs:
        mhtml_path = Path(raw)
        if not mhtml_path.is_file():
            print(f"Skipping non-file: {mhtml_path}")
            continue
        out_html = Path(args.output) if args.output else mhtml_path.with_suffix(".html")
        out_files = Path(args.files_dir) if args.files_dir else mhtml_path.with_name(mhtml_path.stem + "_files")
        out_files.mkdir(parents=True, exist_ok=True)
        msg = BytesParser(policy=policy.default).parsebytes(mhtml_path.read_bytes())
        parts: list[Any] = []

        def walk(m: Any) -> None:
            for p in m.iter_parts():
                parts.append(p)
                if p.is_multipart():
                    walk(p)

        if msg.is_multipart():
            walk(msg)
        else:
            parts = [msg]
        html_parts: list[tuple[str | None, bytes]] = []
        other: list[tuple[str | None, str, bytes]] = []
        for p in parts:
            ctype = p.get_content_type()
            cid = (p.get("Content-ID") or "").strip()
            if cid.startswith("<") and cid.endswith(">"):
                cid = cid[1:-1]
            body = p.get_payload(decode=True)
            if ctype == "text/html":
                html_parts.append((cid, body))
            elif body:
                other.append((cid, ctype, body))
        if not html_parts:
            for p in parts:
                if p.get_content_type().startswith("text/"):
                    b = p.get_payload(decode=True)
                    if b:
                        html_parts.append((None, b))
                        break
        if not html_parts:
            print(f"No HTML part in {mhtml_path}")
            continue
        html_text = html_parts[0][1].decode(errors="replace")
        cid_map: dict[str, str] = {}
        for cid, ctype, body in other:
            if ctype == "text/html":
                continue
            ext = None
            m = re.match(r"^[^/]+/([^;\s]+)", ctype)
            if m:
                ext = m.group(1)
            if ext == "svg+xml":
                ext = "svg"
            base_name = safe_filename(cid or "resource")
            fname = base_name if ext is None or Path(base_name).suffix else f"{base_name}.{ext}"
            target = out_files / fname
            if target.exists():
                target = unique_path(target)
                fname = target.name
            target.write_bytes(body)
            if cid:
                cid_map[cid] = fname

        def cid_repl(m: re.Match) -> str:
            attr, cid = m.group(1), m.group(2)
            if cid in cid_map:
                return f'{attr}="{out_files.name}/{cid_map[cid]}"'
            return m.group(0)

        html_text = CID_RE.sub(cid_repl, html_text)

        def data_repl(m: re.Match) -> str:
            attr, uri = m.group(1), m.group(2)
            fname = _decode_data_uri_to_file(uri, out_files)
            if not fname:
                return m.group(0)
            return f'{attr}="{out_files.name}/{fname}"'

        html_text = SRC_HREF_DATA_URI_RE.sub(data_repl, html_text)
        out_html = unique_path(out_html) if out_html.exists() else out_html
        out_html.write_text(html_text, encoding="utf-8")
        print(f"Done: {out_html}  ({len(cid_map)} CID items)")
    return 0


def _find_in_static(filename: str, static_root: Path) -> Path | None:
    if not static_root.is_dir():
        return None
    for root, _, files in os.walk(static_root):
        if filename in files:
            return Path(root) / filename
    return None


def _font_mime(ext: str) -> str | None:
    return {
        ".eot": "application/vnd.ms-fontobject",
        ".ttf": "application/font-sfnt",
        ".woff": "application/font-woff",
        ".woff2": "font/woff2",
        ".svg": "image/svg+xml",
    }.get(ext)


def _font_to_data_uri(path_or_url: str, static_root: Path, timeout: int) -> str | None:
    ext = Path(urlparse(path_or_url).path).suffix.lower()
    mime = _font_mime(ext)
    if not mime:
        return None
    name = Path(path_or_url).name
    local = _find_in_static(name, static_root)
    if local:
        print(f"Found local font: {name} at {local}")
        return to_data_uri(local.read_bytes(), mime)
    if is_remote(path_or_url):
        body = fetch_remote(path_or_url, timeout)
    else:
        p = Path(path_or_url)
        if p.is_file():
            body = p.read_bytes()
        else:
            body = fetch_remote("file:///" + str(p.resolve()), timeout) if False else None
    if not body:
        return None
    return to_data_uri(body, mime)


def cmd_css(args: argparse.Namespace) -> int:
    inp = Path(args.input).resolve()
    if not inp.is_file():
        print(f"Error: input CSS not found: {inp}")
        return 1
    out = Path(args.output) if args.output else inp.with_name(inp.stem + "_standalone.css")
    css = inp.read_text(encoding="utf-8")
    static_root = Path(args.static_root)
    imports = [m.group(1) for m in CSS_IMPORT_RE.finditer(css)]
    for imp in imports:
        css = css.replace(f'@import url("{imp}");', "")
    merged = css
    seen_imports: set[str] = set()
    queue = list(imports)
    while queue:
        ref = queue.pop(0)
        if ref in seen_imports:
            continue
        seen_imports.add(ref)
        sub_path: Path | str
        if is_remote(ref):
            body = fetch_remote(ref, args.timeout)
            if not body:
                continue
            sub_css = body.decode("utf-8", errors="replace")
            sub_path = ref
        else:
            p = (inp.parent / ref).resolve()
            if not p.is_file():
                continue
            sub_css = p.read_text(encoding="utf-8")
            sub_path = p
        for m in CSS_IMPORT_RE.finditer(sub_css):
            queue.append(m.group(1))
        sub_css = CSS_IMPORT_RE.sub("", sub_css)
        merged += f"\n/* Imported from: {sub_path} */\n{sub_css}\n"

    def font_repl(m: re.Match) -> str:
        quote, ref = m.group(1), m.group(2)
        base = inp.parent if not is_remote(ref) else None
        full = ref if is_remote(ref) else str((base / ref).resolve())
        uri = _font_to_data_uri(full, static_root, args.timeout)
        if not uri:
            return m.group(0)
        return f"url({quote}{uri}{quote})" if quote else f'url("{uri}")'

    merged = CSS_URL_RE.sub(font_repl, merged)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(merged, encoding="utf-8")
    print(f"Standalone CSS file created at: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="htmltool.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bundle", help="Extract all assets and build single_page_local.html")
    b.add_argument("--output-dir", default="output")
    b.add_argument("--assets-dir", default="assets")
    b.add_argument("--timeout", type=int, default=10)
    b.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Anonymously/1.0 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )
    b.add_argument(
        "--no-remote-scripts",
        dest="remote_scripts",
        action="store_false",
        default=True,
        help="Do NOT download remote <script src>",
    )
    b.set_defaults(func=cmd_bundle)
    i = sub.add_parser("inline", help="Inline local/remote assets into HTML/CSS files in place")
    i.add_argument("paths", nargs="*", default=["."])
    i.add_argument("--timeout", type=int, default=15)
    i.set_defaults(func=cmd_inline)
    s = sub.add_parser("isolate", help="Produce a <name>_standalone.html with local assets embedded")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_isolate)
    st = sub.add_parser("standalone", help="Multi-process standalone builder with remote cache")
    st.add_argument("paths", nargs="*")
    st.add_argument("--timeout", type=int, default=30)
    st.add_argument("--max-size", type=int, default=5 * 1024 * 1024)
    st.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip interactive prompt for large downloads",
    )
    st.set_defaults(func=cmd_standalone)
    m = sub.add_parser("mhtml", help="Convert .mhtml to .html + <stem>_files/ directory")
    m.add_argument("inputs", nargs="*", help="One or more .mhtml files (default: *.mhtml in cwd)")
    m.add_argument("-o", "--output", help="Output HTML path (single input only)")
    m.add_argument("--files-dir", help="Output directory for extracted resources")
    m.set_defaults(func=cmd_mhtml)
    c = sub.add_parser("css", help="Inline @import and url() fonts in a CSS file")
    c.add_argument("input")
    c.add_argument("-o", "--output")
    c.add_argument("--static-root", default="/sdcard/_static")
    c.add_argument("--timeout", type=int, default=15)
    c.set_defaults(func=cmd_css)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
