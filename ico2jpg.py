#!/data/data/com.termux/files/usr/bin/env python
"""Convert .ico and .icns icon files to .jpg images.

Selects the largest embedded size where available, writes a .jpg next to each input and optionally removes the original.
"""

from __future__ import annotations
import argparse
from pathlib import Path

from PIL import Image


def convert_to_jpg(input_path: Path, remove_orig: bool = False) -> None:
    if not input_path.is_file():
        print(f"Error: File not found -> {input_path}")
        return

    if input_path.suffix.lower() not in {".ico", ".icns"}:
        print(f"Skipping unsupported file: {input_path}")
        return

    output_path = input_path.with_suffix(".jpg")

    try:
        with Image.open(input_path) as img:
            # Icon files often store multiple sizes; pick the largest available
            if hasattr(img, "ico"):
                sizes = img.ico.sizes()
                if sizes:
                    best_size = max(sizes, key=lambda s: s[0] * s[1])
                    img.size = best_size
            elif hasattr(img, "info") and "sizes" in img.info:
                sizes = img.info["sizes"]
                if sizes:
                    best_size = max(sizes, key=lambda s: s[0] * s[1])
                    img.size = best_size

            # Convert transparency (RGBA/LA/P) onto a solid white background for JPEG
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])

            background.save(output_path, "JPEG", quality=95)
            print(f"Converted: {input_path} -> {output_path}")

        if remove_orig and output_path.exists():
            input_path.unlink()
            print(f"Removed original: {input_path}")

    except Exception as e:
        print(f"Failed to convert {input_path}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert .ico or .icns files to .jpg in-place."
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Path(s) to .ico or .icns file(s)",
    )
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove the original file after successful conversion",
    )

    args = parser.parse_args()

    for file_path in args.files:
        convert_to_jpg(file_path, remove_orig=args.remove)


if __name__ == "__main__":
    main()
