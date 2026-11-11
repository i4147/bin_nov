#!/data/data/com.termux/files/usr/bin/env python
"""Extract the base64 data URIs of CSS, JS and HTML files into asset files.
Subcommands: ``extract-cleanuri`` rewrites the ``data:`` URIs as relative paths, ``extract-images``/``extract-css``/``extract-assets``/``extract-elements``/``extract-html`` pull the embedded assets out with magic-byte MIME detection, and ``file-to-base64``/``ttf-to-base64`` do the opposite.
``--hash``, ``--report``, ``--dry-run`` and ``--download-remote`` control the output.
Usage: base64_extractor {extract-cleanuri,extract-images,extract-assets,...} [options] Positional arguments: file files ...
Subcommands: extract-cleanuri Extract data: URIs from css/js/html and rewrite.
extract-images Extract data:image/...;base64 URIs from files.
ttf-to-base64 Convert *.ttf files to base64 .txt files.
file-to-base64 Convert a single file to a base64 .txt.
extract-css Extract url(data:...;base64,...) from CSS files.
extract-assets Comprehensive extractor with magic-byte MIME detection.
extract-elements Extract all data:...;base64 blobs into flat dir.
extract-html HTML-only extractor (requires requests+bs4).
list-lines Report every line containing 'base64,'.
Options: --out --extensions File extensions to scan (default: .css .js .html) --hash --hash-len --pattern --force Overwrite existing .txt files --output --dry-run --no-magic Disable magic-byte MIME sniffing --files Explicit file list (overrides root scan) ...
plus 4 more; run ``base64_extractor --help`` for the full list.
Examples: $ base64_extractor extract-assets --root dist/ Command-line script: run ``python base64_extractor.py`` or the ``base64_extractor`` command.
The module is importable as well."""

import argparse
import base64 as _b64
import concurrent.futures as _cf
from dataclasses import dataclass
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator, Optional

from dh import MAX_WORKERS, SKIP_DIRS


MIME2EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/x-font-otf": ".otf",
    "application/json": ".json",
    "application/javascript": ".js",
    "text/css": ".css",
    "text/plain": ".txt",
    "text/html": ".html",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
    "application/octet-stream": ".bin",
}
TEXTY_EXT: set[str] = {
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".htm",
    ".ipynb",
    ".svg",
    ".json",
    ".txt",
    ".md",
    ".xml",
    ".vue",
    ".svelte",
    ".astro",
    ".mjs",
    ".cjs",
    ".scss",
    ".sass",
    ".less",
}
_B64: str = r"[A-Za-z0-9+/=\s]+"
DATA_URI_RE = re.compile(
    r"data:(?P<mime>[^;,)\s\"']+)(?:;[^,)\"']*)?;base64\s*,\s*(?P<data>" + _B64 + r")",
    re.IGNORECASE,
)
CSS_URL_DATA_RE = re.compile(
    r"""url\(\s*([\"']?)data:(?P<mime>[^;,)\s\"']+)(?:;charset=[^;]+)?;base64,\s*(?P<data>""" + _B64 + r""")\1\s*\)""",
    re.IGNORECASE | re.VERBOSE,
)
IMAGE_DATA_RE = re.compile(r"data:image/(?P<ext>[a-zA-Z0-9+.\-]+);base64,(?P<data>[A-Za-z0-9+/=\n\r]+)")


def guess_extension(mime: str) -> str:
    mime = (mime or "").lower().strip()
    if not mime:
        return ".bin"
    if mime in MIME2EXT:
        return MIME2EXT[mime]
    try:
        ext = mimetypes.guess_extension(mime)
        if ext:
            return ext
    except Exception:
        pass
    tail = mime.split("/")[-1].split(";")[0].strip()
    return f".{tail}" if tail else ".bin"


def hash_bytes(data: bytes, algo: str = "sha256", length: Optional[int] = None) -> str:
    h = hashlib.new(algo, data).hexdigest()
    return h[:length] if length else h


def safe_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def iter_files(
    root: Path,
    extensions: Iterable[str],
    skip_dirs: Iterable[str] = SKIP_DIRS,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    skip = {s.lower() for s in skip_dirs}

    def matches(p: Path) -> bool:
        return p.is_file() and p.suffix.lower() in exts

    if root.is_file():
        if matches(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        base = Path(dirpath)
        for name in filenames:
            p = base / name
            if matches(p):
                yield p


def read_text_safe(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"WARNING: cannot read {path}: {e}", file=sys.stderr)
        return None


def detect_mime_from_bytes(
    blob: bytes,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not blob or len(blob) < 4:
        return (None, None, None)
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("image/png", ".png", "images")
    if blob.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg", ".jpg", "images")
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return ("image/gif", ".gif", "images")
    if blob.startswith(b"RIFF") and len(blob) > 12 and blob[8:12] == b"WEBP":
        return ("image/webp", ".webp", "images")
    if b"<?xml" in blob[:100] or b"<svg" in blob[:100]:
        return ("image/svg+xml", ".svg", "images")
    if blob.startswith(b"wOF2"):
        return ("font/woff2", ".woff2", "fonts")
    if blob.startswith(b"wOFF"):
        return ("font/woff", ".woff", "fonts")
    if blob.startswith((b"\x00\x01\x00\x00", b"true")):
        return ("font/ttf", ".ttf", "fonts")
    if blob.startswith(b"OTTO"):
        return ("font/otf", ".otf", "fonts")
    if blob.startswith((b"{", b"[")):
        return ("application/json", ".json", "data")
    if blob.startswith(b"\x00\x00\x01\x00"):
        return ("image/x-icon", ".ico", "images")
    if blob.startswith(b"BM"):
        return ("image/bmp", ".bmp", "images")
    if blob.startswith((b"\x00\x00\x00\x18ftypmp42", b"\x00\x00\x00 ftypmp42")):
        return ("video/mp4", ".mp4", "videos")
    sample = blob[:256]
    if sample:
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        if printable / max(1, len(sample)) > 0.8:
            return ("text/plain", ".txt", "data")
    return (None, None, None)


def decode_b64(data: str) -> Optional[bytes]:
    cleaned = re.sub(r"\s+", "", data)
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned += "=" * pad
    try:
        return _b64.b64decode(cleaned, validate=False)
    except Exception:
        return None


def relurl(target: Path, from_dir: Path) -> str:
    try:
        return target.relative_to(from_dir).as_posix()
    except ValueError:
        return Path(Path(target).relative_to(from_dir)).as_posix()


def cmd_extract_cleanuri(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    algo = args.hash
    digest_len = args.hash_len
    seen: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        uri = match.group(0)
        mime = match.group("mime") or None
        b64 = match.group("data")
        key = hashlib.new(algo, uri.encode("utf-8")).hexdigest()
        if key in seen:
            name = seen[key]
        else:
            ext = guess_extension(mime) if mime else ".bin"
            name = f"{key[:digest_len]}{ext}"
            payload = decode_b64(b64)
            if payload is None:
                print(f"WARNING: base64 decode failed for {key} — keeping original")
                return uri
            target = out / name
            if not target.exists():
                target.write_bytes(payload)
                print(f"OK   saved asset: {target}")
            seen[key] = name
        return (
            relurl(out / name, match.string_dir)
            if False
            else (Path(Path(out / name).relative_to(match.string_dir)).as_posix())
        )

    changed_files = 0
    exts = args.extensions or [".css", ".js", ".html"]
    for f in iter_files(root, exts):
        text = read_text_safe(f)
        if text is None:
            continue

        def repl(m: re.Match, _f: Path = f) -> str:
            uri = m.group(0)
            mime = m.group("mime") or None
            b64 = m.group("data")
            key = hashlib.new(algo, uri.encode("utf-8")).hexdigest()
            if key in seen:
                name = seen[key]
            else:
                ext = guess_extension(mime) if mime else ".bin"
                name = f"{key[:digest_len]}{ext}"
                payload = decode_b64(b64)
                if payload is None:
                    print(f"WARNING: base64 decode failed in {_f} — keeping original")
                    return uri
                target = out / name
                if not target.exists():
                    target.write_bytes(payload)
                    print(f"OK   saved asset: {target}")
                seen[key] = name
            return relurl(out / name, _f.parent)

        new_text = DATA_URI_RE.sub(repl, text)
        if new_text != text:
            if args.dry_run:
                print(f"DRY  would update {f}")
            else:
                f.write_text(new_text, encoding="utf-8")
                print(f"EDIT updated {f}")
            changed_files += 1
    print(f"Done. Files changed: {changed_files}, unique assets: {len(seen)}")
    return 0


def cmd_extract_images(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    exts = args.extensions or [".ipynb", ".js", ".html"]
    total = 0
    for f in iter_files(root, exts):
        text = read_text_safe(f)
        if text is None:
            continue
        per_file = 0
        for m in IMAGE_DATA_RE.finditer(text):
            ext = m.group("ext").lower()
            payload = decode_b64(m.group("data"))
            if payload is None:
                continue
            digest = hash_bytes(payload, "sha1", length=12)
            name = f"{f.stem}_{digest}.{ext}"
            target = out / name
            if args.dry_run:
                print(f"DRY  would write {target}")
            else:
                target.write_bytes(payload)
            per_file += 1
        if per_file:
            print(f"IMG  extracted {per_file} image(s) from {f}")
        total += per_file
    print(f"\nExtraction complete. Total images saved: {total}")
    return 0


def cmd_ttf_to_base64(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    pattern = args.pattern
    for f in sorted(root.glob(pattern)):
        if not f.is_file():
            continue
        out = f.with_suffix(".txt")
        if out.exists() and not args.force:
            print(f"SKIP {out.name} exists")
            continue
        b64 = _b64.b64encode(f.read_bytes()).decode("utf-8")
        if args.dry_run:
            print(f"DRY  would write {out}")
        else:
            out.write_text(b64, encoding="utf-8")
            print(f"OK   {f.name} -> {out.name}")
    return 0


def cmd_file_to_base64(args: argparse.Namespace) -> int:
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(f"ERROR: not a file: {src}", file=sys.stderr)
        return 2
    out = src.with_suffix(".txt") if not args.output else Path(args.output)
    if out.exists() and not args.force:
        print(f"{out.name} exists. remove and run again (or pass --force)")
        return 0
    b64 = _b64.b64encode(src.read_bytes()).decode("utf-8")
    if args.dry_run:
        print(f"DRY  would write {out}")
    else:
        out.write_text(b64, encoding="utf-8")
        print(f"{out.name} created.")
    return 0


def cmd_extract_css(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    algo = args.hash
    digest_len = args.hash_len
    total = 0
    files = [Path(p) for p in args.files]
    if not files:
        print("No CSS files given", file=sys.stderr)
        return 2
    for css in files:
        if not css.is_file():
            print(f"skip: {css}")
            continue
        text = read_text_safe(css)
        if text is None:
            continue
        seen: dict[str, str] = {}

        def repl(m: re.Match, _css: Path = css) -> str:
            mime = m.group("mime")
            payload = decode_b64(m.group("data"))
            if payload is None:
                return m.group(0)
            digest = hash_bytes(payload, algo, length=digest_len)
            if digest not in seen:
                ext = guess_extension(mime)
                name = f"asset-{digest}{ext}"
                target = out / name
                if not args.dry_run and not target.exists():
                    target.write_bytes(payload)
                seen[digest] = name
            return f"url('{out.name}/{seen[digest]}')"

        new_text = CSS_URL_DATA_RE.sub(repl, text)
        if new_text != text and not args.dry_run:
            css.write_text(new_text, encoding="utf-8")
        print(f"{css}: extracted {len(seen)} assets")
        total += len(seen)
    print(f"\nTotal saved assets: {total}")
    print(f"Output directory: ./{out}")
    return 0


_ASSET_PATTERNS: dict[str, re.Pattern[str]] = {
    "url": re.compile(
        r"""url\(\s*["']?data:(?P<mime>[^;,)\s"']+)(?:;[^,)]*)?;base64\s*,\s*(?P<data>""" + _B64 + r""")["']?\s*\)""",
        re.IGNORECASE | re.VERBOSE,
    ),
    "src": re.compile(
        r"""\bsrc\s*=\s*["']data:(?P<mime>[^;,]+)(?:;[^,]*)?;base64\s*,\s*(?P<data>""" + _B64 + r""")["']""",
        re.IGNORECASE | re.VERBOSE,
    ),
    "href": re.compile(
        r"""\bhref\s*=\s*["']data:(?P<mime>[^;,]+)(?:;[^,]*)?;base64\s*,\s*(?P<data>""" + _B64 + r""")["']""",
        re.IGNORECASE | re.VERBOSE,
    ),
    "data_uri": DATA_URI_RE,
    "background": re.compile(
        r"""\bbackground(?:-image)?\s*:\s*url\(\s*["']?data:(?P<mime>[^;,)\s"']+)(?:;[^,)]*)?;base64\s*,\s*(?P<data>"""
        + _B64
        + r""")["']?\s*\)""",
        re.IGNORECASE | re.VERBOSE,
    ),
}


@dataclass(slots=True)
class AssetHit:
    path: Path
    start: int
    end: int
    match_type: str
    mime: str
    data: str
    context: str


def find_asset_hits(text: str, path: Path) -> list[AssetHit]:
    hits: list[AssetHit] = []
    seen: set[str] = set()
    for kind, rx in _ASSET_PATTERNS.items():
        for m in rx.finditer(text):
            data = m.group("data")
            if len(data) < 64:
                continue
            key = hashlib.md5(data.encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                AssetHit(
                    path=path,
                    start=m.start(),
                    end=m.end(),
                    match_type=kind,
                    mime=m.group("mime"),
                    data=data,
                    context=m.group(0),
                )
            )
    return hits


def _process_asset_file(
    path: Path,
    assets_dir: Path,
    algo: str,
    digest_len: int,
    use_magic: bool,
    dry_run: bool,
) -> tuple[int, int]:
    text = read_text_safe(path)
    if text is None:
        return (0, 0)
    hits = find_asset_hits(text, path)
    if not hits:
        return (0, 0)
    replacements: list[tuple[str, str]] = []
    extracted = 0
    for hit in hits:
        payload = decode_b64(hit.data)
        if payload is None:
            continue
        mime, ext, category = (None, None, None)
        if use_magic:
            mime, ext, category = detect_mime_from_bytes(payload)
        if not mime:
            mime = hit.mime
        if not ext:
            ext = guess_extension(mime)
        if not category:
            if mime.startswith("image/"):
                category = "images"
            elif mime.startswith("font/") or "font" in mime:
                category = "fonts"
            elif mime.startswith("video/"):
                category = "videos"
            else:
                category = "data"
        digest = hash_bytes(payload, algo, length=digest_len)
        name = f"{digest}{ext}"
        target = assets_dir / category / name
        if not target.exists():
            if dry_run:
                print(f"DRY  would write {target} ({len(payload)} bytes)")
            else:
                safe_write_bytes(target, payload)
            extracted += 1
        url = relurl(target, path.parent)
        if path.suffix.lower() == ".css":
            repl = f"url('{url}')"
        elif path.suffix.lower() in {".html", ".htm"}:
            if "href=" in hit.context:
                repl = f'href="{url}"'
            else:
                repl = f'src="{url}"'
        else:
            repl = f'"{url}"'
        replacements.append((hit.context, repl))
    if replacements and not dry_run:
        new_text = text
        for old, new in replacements:
            new_text = new_text.replace(old, new)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
    return extracted, len(replacements)


def cmd_extract_assets(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    assets_dir = Path(args.out).resolve()
    assets_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("images", "fonts", "videos", "data"):
        (assets_dir / sub).mkdir(exist_ok=True)
    exts = args.extensions or [".html", ".css", ".js", ".jsx", ".tsx", ".ts"]
    files = list(iter_files(root, exts, skip_dirs=SKIP_DIRS))
    total_extracted = 0
    total_replaced = 0
    processed = 0
    if MAX_WORKERS > 1 and len(files) > 1:
        with _cf.ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _process_asset_file,
                    f,
                    assets_dir,
                    args.hash,
                    args.hash_len,
                    not args.no_magic,
                    args.dry_run,
                ): f
                for f in files
            }
            for fut in _cf.as_completed(futures):
                f = futures[fut]
                try:
                    e, r = fut.result()
                except Exception as exc:
                    print(f"ERROR {f}: {exc}", file=sys.stderr)
                    continue
                total_extracted += e
                total_replaced += r
                processed += 1
                if e or r:
                    print(f"{f.name}: extracted={e}, replaced={r}")
    else:
        for f in files:
            e, r = _process_asset_file(
                f,
                assets_dir,
                args.hash,
                args.hash_len,
                not args.no_magic,
                args.dry_run,
            )
            total_extracted += e
            total_replaced += r
            processed += 1
            if e or r:
                print(f"{f.name}: extracted={e}, replaced={r}")
    print("=" * 60)
    print(f"Files scanned    : {len(files)}")
    print(f"Files processed  : {processed}")
    print(f"Assets extracted : {total_extracted}")
    print(f"Replacements     : {total_replaced}")
    print(f"Assets saved to  : {assets_dir}")
    return 0


def cmd_extract_elements(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    algo = args.hash
    digest_len = args.hash_len
    files = [Path(p) for p in args.files] if args.files else list(iter_files(root, args.extensions or TEXTY_EXT))
    seen: set[str] = set()
    count = 0
    for f in files:
        text = read_text_safe(f)
        if text is None:
            continue
        for m in DATA_URI_RE.finditer(text):
            mime = m.group("mime")
            payload = decode_b64(m.group("data"))
            if payload is None:
                continue
            digest = hash_bytes(payload, algo, length=digest_len)
            if digest in seen:
                continue
            seen.add(digest)
            ext = guess_extension(mime)
            target = out / f"{digest}{ext}"
            if not target.exists() and not args.dry_run:
                target.write_bytes(payload)
            count += 1
    print(f"{count} elements extracted.")
    return 0


def cmd_extract_html(args: argparse.Namespace) -> int:
    try:
        from bs4 import BeautifulSoup
        import requests
    except ImportError as e:
        print(
            "extract-html requires 'requests' and 'beautifulsoup4'. "
            f"Install with: pip install requests beautifulsoup4 ({e})",
            file=sys.stderr,
        )
        return 3
    from bs4 import BeautifulSoup
    import requests

    root = Path(args.root).resolve()
    output = Path(args.out).resolve()
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    download_remote = args.download_remote
    timeout = args.timeout
    _counter = {"n": 0}

    def save_asset(blob: bytes, mime_or_name: str, prefix: str) -> Path:
        ext = mimetypes.guess_extension(mime_or_name) or ""
        while True:
            name = f"{prefix}_{_counter['n']}{ext}"
            _counter["n"] += 1
            target = assets / name
            if not target.exists():
                break
        target.write_bytes(blob)
        return target

    def extract_data_uri(s: str, prefix: str) -> Optional[Path]:
        m = re.match(r"data:(.*?);base64,(.*)", s, re.DOTALL)
        if not m:
            return None
        mime, payload = m.groups()
        blob = decode_b64(payload)
        if blob is None:
            return None
        return save_asset(blob, mime.split(";")[0], prefix)

    def download(url: str, prefix: str) -> Optional[Path]:
        try:
            print(f"Downloading: {url}")
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                return None
            ctype = r.headers.get("Content-Type", "application/octet-stream")
            return save_asset(r.content, ctype.split(";")[0], prefix)
        except Exception as e:
            print(f"WARN: failed to download {url}: {e}")
            return None

    html_files = list(
        [Path(p) for p in args.files]
        if args.files
        else [p for p in iter_files(root, [".html", ".htm"]) if "output" not in p.parts]
    )
    for html in html_files:
        raw = read_text_safe(html)
        if raw is None:
            continue
        soup = BeautifulSoup(raw, "html.parser")
        stem = html.stem
        for i, tag in enumerate(soup.find_all("style")):
            if not tag.string:
                continue
            target = save_asset(tag.string.encode("utf-8"), "text/css", f"{stem}_style{i}")
            link = soup.new_tag("link", rel="stylesheet", href=str(target.relative_to(output)))
            tag.replace_with(link)
        for i, tag in enumerate(soup.find_all("script")):
            if tag.get("src"):
                src = tag["src"]
                if src.startswith("http") and download_remote:
                    target = download(src, f"{stem}_script_remote")
                    if target:
                        tag["src"] = str(target.relative_to(output))
                continue
            body = tag.string or ""
            target = save_asset(body.encode("utf-8"), "application/javascript", f"{stem}_script{i}")
            new = soup.new_tag("script", src=str(target.relative_to(output)))
            tag.replace_with(new)
        for tag in soup.find_all("img"):
            src = tag.get("src", "")
            if src.startswith("data:"):
                target = extract_data_uri(src, f"{stem}_img")
                if target:
                    tag["src"] = str(target.relative_to(output))
            elif src.startswith("http") and download_remote:
                target = download(src, f"{stem}_img_remote")
                if target:
                    tag["src"] = str(target.relative_to(output))
        css_url_re = re.compile(r'url\("(data:.*?)"\)')
        for tag in soup.find_all(style=True):
            style = tag["style"]
            m = css_url_re.search(style)
            if m:
                target = extract_data_uri(m.group(1), f"{stem}_bg")
                if target:
                    tag["style"] = style.replace(m.group(1), str(target.relative_to(output)))
        for i, svg in enumerate(soup.find_all("svg")):
            target = save_asset(str(svg).encode("utf-8"), "image/svg+xml", f"{stem}_svg{i}")
            img = soup.new_tag("img", src=str(target.relative_to(output)))
            svg.replace_with(img)
        font_re = re.compile(r'url\("(data:font\/.+?)"\)')
        for style_tag in soup.find_all("style"):
            if not style_tag.string:
                continue
            txt = style_tag.string
            for m in font_re.findall(txt):
                target = extract_data_uri(m, f"{stem}_font")
                if target:
                    txt = txt.replace(m, str(target.relative_to(output)))
            style_tag.string.replace_with(txt)
        for tag in soup.find_all("link", href=True):
            href = tag["href"]
            if href.startswith("http") and download_remote:
                target = download(href, f"{stem}_css_remote")
                if target:
                    tag["href"] = str(target)
        out_path = output / html.relative_to(root)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(str(soup), encoding="utf-8")
        print(f"Processed: {html}")
    print(f"\nAll done — extracted assets saved to {output}")
    return 0


def cmd_list_lines(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    report = Path(args.report)
    files = [Path(p) for p in args.files] if args.files else list(iter_files(root, args.extensions or TEXTY_EXT))
    with report.open("a", encoding="utf-8") as fh:
        for f in files:
            text = read_text_safe(f)
            if text is None:
                continue
            blobs: list[str] = []
            for line in text.splitlines():
                if "base64," not in line:
                    continue
                cut = line.index("base64,") + len("base64,")
                rest = line[cut:]
                for ch in ('"', " ", ")"):
                    idx = rest.find(ch)
                    if idx != -1:
                        rest = rest[:idx]
                blobs.append(rest)
            if blobs:
                print(f"{f.name} : {len(blobs)}")
                fh.write("\n")
                fh.write("\n".join(blobs) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="merge.py",
        description="Unified base64 / data-URI extractor and converter.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser, default_root: str = ".") -> None:
        sp.add_argument(
            "--root",
            default=default_root,
            help="Root path to scan (default: current dir)",
        )
        sp.add_argument("--dry-run", action="store_true", help="Do not write anything, just report")

    sp = sub.add_parser("extract-cleanuri", help="Extract data: URIs from css/js/html and rewrite.")
    add_common(sp)
    sp.add_argument("--out", default="assets")
    sp.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help="File extensions to scan (default: .css .js .html)",
    )
    sp.add_argument("--hash", default="sha256")
    sp.add_argument("--hash-len", type=int, default=64)
    sp.set_defaults(func=cmd_extract_cleanuri)
    sp = sub.add_parser("extract-images", help="Extract data:image/...;base64 URIs from files.")
    add_common(sp)
    sp.add_argument("--out", default="extracted_images")
    sp.add_argument("--extensions", nargs="*", default=None, help="Default: .ipynb .js .html")
    sp.set_defaults(func=cmd_extract_images)
    sp = sub.add_parser("ttf-to-base64", help="Convert *.ttf files to base64 .txt files.")
    add_common(sp)
    sp.add_argument("--pattern", default="*.ttf")
    sp.add_argument("--force", action="store_true", help="Overwrite existing .txt files")
    sp.set_defaults(func=cmd_ttf_to_base64)
    sp = sub.add_parser("file-to-base64", help="Convert a single file to a base64 .txt.")
    sp.add_argument("file")
    sp.add_argument("--output", default=None)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_file_to_base64)
    sp = sub.add_parser("extract-css", help="Extract url(data:...;base64,...) from CSS files.")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--out", default="_static")
    sp.add_argument("--hash", default="sha256")
    sp.add_argument("--hash-len", type=int, default=12)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_extract_css)
    sp = sub.add_parser("extract-assets", help="Comprehensive extractor with magic-byte MIME detection.")
    add_common(sp)
    sp.add_argument("--out", default="assets")
    sp.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help="Default: .html .css .js .jsx .tsx .ts",
    )
    sp.add_argument("--hash", default="sha256")
    sp.add_argument("--hash-len", type=int, default=16)
    sp.add_argument("--no-magic", action="store_true", help="Disable magic-byte MIME sniffing")
    sp.set_defaults(func=cmd_extract_assets)
    sp = sub.add_parser("extract-elements", help="Extract all data:...;base64 blobs into flat dir.")
    add_common(sp)
    sp.add_argument("--out", default="extracted_base64")
    sp.add_argument("--extensions", nargs="*", default=None, help="Default: texty extension set")
    sp.add_argument("--hash", default="sha256")
    sp.add_argument("--hash-len", type=int, default=15)
    sp.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Explicit file list (overrides root scan)",
    )
    sp.set_defaults(func=cmd_extract_elements)
    sp = sub.add_parser("extract-html", help="HTML-only extractor (requires requests+bs4).")
    add_common(sp)
    sp.add_argument("--out", default="output")
    sp.add_argument(
        "--download-remote",
        action="store_true",
        help="Also download remote http(s) assets",
    )
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Explicit HTML files (overrides root scan)",
    )
    sp.set_defaults(func=cmd_extract_html)
    sp = sub.add_parser("list-lines", help="Report every line containing 'base64,'.")
    add_common(sp)
    sp.add_argument("--report", default="b64", help="Output report file (default 'b64')")
    sp.add_argument("--extensions", nargs="*", default=None)
    sp.add_argument("--files", nargs="*", default=None)
    sp.set_defaults(func=cmd_list_lines)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
