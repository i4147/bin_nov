#!/data/data/com.termux/files/usr/bin/env python
"""Display images in the terminal as ANSI blocks or ASCII art.
Analysis / deduplication ------------------------ Four near-identical truecolor half-block renderers (``imgviewer.py``, ``pycatimg.py``, ``timg.py``, ``termimg0.py``) plus ``termimage.py`` (same idea hand-rolled for PPM) collapse into one ``show --mode blocks`` renderer; ``pycatimg``'s SVG support (via cairosvg) and background compositing survive as options.
``img2asci.py`` (ascii-magic) is ``show --mode ascii`` with a built-in grayscale-ramp fallback when ascii-magic is not installed, so the tool always works with just Pillow.
Requires Pillow; SVG additionally needs cairosvg.
Examples -------- python3 terminal_image_tool.py show photo.png python3 terminal_image_tool.py show photos/ -W 100 python3 terminal_image_tool.py show logo.svg --dpi 150 python3 terminal_image_tool.py show photo.png --mode ascii -W 80"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys


IMAGE_SUFFIXES: frozenset[str] = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".tiff",
    ".svg",
    ".ppm",
})
ASCII_RAMP: str = " .:-=+*#%@"
RESET: str = "\x1b[0m"


def load_pillow() -> object:
    """Import PIL.Image or exit with an install hint."""
    try:
        from PIL import Image

        return Image
    except ImportError:
        msg = "Missing optional dependency: pip install Pillow"
        raise SystemExit(msg) from None


def load_image(path: Path, dpi: int, bg_color: str | None) -> object:
    """Open an image (SVG via cairosvg), composited onto *bg_color*."""
    Image = load_pillow()
    if path.suffix.lower() == ".svg":
        try:
            import io

            from cairosvg import svg2png

            png_bytes: bytes = svg2png(bytestring=path.read_bytes(), dpi=dpi) or b""
            image = Image.open(io.BytesIO(png_bytes))  # type: ignore[attr-defined]
        except ImportError:
            msg = "SVG needs cairosvg: pip install cairosvg"
            raise SystemExit(msg) from None
    else:
        image = Image.open(path)  # type: ignore[attr-defined]
    image = image.convert("RGBA")
    if bg_color:
        from PIL import (
            Image as pil_image,
            ImageColor,
        )

        background = pil_image.new("RGBA", image.size, ImageColor.getcolor(bg_color, "RGBA"))
        image = pil_image.alpha_composite(background, image)
    return image.convert("RGB")


def fit_size(src_w: int, src_h: int, max_w: int, max_h: int | None, cell_aspect: float) -> tuple[int, int]:
    """Target pixel-grid size fitting the terminal (cells are tall)."""
    width: int = max(1, max_w)
    height: int = max(1, round(src_h / src_w * width / cell_aspect))
    if max_h is not None and height > max_h:
        height = max_h
        width = max(1, round(src_w / src_h * height * cell_aspect))
    return width, height


def render_blocks(image: object, max_w: int, max_h: int | None) -> str:
    """Truecolor half-block rendering (upper=fg, lower=bg per cell)."""
    from PIL import Image as pil_image

    rgb = image.convert("RGB")  # type: ignore[attr-defined]
    width, rows = fit_size(rgb.width, rgb.height, max_w, max_h, cell_aspect=1.0)
    small = rgb.resize((width, rows * 2), pil_image.Resampling.LANCZOS)
    pixels = small.load()
    lines: list[str] = []
    for row in range(rows):
        cells: list[str] = []
        for col in range(width):
            r1, g1, b1 = pixels[col, row * 2]  # type: ignore[index]
            r2, g2, b2 = pixels[col, row * 2 + 1]  # type: ignore[index]
            cells.append(f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m▀")
        lines.append("".join(cells) + RESET)
    return "\n".join(lines)


def render_full_blocks(image: object, max_w: int, max_h: int | None) -> str:
    """Two-spaces-per-pixel background-colour rendering."""
    from PIL import Image as pil_image

    rgb = image.convert("RGB")  # type: ignore[attr-defined]
    width, height = fit_size(rgb.width, rgb.height, max(1, max_w // 2), max_h, cell_aspect=2.0)
    small = rgb.resize((width, height), pil_image.Resampling.LANCZOS)
    pixels = small.load()
    lines: list[str] = []
    for row in range(height):
        cells: list[str] = []
        for col in range(width):
            r, g, b = pixels[col, row]  # type: ignore[index]
            cells.append(f"\x1b[48;2;{r};{g};{b}m  ")
        lines.append("".join(cells) + RESET)
    return "\n".join(lines)


def render_ascii(image: object, max_w: int, max_h: int | None, invert: bool) -> str:
    """Grayscale-ramp ASCII rendering."""
    from PIL import Image as pil_image

    gray = image.convert("L")  # type: ignore[attr-defined]
    width, height = fit_size(gray.width, gray.height, max_w, max_h, cell_aspect=2.1)
    small = gray.resize((width, height), pil_image.Resampling.LANCZOS)
    pixels = small.load()
    ramp: str = ASCII_RAMP[::-1] if not invert else ASCII_RAMP
    lines: list[str] = []
    for row in range(height):
        chars: list[str] = []
        for col in range(width):
            value: int = int(pixels[col, row])  # type: ignore[index]
            chars.append(ramp[value * (len(ramp) - 1) // 255])
        lines.append("".join(chars))
    return "\n".join(lines)


def terminal_limits(args: argparse.Namespace) -> tuple[int, int | None]:
    """Resolve -W/-H against the actual terminal size."""
    size = shutil.get_terminal_size()
    width: int = args.width or size.columns
    height: int | None = args.height
    if height is None and not args.no_limit_height:
        height = max(1, size.lines - 2)
    return width, height


def find_images(inputs: Sequence[Path], recursive: bool) -> list[Path]:
    """Collect image paths from file/dir arguments."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            candidates = root.rglob("*") if recursive else root.iterdir()
            found.extend(p for p in candidates if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    return sorted(found)


def cmd_show(args: argparse.Namespace) -> int:
    """Render each image to the terminal."""
    images: list[Path] = find_images(args.inputs, args.recursive)
    if not images:
        print("No images found.")
        return 0
    width, height = terminal_limits(args)
    for path in images:
        try:
            image: object = load_image(path, args.dpi, args.bg_color)
        except (OSError, ValueError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        if len(images) > 1:
            print(f"\n{path}  ({image.width}x{image.height})")  # type: ignore[attr-defined]
        if args.mode == "ascii":
            print(render_ascii(image, width, height, args.invert))
        elif args.no_half_blocks:
            print(render_full_blocks(image, width, height))
        else:
            print(render_blocks(image, width, height // 2 if height else None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Render images in the terminal.")
    sub = parser.add_subparsers(dest="command", required=True)
    sh = sub.add_parser("show", help="Display image file(s)/dir(s)")
    sh.add_argument("inputs", nargs="*", type=Path, help="Images or dirs (default: .)")
    sh.add_argument("--mode", choices=("blocks", "ascii"), default="blocks")
    sh.add_argument("-W", "--width", type=int, default=None, help="Max columns (default: terminal)")
    sh.add_argument("-H", "--height", type=int, default=None, help="Max rows (default: terminal)")
    sh.add_argument("--no-limit-height", action="store_true", help="Ignore terminal height")
    sh.add_argument("--no-half-blocks", action="store_true", help="Lower-res space cells")
    sh.add_argument("--invert", action="store_true", help="Invert the ASCII ramp")
    sh.add_argument("--dpi", type=int, default=96, help="SVG raster DPI (default: 96)")
    sh.add_argument("--bg-color", default=None, help="Composite transparency onto, e.g. black")
    sh.add_argument("--no-recursive", dest="recursive", action="store_false")
    sh.set_defaults(handler=cmd_show, recursive=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
