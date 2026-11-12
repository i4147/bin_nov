#!/data/data/com.termux/files/usr/bin/env python
"""Extract the ``data:`` URIs of HTML files into ``extracted_base64``.
Finds the base64 data URLs, decodes them, names the files after a content hash with the extension inferred from the MIME type and writes them to ``extracted_base64``, skipping the duplicates.
Usage: xem [html files ...] Examples: $ xem page.html Runs as a script (``python xem.py``) in the directory it should act on; the module can also be imported.
"""

import base64
import hashlib
from pathlib import Path
import re
import sys
from typing import Any, Iterator

from dh import MIME2EXT, get_nobinary

OUTPUT_DIR: Path = Path("extracted_base64")
DATA_URL_RE = re.compile(r"data:(?P<mime>[-\w.+/]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)", re.IGNORECASE)


def infer_extension(mime: str) -> str:
    return MIME2EXT.get(mime.lower(), mime.rsplit("/", maxsplit=1)[-1])[0]


def decode_base64(data: str):
    cleaned = "".join(data.split())
    return base64.b64decode(cleaned, validate=False)


def content_hash(data: Any) -> str:
    return hashlib.sha256(data).hexdigest()[:15]


def extract_from_html(html: str) -> Iterator[tuple[Any, ...]]:
    for matchz in DATA_URL_RE.finditer(html):
        mime = matchz.group("mime")
        raw_data = matchz.group("data")
        try:
            decoded = decode_base64(raw_data)
        except Exception:
            continue
        yield (mime, decoded)


def save_asset(mime: Any, data: Any):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ext = infer_extension(mime)
    digest = content_hash(data)
    filename = f"{digest}.{ext}"
    path = OUTPUT_DIR / filename
    if not path.exists():
        path.write_bytes(data)
    return path


def main() -> None:
    cwd = Path.cwd()
    seen_hashes = set()
    extracted_count = 0
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_nobinary(cwd)
    for html_file in files:
        try:
            html = html_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for mime, data in extract_from_html(html):
            digest = content_hash(data)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            save_asset(mime, data)
            extracted_count += 1
    print(f"{extracted_count} elements extracted.")


if __name__ == "__main__":
    raise SystemExit(main())
