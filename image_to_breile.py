#!/data/data/com.termux/files/usr/bin/env python
"""
Convert an image to Unicode Braille art and render the same dots to PNG.

Requires Pillow: pip install Pillow Run: python image_to_braille.py input.jpg -o output.png --text output.txt

"""

import argparse
import sys
from pathlib import Path
from typing import Any

from dh import atomic_write
from PIL import Image, ImageDraw, ImageOps

# Braille bit positions, indexed by [row][column] within each 2x4 cell.
BRAILLE_BITS: tuple[
    tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]
] = (
    (0, 3),
    (1, 4),
    (2, 5),
    (6, 7),
)

# The rendered cell is 12 x 20 units, with four evenly spaced dot rows.
CELL_WIDTH, CELL_HEIGHT = 12, 20
DOT_X: tuple[int, int] = (4, 8)
DOT_Y: tuple[int, int, int, int] = (4, 8, 12, 16)


def positive_int(value: int) -> int:
    number = int(value)
    if number < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return number


def threshold_int(value: int) -> int:
    number = int(value)
    if not 0 <= number <= 255:
        msg = "must be between 0 and 255"
        raise argparse.ArgumentTypeError(msg)
    return number


def convert(
    image_path: Path, columns: int, scale: float, invert: Any, threshold: float
) -> tuple[str,]:
    with Image.open(image_path) as source:
        # Correct phone-camera orientation, and put transparent pixels on white.
        source = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", source.size, "white")
        background.alpha_composite(source)
        gray = background.convert("L")

    # Correct for the proportions of a rendered Braille cell rather than
    # assuming each character is square. Each character represents 2 x 4 dots.
    rows = max(1, round(gray.height / gray.width * columns * CELL_WIDTH / CELL_HEIGHT))
    gray = gray.resize((columns * 2, rows * 4), Image.Resampling.LANCZOS)
    if invert:
        gray = ImageOps.invert(gray)
    if threshold is None:
        # Dithering preserves detail and approximates intermediate shades.
        dots = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    else:
        dots = gray.point(lambda value: 255 if value >= threshold else 0, mode="1")

    canvas = Image.new(
        "RGB", (columns * CELL_WIDTH * scale, rows * CELL_HEIGHT * scale), "white"
    )
    draw = ImageDraw.Draw(canvas)
    pixels = dots.load()
    lines = []
    for cell_y in range(rows):
        characters = []
        for cell_x in range(columns):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    if pixels[cell_x * 2 + dx, cell_y * 4 + dy] == 0:
                        bits |= 1 << BRAILLE_BITS[dy][dx]
                        cx = (cell_x * CELL_WIDTH + DOT_X[dx]) * scale
                        cy = (cell_y * CELL_HEIGHT + DOT_Y[dy]) * scale
                        radius = scale
                        draw.ellipse(
                            (cx - radius, cy - radius, cx + radius, cy + radius),
                            fill="black",
                        )
            characters.append(chr(0x2800 + bits))
        lines.append("".join(characters))
    return "\n".join(lines) + "\n", canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="input image")
    parser.add_argument(
        "-o", "--output", type=Path, help="output PNG (default: INPUT_braille.png)"
    )
    parser.add_argument(
        "--text", type=Path, help="also save Braille text to a UTF-8 file"
    )
    parser.add_argument(
        "-w",
        "--width",
        type=positive_int,
        default=64,
        help="width in Braille characters (default: 64)",
    )
    parser.add_argument(
        "--scale", type=positive_int, default=2, help="PNG pixel scale (default: 2)"
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="make light image areas produce dots instead of dark areas",
    )
    parser.add_argument(
        "--threshold",
        type=threshold_int,
        help="disable dithering and use this brightness cutoff (0-255)",
    )
    args = parser.parse_args()

    output = args.output or args.image.with_name(args.image.stem + "_braille.png")
    if output.suffix.lower() != ".png":
        parser.error("output filename must end in .png")
    if output.resolve() == args.image.resolve():
        parser.error("output PNG must not overwrite the input image")
    if args.text and args.text.resolve() in (args.image.resolve(), output.resolve()):
        parser.error("text output must be different from the input and PNG output")
    try:
        text, png = convert(
            args.image, args.width, args.scale, args.invert, args.threshold
        )
        png.save(output, format="PNG")
        if args.text:
            atomic_write(args.text, text, encoding="utf-8")
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            print(
                "Could not print Braille in this terminal; use --text to save it as UTF-8.",
                file=sys.stderr,
            )
        print(f"Saved PNG: {output}", file=sys.stderr)
        if args.text:
            print(f"Saved text: {args.text}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
