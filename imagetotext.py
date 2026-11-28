#!/data/data/com.termux/files/usr/bin/env python
"""
OCR the images below the current directory and keep only readable results.

Runs Tesseract with the English and Persian models, keeps the output only when
it looks like real words and writes <image>.txt next to the image. Existing
text files are skipped unless OVERWRITE is enabled; images are processed in
parallel.

Usage:
    imagetotext

Examples:
    $ imagetotext Runs as a script (python imagetotext.py) in the directory it should act on; the module can also be imported.
"""

from __future__ import annotations
from multiprocessing import Pool
import os
from pathlib import Path
import re
from typing import Any


# Keep each Tesseract subprocess single-threaded: we already parallelise
# across images, so this avoids CPU thrashing and extra heat on phones.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

from dh import IMAGE_EXT, atomic_write
from PIL import Image
import pytesseract


TESSDATA = str(Path.home() / ".local" / "share" / "tessdata")
LANGS: str = "eng+fas"
OVERWRITE: bool = False
DEBUG: bool = False

# Small, fast set: LSTM-only engine (oem=1) and the PSMs that matter most.
# Tried in order; the first "meaningful" result wins.
CONFIGS: list[tuple[int, int]] = [
    (3, 1),  # fully automatic page segmentation (default)
    (6, 1),  # assume a single uniform block of text
    (11, 1),  # sparse text, no specific order
]

_LETTERS: str = r"A-Za-z\u0600-\u06FF"
_WORD_RE = re.compile(rf"[{_LETTERS}]{{2,}}")
_ALPHA_RE = re.compile(rf"[{_LETTERS}]")


def _is_meaningful(text: str) -> bool:
    if not text:
        return False
    s = text.strip()
    if len(s) < 3:
        return False
    total = sum(1 for ch in s if not ch.isspace())
    if total < 3:
        return False
    alpha = len(_ALPHA_RE.findall(s))
    if alpha / total < 0.5:
        return False
    return _WORD_RE.search(s) is not None


def _ocr(image_input: Any, psm: int, oem: int) -> str:
    config = f'--oem {oem} --psm {psm} --tessdata-dir "{TESSDATA}"'
    return pytesseract.image_to_string(image_input, lang=LANGS, config=config)


def _try_configs(image_input: Any) -> tuple[str, tuple[int, int] | None]:
    best, best_cfg = "", None
    for psm, oem in CONFIGS:
        try:
            text = _ocr(image_input, psm, oem)
        except Exception:
            continue
        if DEBUG:
            print(f"\npsm={psm} oem={oem}\n{text}\n")
        if _is_meaningful(text):
            return text, (psm, oem)
        if len(text) > len(best):
            best, best_cfg = text, (psm, oem)
    return best, best_cfg


def process_image(img_path_str: str):
    img_path = Path(img_path_str)
    txt_path = img_path.with_suffix(".txt")

    if txt_path.exists() and not OVERWRITE:
        return str(img_path), "skipped"

    # Fast path: hand the file straight to Tesseract (no temp re-encode).
    best, best_cfg = _try_configs(str(img_path))

    # Rare fallback for formats Tesseract can't read directly (e.g. RGBA PNGs).
    if not best:
        try:
            with Image.open(img_path) as im:
                if im.mode not in ("L", "RGB"):
                    im = im.convert("RGB")
                im.load()
                best, best_cfg = _try_configs(im)
        except Exception:
            pass

    try:
        atomic_write(txt_path, best, encoding="utf-8")
    except Exception as e:
        return str(img_path), f"error writing txt: {e}"

    good = _is_meaningful(best)
    tag = "ok" if good else "fallback"
    if best_cfg:
        tag += f" (psm={best_cfg[0]},oem={best_cfg[1]})"
    return str(img_path), tag


def main() -> None:
    root = Path()
    images = [str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]

    print(f"Found {len(images)} image(s) under {root.resolve()}")
    if not images:
        return

    # Phones throttle under sustained load; a small pool is faster and cooler.
    workers = max(1, min(os.cpu_count() or 2, 4))
    with Pool(processes=workers) as pool:
        for img, status in pool.imap_unordered(process_image, images, chunksize=1):
            print(f"[{status}] {img}")


if __name__ == "__main__":
    raise SystemExit(main())
