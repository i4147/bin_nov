#!/data/data/com.termux/files/usr/bin/env python
"""Colour utilities: extract, convert, sort and display hex colours.
Subcommands: ``html`` writes a swatch page, ``extract``/``extract-show`` gather the ``#rrggbb``/``rgb()`` colours of a tree (with ANSI previews), ``hex2rgb`` converts one value, ``showcolor`` prints random colour blocks and ``sorthue`` sorts the hex lines by hue.
Usage: colortools {html,extract,extract-show,hex2rgb,showcolor,sorthue} [options] Positional arguments: input output path hex file Subcommands: html Write an HTML color display page.
extract Scan for hex colors and write them out.
extract-show Scan and print ANSI color swatches.
hex2rgb Convert a hex color to RGB.
showcolor Print random ANSI color blocks.
sorthue Sort #rrggbb lines by HSV hue.
Options: --output --max-colors --max-size --ext File extension to include (repeatable).
--format --allow-short Allow 3-digit #abc form.
--count Number of blocks (default: random 1-999).
Examples: $ colortools extract-show --path css/ $ colortools hex2rgb '#ff8800' Command-line script: run ``python colortools.py`` or the ``colortools`` command.
The module is importable as well."""

import argparse
import colorsys
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import random
import re
import sys
from typing import Iterator, Optional, Sequence


try:
    from dh import cprint, is_binary, should_skip
except ImportError:

    def cprint(*args, **kwargs):
        print(*args, **kwargs)

    def is_binary(path) -> bool:
        try:
            with open(path, "rb") as f:
                return b"\x00" in f.read(1024)
        except Exception:
            return True

    def should_skip(path) -> bool:
        return False


HEX_RE = re.compile(r"#([a-fA-F0-9]{6}|[a-fA-F0-9]{3})\b")
FULL_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
BARE_HEX_RE = re.compile(r"(?<![0-9A-Fa-f])" r"(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})" r"(?![0-9A-Fa-f])")
RGB_RE = re.compile(
    r"\brgba?\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*" r"(?P<b>\d{1,3})(?:\s*,\s*(?P<a>[\d\.]+))?\s*\)\b",
    re.IGNORECASE,
)
RESET = "\x1b[0m"


def _walk_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, _dirs, filenames in os.walk(root, topdown=True):
        d = Path(dirpath)
        for name in filenames:
            p = d / name
            if not should_skip(p):
                yield p


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else min(x, 1.0)


def _luminance(r: int, g: int, b: int) -> float:
    def f(x: float) -> float:
        x /= 255.0
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _bg(r: int, g: int, b: int) -> str:
    return f"\x1b[48;2;{r};{g};{b}m"


def _fg(r: int, g: int, b: int) -> str:
    return f"\x1b[38;2;{r};{g};{b}m"


def _text_color(r: int, g: int, b: int) -> tuple[int, int, int]:
    return (0, 0, 0) if _luminance(r, g, b) > 0.35 else (255, 255, 255)


@dataclass(frozen=True)
class Color:
    r: int
    g: int
    b: int
    a: float = 1.0

    def as_tuple(self) -> tuple[int, int, int, float]:
        return (self.r, self.g, self.b, self.a)

    def to_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"


def _hex_to_color(hex_str: str) -> Color:
    if len(hex_str) == 3:
        r = int(hex_str[0] * 2, 16)
        g = int(hex_str[1] * 2, 16)
        b = int(hex_str[2] * 2, 16)
        a = 1.0
    elif len(hex_str) == 6:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        a = 1.0
    elif len(hex_str) == 8:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        a = int(hex_str[6:8], 16) / 255.0
    else:
        msg = f"Unexpected hex length: {len(hex_str)}"
        raise ValueError(msg)
    return Color(r=r, g=g, b=b, a=a)


def _match_rgba(m: "re.Match[str]") -> Optional[Color]:
    r = int(m.group("r"))
    g = int(m.group("g"))
    b = int(m.group("b"))
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        return None
    raw_a = m.groupdict().get("a")
    if raw_a is None:
        a = 1.0
    else:
        n = float(raw_a)
        a = n / 255.0 if n > 1.0 else n
        a = _clamp(a)
    return Color(r=r, g=g, b=b, a=a)


def _extract_colors(text: str) -> list[Color]:
    out: list[Color] = []
    for hm in BARE_HEX_RE.finditer(text):
        with contextlib.suppress(Exception):
            out.append(_hex_to_color(hm.group(0)))
    for rm in RGB_RE.finditer(text):
        c = _match_rgba(rm)
        if c is not None:
            out.append(c)
    return out


def _display_colors(colors: Sequence[Color], max_colors: int = 200) -> None:
    seen: dict[tuple[int, int, int, float], Color] = {}
    for c in colors:
        seen[c.as_tuple()] = c
    unique = list(seen.values())
    unique.sort(key=lambda c: (_luminance(c.r, c.g, c.b), c.r, c.g, c.b))
    if len(unique) > max_colors:
        unique = unique[:max_colors]
    print(f"Found {len(seen)} unique colors (showing {len(unique)}).")
    for c in unique:
        bg = _bg(c.r, c.g, c.b)
        fg = _fg(*_text_color(c.r, c.g, c.b))
        hex_s = c.to_hex()
        rgba_s = f"rgba({c.r},{c.g},{c.b},{c.a:.3f})"
        print(f"{bg}{fg}  {hex_s}  {RESET}")
        print(f"{bg}{fg}  {rgba_s}  {RESET}")
        print()


def _decode_file(path: Path, max_size: int) -> Optional[str]:
    try:
        if path.stat().st_size > max_size:
            return None
        raw = path.read_bytes()
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                return raw.decode(enc, errors="strict")
            except Exception:
                pass
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def cmd_html(args: argparse.Namespace) -> int:
    in_path = Path(args.input)
    out_path = Path(args.output)
    with in_path.open(encoding="utf-8") as f:
        lines = f.readlines()
    colors: list[str] = []
    for d in lines:
        s = d.strip()
        if not s:
            continue
        if s.startswith("#"):
            colors.append(s)
        else:
            colors.append(f"#{s}")
    parts: list[str] = [
        "<html>",
        "<head>",
        "<title>Color Display</title>",
        "</head>",
        "<body>",
    ]
    for c in colors:
        parts.append(f'<div style="background-color:{c}">{c}</div>')
    parts.append("</body>")
    parts.append("</html>")
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"{out_path} created")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    root = Path(args.path)
    found: set[str] = set()
    for f in _walk_files(root):
        if is_binary(str(f)):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found.update(HEX_RE.findall(content))
    count = len(found)
    expanded: list[str] = []
    for c in found:
        expanded.append(c * 2 if len(c) == 3 else c)
    result = sorted({c for c in expanded if len(c) != 3})
    Path(args.output).write_text("\n".join(result), encoding="utf-8")
    cprint(f"{count} colors found", "green")
    return 0


def cmd_extract_show(args: argparse.Namespace) -> int:
    root = Path(args.path)
    allowed_ext: set[str] = set(args.ext or [])
    collected: list[Color] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in allowed_ext and is_binary(str(f)):
            continue
        content = _decode_file(f, args.max_size)
        if not content:
            continue
        found = _extract_colors(content)
        if found:
            collected.extend(found)
    if not collected:
        print("No colors found.")
        return 0
    _display_colors(collected, max_colors=args.max_colors)
    return 0


def cmd_hex2rgb(args: argparse.Namespace) -> int:
    h = args.hex.lstrip("#")
    if not args.allow_short and len(h) == 3:
        print("Error: short hex requires --allow-short", file=sys.stderr)
        return 1
    if len(h) == 3:
        r = int(h[0] * 2, 16)
        g = int(h[1] * 2, 16)
        b = int(h[2] * 2, 16)
    elif len(h) == 6:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    else:
        print(f"Error: unexpected hex length {len(h)}", file=sys.stderr)
        return 1
    if args.format == "tuple":
        print((r, g, b))
    else:
        print({"r": r, "g": g, "b": b})
    return 0


def cmd_showcolor(args: argparse.Namespace) -> int:
    n = args.count if args.count is not None else random.randrange(1000)
    for _ in range(1, n):
        r = random.randrange(256)
        g = random.randrange(256)
        b = random.randrange(256)
        print(f"{_bg(r, g, b)}        {RESET} {r!s} {g!s} {b!s}")
    return 0


def cmd_sorthue(args: argparse.Namespace) -> int:
    p = Path(args.file)

    def key(line: str) -> tuple[float, float, float]:
        r = int(line[1:3], 16) / 255
        g = int(line[3:5], 16) / 255
        b = int(line[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    with p.open(encoding="utf-8") as f:
        colors = [n.strip() for n in f if FULL_HEX_RE.match(n.strip())]
    colors.sort(key=key)
    with p.open("w", encoding="utf-8") as f:
        f.writelines(c.lower() + "\n" for c in colors)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Merged color utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("html", help="Write an HTML color display page.")
    p.add_argument("input", nargs="?", default="/sdcard/colors")
    p.add_argument("output", nargs="?", default="/sdcard/colors.html")
    p.set_defaults(func=cmd_html)
    p = sub.add_parser("extract", help="Scan for hex colors and write them out.")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--output", default="colors")
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("extract-show", help="Scan and print ANSI color swatches.")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--max-colors", type=int, default=200)
    p.add_argument("--max-size", type=int, default=5_000_000)
    p.add_argument(
        "--ext",
        action="append",
        default=[],
        help="File extension to include (repeatable).",
    )
    p.set_defaults(func=cmd_extract_show)
    p = sub.add_parser("hex2rgb", help="Convert a hex color to RGB.")
    p.add_argument("hex")
    p.add_argument("--format", choices=["tuple", "dict"], default="dict")
    p.add_argument("--allow-short", action="store_true", help="Allow 3-digit #abc form.")
    p.set_defaults(func=cmd_hex2rgb)
    p = sub.add_parser("showcolor", help="Print random ANSI color blocks.")
    p.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of blocks (default: random 1-999).",
    )
    p.set_defaults(func=cmd_showcolor)
    p = sub.add_parser("sorthue", help="Sort #rrggbb lines by HSV hue.")
    p.add_argument("file")
    p.set_defaults(func=cmd_sorthue)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
