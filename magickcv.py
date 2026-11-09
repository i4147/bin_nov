#!/data/data/com.termux/files/usr/bin/env python
"""Run ImageMagick-style edit operations on images with Pillow and OpenCV.
Parses the classic command line (``-resize``, ``-crop``, ``-rotate``, ``-flip``/``-flop``, ``-blur``, ``-sharpen``, ``-modulate``, ``-grayscale``, ``-negate``, ``-sepia-tone``, ``-threshold``, ``-alpha``, ``-gravity``, ``-strip`` ...) and applies the operations to the input image(s), writing the result to the output path.
``-identify`` prints the image properties instead of editing.
Usage: magickcv INPUT OUTPUT [operations ...] Positional arguments: inputs ...
output +append Options: -identify, --identify -resize -thumbnail -sample -scale -crop -extent -rotate -flip -flop ...
plus 51 more; run ``magickcv --help`` for the full list.
Examples: $ magickcv in.jpg out.jpg -resize 800x600 -grayscale -strip Command-line script: run ``python magickcv.py`` or the ``magickcv`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFont, ImageOps


RESAMPLING = {
    "nearest": Image.Resampling.NEAREST,
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "hamming": Image.Resampling.HAMMING,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}


def parse_color(value: str) -> tuple[int, int, int, int]:
    try:
        rgb = ImageColor.getcolor(value, "RGBA")
        return rgb
    except ValueError as exc:
        msg = f"invalid color: {value}"
        raise argparse.ArgumentTypeError(msg) from exc


def parse_geometry(value: str) -> tuple[int | None, int | None, str]:
    match = re.fullmatch(r"(?:(\d+)?x?(\d+)?)?([!%^<>]*)", value.strip())
    if not match or not match.group(0):
        msg = f"invalid geometry: {value}"
        raise argparse.ArgumentTypeError(msg)
    width = int(match.group(1)) if match.group(1) else None
    height = int(match.group(2)) if match.group(2) else None
    flags = match.group(3)
    if width is None and height is None:
        msg = f"invalid geometry: {value}"
        raise argparse.ArgumentTypeError(msg)
    return width, height, flags


def parse_offset(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([+-]?\d+)([+-]\d+)", value.replace(" ", ""))
    if not match:
        msg = f"invalid offset: {value}"
        raise argparse.ArgumentTypeError(msg)
    return int(match.group(1)), int(match.group(2))


def parse_crop(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", value.replace(" ", ""))
    if not match:
        msg = f"invalid crop geometry: {value}"
        raise argparse.ArgumentTypeError(msg)
    return tuple(map(int, match.groups()))


def parse_sigma(value: str) -> tuple[float, float]:
    parts = value.split("x")
    try:
        radius = float(parts[0])
        sigma = float(parts[1]) if len(parts) > 1 else radius
    except ValueError as exc:
        msg = f"invalid radius/sigma: {value}"
        raise argparse.ArgumentTypeError(msg) from exc
    return radius, sigma


def parse_point(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([+-]?\d+),([+-]?\d+)", value.replace(" ", ""))
    if not match:
        msg = f"invalid point: {value}"
        raise argparse.ArgumentTypeError(msg)
    return int(match.group(1)), int(match.group(2))


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value.strip())
    if not match:
        msg = f"invalid size: {value}"
        raise argparse.ArgumentTypeError(msg)
    return int(match.group(1)), int(match.group(2))


def parse_quality(value: str) -> int:
    quality = int(value)
    if not 0 <= quality <= 100:
        msg = "quality must be between 0 and 100"
        raise argparse.ArgumentTypeError(msg)
    return quality


def parse_percent(value: str) -> float:
    parsed = float(value.rstrip("%"))
    return parsed / 100.0 if value.endswith("%") else parsed


def pil_to_array(image: Image.Image) -> np.ndarray:
    return np.array(image.convert("RGBA"), dtype=np.uint8)


def array_to_pil(array: np.ndarray) -> Image.Image:
    if array.ndim == 2:
        return Image.fromarray(array, "L").convert("RGBA")
    if array.shape[2] == 3:
        return Image.fromarray(array, "RGB").convert("RGBA")
    return Image.fromarray(array, "RGBA")


def flatten_alpha(image: Image.Image, background: tuple[int, int, int, int]) -> Image.Image:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    canvas = Image.new("RGBA", image.size, background)
    canvas.alpha_composite(image)
    return canvas.convert("RGB")


def load_image(path: Path, page: int = 0) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix in {".gif", ".webp", ".tif", ".tiff"}:
        with Image.open(path) as source:
            try:
                source.seek(page)
            except EOFError:
                msg = f"page {page} does not exist in {path}"
                raise ValueError(msg)
            return source.convert("RGBA")
    return Image.open(path).convert("RGBA")


def load_frames(path: Path) -> list[Image.Image]:
    with Image.open(path) as source:
        frames = []
        for frame in range(getattr(source, "n_frames", 1)):
            source.seek(frame)
            frames.append(source.convert("RGBA"))
        return frames


def resize_image(
    image: Image.Image,
    geometry: tuple[int | None, int | None, str],
    mode: str,
    filter_name: str,
) -> Image.Image:
    width, height, flags = geometry
    source_width, source_height = image.size
    if "%" in flags:
        width = max(1, round(source_width * (width or 100) / 100))
        height = max(1, round(source_height * (height or width or 100) / 100))
        return image.resize((width, height), RESAMPLING[filter_name])
    if width is None:
        width = max(1, round(source_width * height / source_height))
    if height is None:
        height = max(1, round(source_height * width / source_width))
    source_ratio = source_width / source_height
    target_ratio = width / height
    if "!" in flags:
        result_size = (width, height)
    elif mode == "thumbnail":
        factor = min(width / source_width, height / source_height)
        result_size = (
            max(1, round(source_width * factor)),
            max(1, round(source_height * factor)),
        )
    elif mode == "sample":
        factor = min(width / source_width, height / source_height)
        result_size = (
            max(1, round(source_width * factor)),
            max(1, round(source_height * factor)),
        )
        return image.resize(result_size, Image.Resampling.NEAREST)
    elif "^" in flags:
        factor = max(width / source_width, height / source_height)
        result_size = (
            max(1, round(source_width * factor)),
            max(1, round(source_height * factor)),
        )
    elif target_ratio > source_ratio:
        result_size = (max(1, round(height * source_ratio)), height)
    else:
        result_size = (width, max(1, round(width / source_ratio)))
    if "<" in flags and source_width < width and source_height < height:
        return image
    if ">" in flags and source_width > width and source_height > height:
        return image
    return image.resize(result_size, RESAMPLING[filter_name])


def extent_image(
    image: Image.Image,
    size: tuple[int, int],
    background: tuple[int, int, int, int],
    gravity: str,
) -> Image.Image:
    width, height = size
    result = Image.new("RGBA", (width, height), background)
    x, y = gravity_position(result.size, image.size, gravity)
    result.alpha_composite(image, (x, y))
    return result


def gravity_position(container: tuple[int, int], item: tuple[int, int], gravity: str) -> tuple[int, int]:
    cw, ch = container
    iw, ih = item
    positions = {
        "northwest": (0, 0),
        "north": ((cw - iw) // 2, 0),
        "northeast": (cw - iw, 0),
        "west": (0, (ch - ih) // 2),
        "center": ((cw - iw) // 2, (ch - ih) // 2),
        "east": (cw - iw, (ch - ih) // 2),
        "southwest": (0, ch - ih),
        "south": ((cw - iw) // 2, ch - ih),
        "southeast": (cw - iw, ch - ih),
    }
    return positions.get(gravity.lower(), positions["center"])


def crop_image(
    image: Image.Image,
    crop: tuple[int, int, int, int],
    background: tuple[int, int, int, int],
) -> Image.Image:
    width, height, x, y = crop
    result = Image.new("RGBA", (width, height), background)
    result.alpha_composite(image, (-x, -y))
    return result


def rotate_image(image: Image.Image, angle: float, background: tuple[int, int, int, int]) -> Image.Image:
    return image.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC, fillcolor=background)


def opencv_blur(image: Image.Image, radius: float, sigma: float) -> Image.Image:
    array = pil_to_array(image)
    kernel = max(3, round(radius * 2 + 1) | 1)
    sigma = sigma if sigma > 0 else radius
    blurred = cv2.GaussianBlur(array, (kernel, kernel), sigmaX=sigma, sigmaY=sigma)
    return array_to_pil(blurred)


def sharpen_image(image: Image.Image, radius: float, sigma: float, amount: float, threshold: int) -> Image.Image:
    array = pil_to_array(image)
    kernel = max(3, round(radius * 2 + 1) | 1)
    sigma = sigma if sigma > 0 else radius
    blur = cv2.GaussianBlur(array, (kernel, kernel), sigmaX=sigma, sigmaY=sigma)
    difference = array.astype(np.int16) - blur.astype(np.int16)
    if threshold > 0:
        difference[np.abs(difference) < threshold] = 0
    result = np.clip(array.astype(np.float32) + difference * amount, 0, 255).astype(np.uint8)
    return array_to_pil(result)


def median_image(image: Image.Image, size: int) -> Image.Image:
    array = pil_to_array(image)
    kernel = max(3, int(size) | 1)
    return array_to_pil(cv2.medianBlur(array, kernel))


def despeckle_image(image: Image.Image) -> Image.Image:
    array = pil_to_array(image)
    return array_to_pil(cv2.medianBlur(array, 3))


def edge_image(image: Image.Image, radius: float) -> Image.Image:
    gray = np.array(image.convert("L"))
    blurred = cv2.GaussianBlur(gray, (0, 0), max(radius, 0.1))
    edges = cv2.Canny(blurred, 50, 150)
    return Image.fromarray(edges, "L").convert("RGBA")


def emboss_image(image: Image.Image) -> Image.Image:
    array = pil_to_array(image)
    kernel = np.array([[-2, -1, 0], [-1, 1, 1], [0][1][2]], dtype=np.float32)
    result = cv2.filter2D(array, -1, kernel) + 128
    return array_to_pil(np.clip(result, 0, 255).astype(np.uint8))


def normalize_image(image: Image.Image, equalize: bool = False) -> Image.Image:
    array = pil_to_array(image)
    rgb = array[:, :, :3]
    if equalize:
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        lab[:, :, 0] = cv2.equalizeHist(lab[:, :, 0])
        rgb = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    else:
        rgb = cv2.normalize(rgb, None, 0, 255, cv2.NORM_MINMAX)
    array[:, :, :3] = rgb
    return array_to_pil(array)


def contrast_stretch(image: Image.Image, low: float, high: float) -> Image.Image:
    array = pil_to_array(image)
    rgb = array[:, :, :3].astype(np.float32)
    lo = np.percentile(rgb, low * 40)
    hi = np.percentile(rgb, 100 - high * 40)
    if hi <= lo:
        return image
    array[:, :, :3] = np.clip((rgb - lo) * 255 / (hi - lo), 0, 255).astype(np.uint8)
    return array_to_pil(array)


def gamma_image(image: Image.Image, gamma: float) -> Image.Image:
    array = pil_to_array(image)
    lut = np.clip(((np.arange(256) / 255.0) ** (1.0 / gamma)) * 255, 0, 255).astype(np.uint8)
    array[:, :, :3] = cv2.LUT(array[:, :, :3], lut)
    return array_to_pil(array)


def modulate_image(image: Image.Image, brightness: float, saturation: float, hue: float) -> Image.Image:
    array = pil_to_array(image)
    rgb = array[:, :, :3]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation / 100.0, 0, 255)
    hsv[:, :, 2] = np.clip(hsv[:, :, 2] * brightness / 100.0, 0, 255)
    hsv[:, :, 0] = (hsv[:, :, 0] + hue / 2.0) % 180
    array[:, :, :3] = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    return array_to_pil(array)


def tint_image(image: Image.Image, color: tuple[int, int, int, int], amount: float) -> Image.Image:
    overlay = Image.new("RGBA", image.size, color)
    return Image.blend(image, overlay, max(0, min(1, amount)))


def threshold_image(image: Image.Image, threshold: int) -> Image.Image:
    gray = np.array(image.convert("L"))
    _, result = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    return Image.fromarray(result, "L").convert("RGBA")


def adaptive_threshold_image(image: Image.Image, block_size: int, constant: float) -> Image.Image:
    gray = np.array(image.convert("L"))
    block_size = max(3, block_size | 1)
    result = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size,
        constant,
    )
    return Image.fromarray(result, "L").convert("RGBA")


def negate_image(image: Image.Image) -> Image.Image:
    array = pil_to_array(image)
    array[:, :, :3] = 255 - array[:, :, :3]
    return array_to_pil(array)


def sepia_image(image: Image.Image) -> Image.Image:
    array = pil_to_array(image)
    rgb = array[:, :, :3].astype(np.float32)
    matrix = np.array(
        [[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]],
        dtype=np.float32,
    )
    array[:, :, :3] = np.clip(rgb @ matrix.T, 0, 255).astype(np.uint8)
    return array_to_pil(array)


def channel_image(image: Image.Image, channel: str) -> Image.Image:
    array = pil_to_array(image)
    mapping = {"r": 0, "g": 1, "b": 2, "a": 3}
    channel = channel.lower()
    if channel not in mapping:
        msg = "channel must be one of r, g, b, a"
        raise ValueError(msg)
    return Image.fromarray(array[:, :, mapping[channel]], "L").convert("RGBA")


def composite_image(
    base: Image.Image,
    overlay: Image.Image,
    offset: tuple[int, int],
    gravity: str,
    blend: str,
    dissolve: float,
) -> Image.Image:
    result = base.copy()
    if gravity:
        x, y = gravity_position(result.size, overlay.size, gravity)
    else:
        x, y = 0, 0
    x += offset[0]
    y += offset[1]
    if blend == "over":
        if dissolve < 1:
            alpha = overlay.getchannel("A").point(lambda value: round(value * dissolve))
            overlay = overlay.copy()
            overlay.putalpha(alpha)
        result.alpha_composite(overlay, (x, y))
        return result
    layer = Image.new("RGBA", result.size, (0, 0, 0, 0))
    layer.alpha_composite(overlay, (x, y))
    a = pil_to_array(result)
    b = pil_to_array(layer)
    alpha = b[:, :, 3:4].astype(np.float32) / 255.0 * dissolve
    if blend == "multiply":
        mixed = a[:, :, :3].astype(np.float32) * b[:, :, :3].astype(np.float32) / 255.0
    elif blend == "screen":
        mixed = 255 - (255 - a[:, :, :3].astype(np.float32)) * (255 - b[:, :, :3].astype(np.float32)) / 255.0
    elif blend == "difference":
        mixed = np.abs(a[:, :, :3].astype(np.float32) - b[:, :, :3].astype(np.float32))
    elif blend == "add":
        mixed = np.clip(a[:, :, :3].astype(np.float32) + b[:, :, :3].astype(np.float32), 0, 255)
    else:
        mixed = b[:, :, :3]
    a[:, :, :3] = (a[:, :, :3].astype(np.float32) * (1 - alpha) + mixed * alpha).astype(np.uint8)
    a[:, :, 3] = np.maximum(a[:, :, 3], b[:, :, 3])
    return array_to_pil(a)


def draw_text(
    image: Image.Image,
    text: str,
    point: tuple[int, int],
    fill: tuple[int, int, int, int],
    stroke: tuple[int, int, int, int],
    stroke_width: int,
    font_path: str | None,
    font_size: int,
    gravity: str,
) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    text_size = (box[2] - box[0], box[3] - box[1])
    x, y = gravity_position(image.size, text_size, gravity) if gravity else point
    if gravity:
        x += point[0]
        y += point[1]
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke,
    )
    return result


def append_images(images: list[Image.Image], vertical: bool, background: tuple[int, int, int, int]) -> Image.Image:
    if vertical:
        width = max(image.width for image in images)
        height = sum(image.height for image in images)
        result = Image.new("RGBA", (width, height), background)
        y = 0
        for image in images:
            result.alpha_composite(image, ((width - image.width) // 2, y))
            y += image.height
    else:
        width = sum(image.width for image in images)
        height = max(image.height for image in images)
        result = Image.new("RGBA", (width, height), background)
        x = 0
        for image in images:
            result.alpha_composite(image, (x, (height - image.height) // 2))
            x += image.width
    return result


def montage_images(
    images: list[Image.Image],
    tile: tuple[int, int],
    geometry: tuple[int, int],
    background: tuple[int, int, int, int],
) -> Image.Image:
    columns, rows = tile
    cell_width, cell_height = geometry
    rows = max(rows, (len(images) + columns - 1) // columns)
    result = Image.new("RGBA", (columns * cell_width, rows * cell_height), background)
    for index, image in enumerate(images):
        row, column = divmod(index, columns)
        thumbnail = image.copy()
        thumbnail.thumbnail((cell_width, cell_height), Image.Resampling.LANCZOS)
        x = column * cell_width + (cell_width - thumbnail.width) // 2
        y = row * cell_height + (cell_height - thumbnail.height) // 2
        result.alpha_composite(thumbnail, (x, y))
    return result


def save_image(
    image: Image.Image,
    output: Path,
    quality: int | None,
    background: tuple[int, int, int, int],
    compression: int | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    params: dict[str, object] = {}
    if suffix in {".jpg", ".jpeg"}:
        image = flatten_alpha(image, background)
        params["quality"] = quality if quality is not None else 92
        params["optimize"] = True
    elif suffix == ".webp":
        params["quality"] = quality if quality is not None else 90
    elif suffix == ".png" and compression is not None:
        params["compress_level"] = max(0, min(9, compression))
    elif suffix in {".bmp", ".ppm", ".pgm"}:
        image = flatten_alpha(image, background)
    image.save(output, **params)


def identify(path: Path) -> None:
    with Image.open(path) as image:
        width, height = image.size
        frames = getattr(image, "n_frames", 1)
        print(f"{path}: {width}x{height} {image.mode} {image.format} frames={frames}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magickcv")
    parser.add_argument("inputs", nargs="*")
    parser.add_argument("output", nargs="?")
    parser.add_argument("-identify", "--identify", action="store_true")
    parser.add_argument("-resize")
    parser.add_argument("-thumbnail")
    parser.add_argument("-sample")
    parser.add_argument("-scale")
    parser.add_argument("-crop")
    parser.add_argument("-extent")
    parser.add_argument("-rotate", type=float)
    parser.add_argument("-flip", action="store_true")
    parser.add_argument("-flop", action="store_true")
    parser.add_argument("-transpose", action="store_true")
    parser.add_argument("-transverse", action="store_true")
    parser.add_argument("-strip", action="store_true")
    parser.add_argument("-background", type=parse_color, default=(0, 0, 0, 0))
    parser.add_argument("-fill", type=parse_color, default=(255, 255, 255, 255))
    parser.add_argument("-stroke", type=parse_color, default=(0, 0, 0, 255))
    parser.add_argument("-strokewidth", type=int, default=0)
    parser.add_argument("-gravity", default="center")
    parser.add_argument("-filter", choices=sorted(RESAMPLING), default="lanczos")
    parser.add_argument("-blur", type=parse_sigma)
    parser.add_argument("-gaussian-blur", dest="gaussian_blur", type=parse_sigma)
    parser.add_argument("-sharpen", type=parse_sigma)
    parser.add_argument("-unsharp", nargs="+", type=float)
    parser.add_argument("-median", type=int)
    parser.add_argument("-despeckle", action="store_true")
    parser.add_argument("-edge", type=float)
    parser.add_argument("-emboss", action="store_true")
    parser.add_argument("-normalize", action="store_true")
    parser.add_argument("-equalize", action="store_true")
    parser.add_argument("-contrast-stretch", nargs="?", const="0x0")
    parser.add_argument("-gamma", type=float)
    parser.add_argument("-brightness-contrast", nargs=2, type=float)
    parser.add_argument("-modulate")
    parser.add_argument("-tint", nargs=2)
    parser.add_argument("-colorspace", choices=["gray", "grey", "rgb", "rgba"])
    parser.add_argument("-grayscale", action="store_true")
    parser.add_argument("-negate", action="store_true")
    parser.add_argument("-sepia-tone", type=parse_percent)
    parser.add_argument("-threshold", type=int)
    parser.add_argument("-adaptive-threshold", nargs=2, type=float)
    parser.add_argument("-channel")
    parser.add_argument("-alpha", choices=["on", "off", "remove", "set", "opaque"])
    parser.add_argument("-transparent", type=parse_color)
    parser.add_argument("-transparent-color", type=parse_color)
    parser.add_argument("-border", type=parse_size)
    parser.add_argument("-bordercolor", type=parse_color)
    parser.add_argument("-frame", type=parse_size)
    parser.add_argument("-draw")
    parser.add_argument("-annotate", nargs=2)
    parser.add_argument("-font")
    parser.add_argument("-pointsize", type=int, default=24)
    parser.add_argument("-composite")
    parser.add_argument("-geometry", type=parse_offset, default=(0, 0))
    parser.add_argument(
        "-compose",
        choices=["over", "multiply", "screen", "difference", "add"],
        default="over",
    )
    parser.add_argument("-dissolve", type=parse_percent, default=1.0)
    parser.add_argument("-append", action="store_true")
    parser.add_argument("+append", dest="append_horizontal", action="store_true")
    parser.add_argument("-montage", action="store_true")
    parser.add_argument("-tile", type=parse_size, default=(4, 0))
    parser.add_argument("-quality", type=parse_quality)
    parser.add_argument("-compression", type=int)
    parser.add_argument("-page", type=int, default=0)
    return parser


def apply_operations(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    if args.resize:
        image = resize_image(image, parse_geometry(args.resize), "resize", args.filter)
    if args.thumbnail:
        image = resize_image(image, parse_geometry(args.thumbnail), "thumbnail", args.filter)
    if args.sample:
        image = resize_image(image, parse_geometry(args.sample), "sample", args.filter)
    if args.scale:
        image = resize_image(image, parse_geometry(args.scale), "resize", "nearest")
    if args.crop:
        image = crop_image(image, parse_crop(args.crop), args.background)
    if args.extent:
        image = extent_image(image, parse_size(args.extent), args.background, args.gravity)
    if args.rotate is not None:
        image = rotate_image(image, args.rotate, args.background)
    if args.flip:
        image = ImageOps.flip(image)
    if args.flop:
        image = ImageOps.mirror(image)
    if args.transpose:
        image = image.transpose(Image.Transpose.TRANSPOSE)
    if args.transverse:
        image = image.transpose(Image.Transpose.TRANSVERSE)
    if args.border:
        color = args.bordercolor or args.background
        image = ImageOps.expand(image, border=args.border, fill=color)
    if args.frame:
        color = args.bordercolor or args.background
        image = ImageOps.expand(image, border=args.frame, fill=color)
    if args.blur:
        image = opencv_blur(image, *args.blur)
    if args.gaussian_blur:
        image = opencv_blur(image, *args.gaussian_blur)
    if args.sharpen:
        radius, sigma = args.sharpen
        image = sharpen_image(image, radius, sigma, 1.0, 0)
    if args.unsharp:
        values = args.unsharp + [0.0] * (4 - len(args.unsharp))
        radius, sigma, amount, threshold = values[:4]
        image = sharpen_image(
            image,
            radius,
            sigma,
            amount / 100.0 if amount > 2 else amount,
            int(threshold),
        )
    if args.median:
        image = median_image(image, args.median)
    if args.despeckle:
        image = despeckle_image(image)
    if args.edge is not None:
        image = edge_image(image, args.edge)
    if args.emboss:
        image = emboss_image(image)
    if args.normalize:
        image = normalize_image(image)
    if args.equalize:
        image = normalize_image(image, equalize=True)
    if args.contrast_stretch:
        low, high = args.contrast_stretch.split("x", 1)
        image = contrast_stretch(image, parse_percent(low), parse_percent(high))
    if args.gamma:
        image = gamma_image(image, args.gamma)
    if args.brightness_contrast:
        brightness, contrast = args.brightness_contrast
        image = ImageEnhance.Brightness(image).enhance(brightness / 100.0)
        image = ImageEnhance.Contrast(image).enhance(contrast / 100.0)
    if args.modulate:
        parts = [float(part) for part in args.modulate.split(",")]
        parts += [100.0] * (3 - len(parts))
        image = modulate_image(image, parts[0], parts[1], parts[2])
    if args.tint:
        image = tint_image(image, parse_color(args.tint[0]), parse_percent(args.tint[1]))
    if args.colorspace in {"gray", "grey"} or args.grayscale:
        alpha = image.getchannel("A")
        image = image.convert("L").convert("RGBA")
        image.putalpha(alpha)
    if args.negate:
        image = negate_image(image)
    if args.sepia_tone is not None:
        sepia = sepia_image(image)
        image = Image.blend(image, sepia, max(0, min(1, args.sepia_tone)))
    if args.threshold is not None:
        image = threshold_image(image, args.threshold)
    if args.adaptive_threshold:
        image = adaptive_threshold_image(image, int(args.adaptive_threshold[0]), args.adaptive_threshold[1])
    if args.channel:
        image = channel_image(image, args.channel)
    if args.transparent or args.transparent_color:
        target = args.transparent or args.transparent_color
        array = pil_to_array(image)
        mask = np.all(array[:, :, :3] == np.array(target[:3], dtype=np.uint8), axis=2)
        array[mask, 3] = 0
        image = array_to_pil(array)
    if args.alpha in {"off", "remove"}:
        image = flatten_alpha(image, args.background).convert("RGBA")
    if args.alpha in {"set", "opaque"}:
        image.putalpha(255)
    if args.draw:
        image = apply_draw(image, args.draw, args.fill, args.stroke, args.strokewidth)
    if args.annotate:
        offset = parse_offset(args.annotate[0])
        image = draw_text(
            image,
            args.annotate[1],
            offset,
            args.fill,
            args.stroke,
            args.strokewidth,
            args.font,
            args.pointsize,
            args.gravity,
        )
    return image


def apply_draw(
    image: Image.Image,
    command: str,
    fill: tuple[int, int, int, int],
    stroke: tuple[int, int, int, int],
    stroke_width: int,
) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    rect = re.fullmatch(
        r"\s*rectangle\s+([+-]?\d+),([+-]?\d+)\s+([+-]?\d+),([+-]?\d+)\s*",
        command,
        re.IGNORECASE,
    )
    line = re.fullmatch(
        r"\s*line\s+([+-]?\d+),([+-]?\d+)\s+([+-]?\d+),([+-]?\d+)\s*",
        command,
        re.IGNORECASE,
    )
    circle = re.fullmatch(
        r"\s*circle\s+([+-]?\d+),([+-]?\d+)\s+([+-]?\d+),([+-]?\d+)\s*",
        command,
        re.IGNORECASE,
    )
    if rect:
        x1, y1, x2, y2 = map(int, rect.groups())
        draw.rectangle((x1, y1, x2, y2), fill=fill, outline=stroke, width=stroke_width)
    elif line:
        x1, y1, x2, y2 = map(int, line.groups())
        draw.line((x1, y1, x2, y2), fill=stroke, width=max(1, stroke_width))
    elif circle:
        cx, cy, px, py = map(int, circle.groups())
        radius = round(((px - cx) ** 2 + (py - cy) ** 2) ** 0.5)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=fill,
            outline=stroke,
            width=stroke_width,
        )
    else:
        msg = f"unsupported draw operation: {command}"
        raise ValueError(msg)
    return result


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.identify:
        if not args.inputs:
            parser.error("identify requires at least one input")
        for value in args.inputs:
            identify(Path(value))
        return 0
    if not args.inputs:
        parser.error("at least one input image is required")
    if not args.output:
        parser.error("an output path is required")
    input_paths = [Path(value) for value in args.inputs]
    output = Path(args.output)
    if args.montage:
        images = [apply_operations(load_image(path, args.page), args) for path in input_paths]
        image = montage_images(
            images,
            args.tile,
            parse_size(args.extent) if args.extent else images[0].size,
            args.background,
        )
    elif args.append or args.append_horizontal:
        images = [apply_operations(load_image(path, args.page), args) for path in input_paths]
        image = append_images(images, vertical=args.append, background=args.background)
    else:
        image = apply_operations(load_image(input_paths[0], args.page), args)
        if args.composite:
            overlay = load_image(Path(args.composite), args.page)
            image = composite_image(image, overlay, args.geometry, args.gravity, args.compose, args.dissolve)
    save_image(image, output, args.quality, args.background, args.compression)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
