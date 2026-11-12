#!/data/data/com.termux/files/usr/bin/env python
"""OCR the images below the current directory and keep only readable results.
Runs Tesseract with the English, Persian and Russian data (several page-segmentation settings), keeps the output only when it looks like real words and writes ``<image>.txt`` next to the image.
Existing text files are skipped unless ``OVERWRITE`` is enabled; the images are processed in parallel.
Usage: imagetotext Examples: $ imagetotext Runs as a script (``python imagetotext.py``) in the directory it should act on; the module can also be imported.
"""

from multiprocessing import Pool
from pathlib import Path
import re
from typing import Any

from PIL import Image
import pytesseract

TESSDATA = str(Path.home() / ".local" / "share" / "tessdata")
LANGS: str = "eng+fas+rus"
OVERWRITE: bool = False

IMAGE_EXTS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".webp",
    ".pnm",
    ".ppm",
}

CONFIGS: list[tuple[Any, ...]] = [
    (3, 3),
    (1, 3),
    (4, 3),
    (6, 3),
    (11, 3),
    (12, 3),
    (3, 1),
    (6, 1),
]

_LETTERS: str = r"A-Za-z\u0600-\u06FF\u0400-\u04FF"
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
    return _WORD_RE.search(s)


def _ocr(img_path: Path, psm: int, oem: int) -> str:
    config = f'--oem {oem} --psm {psm} --tessdata-dir "{TESSDATA}"'
    with Image.open(img_path) as im:
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        return pytesseract.image_to_string(im, lang=LANGS, config=config)


def process_image(img_path_str: str):
    img_path = Path(img_path_str)
    txt_path = img_path.with_suffix(".txt")

    if txt_path.exists() and not OVERWRITE:
        return str(img_path), "skipped"

    best, best_cfg = "", None
    for psm, oem in CONFIGS:
        try:
            text = _ocr(img_path, psm, oem)
            print(f"\npsm={psm} oem={oem}\n")
            print(f"\n{text}\n")
        except Exception:
            continue

        if _is_meaningful(text):
            best, best_cfg = text, (psm, oem)
            break
        if len(text) > len(best):
            best, best_cfg = text, (psm, oem)

    try:
        txt_path.write_text(best, encoding="utf-8")
    except Exception as e:
        return str(img_path), f"error writing txt: {e}"

    good = _is_meaningful(best)
    tag = "ok" if good else "fallback"
    if best_cfg:
        tag += f" (psm={best_cfg[0]},oem={best_cfg[1]})"
    return str(img_path), tag


def main() -> None:
    root = Path()
    images = [str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]

    print(f"Found {len(images)} image(s) under {root.resolve()}")
    if not images:
        return

    with Pool(processes=2) as pool:
        for img, status in pool.imap_unordered(process_image, images):
            print(f"[{status}] {img}")


if __name__ == "__main__":
    main()
