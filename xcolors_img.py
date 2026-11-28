#!/data/data/com.termux/files/usr/bin/env python
"""Count colors in an image and print them as RGB/hex.

Converts the image to RGB, tallies pixel frequencies with a Counter and prints the most common colors; --brief (default) and the mutually exclusive options control the output.
"""

from __future__ import annotations
import argparse
from collections import Counter
import os
from typing import Any, Sequence

from PIL import Image


def rgb_to_hex(rgb: Sequence[Any]) -> str:
    return "#{:02x}{:02x}{:02x}".format(rgb[0], rgb[1], rgb[2])


def get_colors(image_path: str) -> tuple[int,]:
    with Image.open(image_path) as img:
        # Convert to RGB to ensure uniform 3-tuple values
        rgb_img = img.convert("RGB")
        pixels = list(rgb_img.getdata())

    color_counts = Counter(pixels).most_common()
    return color_counts, len(pixels)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract RGB and Hex color values from an image."
    )
    parser.add_argument("image", help="Path to the input image")

    # Mutually exclusive group; brief is the default behavior when neither is passed
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-b",
        "--brief",
        action="store_true",
        default=True,
        help="Print only the top 5 most frequent colors (default)",
    )
    group.add_argument(
        "-f",
        "--full",
        action="store_true",
        help="Print a full report of all unique colors",
    )

    parser.add_argument(
        "-s",
        "--save",
        nargs="?",
        const="colors.txt",
        help=("Save all colors to a text file in hex format (default: colors.txt)"),
    )

    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"Error: File '{args.image}' not found.")
        return

    color_counts, total_pixels = get_colors(args.image)

    if args.full:
        print(
            f"Full Color Report ({len(color_counts)} unique colors across {total_pixels} pixels):"
        )
        print("-" * 55)
        for rgb, count in color_counts:
            percentage = (count / total_pixels) * 100
            print(
                f"RGB: {rgb!s:<16} | Hex: {rgb_to_hex(rgb)} | Count: {count:<8} ({percentage:.2f}%)"
            )
    else:
        # Runs by default when no flag is provided or when -b/--brief is passed
        print("Top 5 Most Frequent Colors:")
        print("-" * 45)
        for rgb, count in color_counts[:5]:
            percentage = (count / total_pixels) * 100
            print(f"RGB: {rgb!s:<16} | Hex: {rgb_to_hex(rgb)} | ({percentage:.2f}%)")

    if args.save:
        with open(args.save, "w") as file:
            for rgb, _ in color_counts:
                file.write(f"{rgb_to_hex(rgb)}\n")
        print(f"\nSaved {len(color_counts)} unique hex colors to '{args.save}'.")


if __name__ == "__main__":
    main()
