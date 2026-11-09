#!/data/data/com.termux/files/usr/bin/env python
"""Invert the colours of image files in place.
Negates the RGB channels while keeping the alpha channel and the EXIF/ICC/DPI metadata, and replaces each file atomically (JPEG saved at quality 95).
Files it cannot decode are reported and the exit status becomes 1.
Usage: neg IMAGE [IMAGE ...] Examples: $ neg photo.jpg scan.png Runs as a script (``python neg.py``) in the directory it should act on; the module can also be imported."""

import io
from pathlib import Path
import sys
from typing import Any

from atomicio import atomic_write
from PIL import Image, ImageOps, UnidentifiedImageError


DIRECT_MODES: set[str] = {"L", "RGB", "CMYK"}

META_KEYS: tuple[str, str, str] = ("exif", "icc_profile", "dpi")


def negate(img: Image.Image) -> Image.Image:
    if img.mode in DIRECT_MODES:
        return ImageOps.invert(img)

    has_alpha = "A" in img.getbands() or "transparency" in img.info
    if has_alpha:
        r, g, b, a = img.convert("RGBA").split()
        rgb = ImageOps.invert(Image.merge("RGB", (r, g, b)))
        r, g, b = rgb.split()
        return Image.merge("RGBA", (r, g, b, a))

    return ImageOps.invert(img.convert("RGB"))


def negate_inplace(path: Path) -> None:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    with Image.open(path) as img:
        img.load()
        fmt = img.format
        if not fmt:
            msg = "unknown image format"
            raise ValueError(msg)
        meta = {k: img.info[k] for k in META_KEYS if k in img.info}
        out = negate(img)

    save_kwargs = dict(meta)
    if fmt == "JPEG":
        save_kwargs["quality"] = 95

    buf = io.BytesIO()
    out.save(buf, format=fmt, **save_kwargs)
    atomic_write(str(path), buf.getvalue())


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {Path(argv[0]).name} IMAGE [IMAGE ...]", file=sys.stderr)
        return 2

    rc = 0
    for arg in argv[1:]:
        p = Path(arg)
        try:
            negate_inplace(p)
            print(f"negated {p}")
        except (UnidentifiedImageError, FileNotFoundError, OSError, ValueError) as exc:
            print(f"error: {p}: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
