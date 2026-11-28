#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from secrets import randbelow

import readchar

RESET: str = "\033[0m"
CLEAR_SCREEN: str = "\033[2J\033[H"
PAGE_SIZE: int = 25
DEFAULT_COUNT: int = 50
DEFAULT_BATCHES: int = 3


def _bg(r: int, g: int, b: int) -> str:
    return f"\x1b[48;2;{r};{g};{b}m"


def cube_index_to_rgb(index: int) -> tuple[int, int, int]:
    """Convert an xterm 6x6x6 color cube index (0..215) to an RGB triplet."""
    if not 0 <= index <= 215:
        msg = f"Cube index out of range (0..215): {index}"
        raise ValueError(msg)
    steps: tuple[int, int, int, int, int, int] = (0, 95, 135, 175, 215, 255)
    r_idx: int = index // 36
    g_idx: int = (index % 36) // 6
    b_idx: int = index % 6
    return steps[r_idx], steps[g_idx], steps[b_idx]


def cube_coords_to_rgb(r_idx: int, g_idx: int, b_idx: int) -> tuple[int, int, int]:
    """Convert 6x6x6 cube coordinates (each 0..5) to an RGB triplet."""
    for axis, val in (("R", r_idx), ("G", g_idx), ("B", b_idx)):
        if not 0 <= val <= 5:
            msg = f"Cube {axis} coordinate out of range (0..5): {val}"
            raise ValueError(msg)
    index: int = r_idx * 36 + g_idx * 6 + b_idx
    return cube_index_to_rgb(index)


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse 3-digit or 6-digit hex (with or without leading '#') into RGB."""
    clean: str = hex_str.strip().lstrip("#")
    if len(clean) == 3:
        expanded: str = "".join(ch * 2 for ch in clean)
    elif len(clean) == 6:
        expanded = clean
    else:
        msg = f"Invalid hex color: '{hex_str}'"
        raise ValueError(msg)

    try:
        r: int = int(expanded[0:2], 16)
        g: int = int(expanded[2:4], 16)
        b: int = int(expanded[4:6], 16)
    except ValueError as exc:
        msg = f"Invalid hex digits in color: '{hex_str}'"
        raise ValueError(msg) from exc

    return r, g, b


def _parse_int_strict(token: str) -> int | None:
    stripped: str = token.strip()
    if not stripped:
        return None
    try:
        return int(stripped, 10)
    except ValueError:
        return None


def parse_color_tokens(
    tokens: list[str], *, force_cube: bool = False
) -> tuple[tuple[int, int, int], str | None]:
    """Parse CLI color arguments (RGB triplet, cube index/coords, or hex) into RGB + optional label."""
    if not tokens:
        msg = "No color tokens provided."
        raise ValueError(msg)

    if len(tokens) == 1:
        raw: str = tokens[0].strip()

        # Cube coordinates syntax: "cube:r,g,b" or "r,g,b" with values 0..5
        coord_payload: str | None = None
        if raw.lower().startswith("cube:"):
            coord_payload = raw.split(":", 1)[1]
        elif "," in raw and not raw.startswith("#"):
            coord_payload = raw

        if coord_payload is not None:
            parts: list[str] = [p.strip() for p in coord_payload.split(",")]
            if len(parts) != 3:
                msg = (
                    f"Invalid cube coordinate format: '{raw}' (expected r,g,b in 0..5)"
                )
                raise ValueError(msg)
            parsed_coords: list[int] = []
            for part in parts:
                val: int | None = _parse_int_strict(part)
                if val is None:
                    msg = f"Invalid cube coordinate integer: '{part}'"
                    raise ValueError(msg)
                parsed_coords.append(val)
            rgb: tuple[int, int, int] = cube_coords_to_rgb(
                parsed_coords[0],
                parsed_coords[1],
                parsed_coords[2],
            )
            return (
                rgb,
                f"cube({parsed_coords[0]},{parsed_coords[1]},{parsed_coords[2]})",
            )

        # Single integer: cube index when forced or in 0..215, otherwise invalid single int
        maybe_int: int | None = _parse_int_strict(raw)
        if maybe_int is not None:
            if force_cube:
                return cube_index_to_rgb(maybe_int), f"cube#{maybe_int}"
            msg = (
                f"Single integer '{raw}' is ambiguous; pass --cube to treat as a 0..215 cube index, "
                "or provide 3 RGB values (0..255)."
            )
            raise ValueError(msg)

        if force_cube:
            msg = f"--cube requires an integer index (0..215) or r,g,b coordinates, got '{raw}'"
            raise ValueError(msg)

        # Hex string (with or without '#')
        hex_rgb: tuple[int, int, int] = hex_to_rgb(raw)
        normalized: str = raw.lstrip("#").lower()
        if len(normalized) == 3:
            normalized = "".join(ch * 2 for ch in normalized)
        return hex_rgb, f"#{normalized}"

    if len(tokens) == 3:
        if force_cube:
            coords: list[int] = []
            for tok in tokens:
                val_c: int | None = _parse_int_strict(tok)
                if val_c is None:
                    msg = f"Invalid cube coordinate: '{tok}'"
                    raise ValueError(msg)
                coords.append(val_c)
            return cube_coords_to_rgb(
                coords[0], coords[1], coords[2]
            ), f"cube({coords[0]},{coords[1]},{coords[2]})"

        rgb_vals: list[int] = []
        for tok in tokens:
            val_rgb: int | None = _parse_int_strict(tok)
            if val_rgb is None or not 0 <= val_rgb <= 255:
                msg = f"RGB value out of range (0..255): '{tok}'"
                raise ValueError(msg)
            rgb_vals.append(val_rgb)
        return (rgb_vals[0], rgb_vals[1], rgb_vals[2]), None

    msg = f"Expected 1 color token (hex/cube) or 3 RGB integers, got {len(tokens)} tokens."
    raise ValueError(msg)


def format_swatch(r: int, g: int, b: int, label_text: str | None = None) -> str:
    """Render a terminal color block with RGB numbers and optional extra label."""
    suffix: str = f" ({label_text})" if label_text else ""
    return f"{_bg(r, g, b)}       {RESET} {r:>3} {g:>3} {b:>3}{suffix}"


def load_swatches_from_file(filepath: str) -> list[str]:
    """Load hex colors (one per line, ignoring blank lines and '#' comment lines) into swatch strings."""
    swatches: list[str] = []
    with open(filepath, "r", encoding="utf-8") as file:
        for line_num, line in enumerate(file, start=1):
            stripped_line: str = line.strip()
            if (
                not stripped_line
                or stripped_line.startswith("//")
                or stripped_line.startswith(";")
            ):
                continue
            # Allow full-line comments starting with '#' followed by space or non-hex words,
            # while still accepting '#1e1f20' or '#abc'.
            candidate: str = stripped_line
            if stripped_line.startswith("#"):
                token_part: str = stripped_line.split()[0]
                hex_body: str = token_part.lstrip("#")
                if len(hex_body) not in (3, 6) or any(
                    c not in "0123456789abcdefABCDEF" for c in hex_body
                ):
                    continue
                candidate = token_part

            try:
                r, g, b = hex_to_rgb(candidate)
                clean_hex: str = candidate.lstrip("#").lower()
                if len(clean_hex) == 3:
                    clean_hex = "".join(ch * 2 for ch in clean_hex)
                swatches.append(format_swatch(r, g, b, f"#{clean_hex}"))
            except ValueError:
                print(
                    f"Warning: Skipping invalid hex on line {line_num}: '{stripped_line}'",
                    file=sys.stderr,
                )
    return swatches


def browse_with_less(swatches: list[str]) -> int:
    """Pipe swatches to `less -R` if installed on PATH."""
    less_path: str | None = shutil.which("less")
    if not less_path:
        print(
            "Warning: 'less' not found on PATH; falling back to built-in pager.",
            file=sys.stderr,
        )
        return browse_swatches(swatches)

    payload: str = (
        "\n".join(f"{idx + 1:>5}: {row}" for idx, row in enumerate(swatches)) + "\n"
    )
    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [less_path, "-R"],
            input=payload,
            text=True,
            check=False,
        )
        return completed.returncode
    except OSError as exc:
        print(f"Error launching less: {exc}", file=sys.stderr)
        return 1


def browse_swatches(
    swatches: list[str],
    page_size: int = PAGE_SIZE,
    *,
    clear_screen: bool = True,
) -> int:
    """Interactive terminal pager for swatch lines."""
    if not swatches:
        print("No valid colors to display.")
        return 0

    total: int = len(swatches)
    offset: int = 0
    while True:
        if clear_screen:
            sys.stdout.write(CLEAR_SCREEN)
        end: int = min(offset + page_size, total)
        for idx in range(offset, end):
            print(f"{idx + 1:>5}: {swatches[idx]}")
        print("-" * 45)
        print(
            f"Showing {offset + 1}-{end} of {total} | "
            "[PgDn/Down/Space] Next | [PgUp/Up] Prev | [Home/End] Jump | [q/Esc] Quit"
        )
        try:
            key: str = readchar.readkey()
        except KeyboardInterrupt:
            print()
            break

        if key in (
            readchar.key.PAGE_DOWN,
            readchar.key.DOWN,
            " ",
            readchar.key.RIGHT,
        ):
            if offset + page_size < total:
                offset += page_size
        elif key in (readchar.key.PAGE_UP, readchar.key.UP, readchar.key.LEFT):
            offset = max(0, offset - page_size)
        elif key == readchar.key.HOME:
            offset = 0
        elif key == readchar.key.END:
            last_page_start: int = ((total - 1) // page_size) * page_size
            offset = max(0, last_page_start)
        elif key.lower() == "q" or key in (readchar.key.ESC, readchar.key.CTRL_C):
            break
    return 0


def showcolor(n: int = DEFAULT_COUNT) -> int:
    """Print `n` random 24-bit RGB swatches."""
    if n < 1:
        return 0
    for _ in range(n):
        r: int = randbelow(256)
        g: int = randbelow(256)
        b: int = randbelow(256)
        print(format_swatch(r, g, b))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Display terminal RGB color swatches from random generation, "
            "hex/RGB/cube arguments, or a hex file."
        )
    )
    parser.add_argument(
        "color",
        nargs="*",
        type=str,
        help=(
            "Color spec: hex (#1e1f20, 1e1f20, #abc), cube coords (cube:1,2,3 or 1,2,3), "
            "cube index with --cube (0..215), or 3 RGB integers (0..255)"
        ),
    )
    parser.add_argument(
        "-f",
        "--file",
        type=str,
        help="Path to a text file containing hex colors (one per line)",
    )
    parser.add_argument(
        "-c",
        "--cube",
        action="store_true",
        help="Treat a single integer argument as a 6x6x6 color cube index (0..215)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Number of random swatches per batch (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "-b",
        "--batches",
        type=int,
        default=DEFAULT_BATCHES,
        help=f"Number of random batches before prompting (default: {DEFAULT_BATCHES})",
    )
    parser.add_argument(
        "-p",
        "--page-size",
        type=int,
        default=PAGE_SIZE,
        help=f"Number of colors per page when browsing a file (default: {PAGE_SIZE})",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear the terminal screen between pages in file browser mode",
    )
    parser.add_argument(
        "--less",
        action="store_true",
        help="View file swatches using `less -R` instead of the built-in pager",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args(argv)

    if args.file:
        if not os.path.isfile(args.file):
            print(f"Error: File '{args.file}' not found.", file=sys.stderr)
            return 1
        try:
            swatches: list[str] = load_swatches_from_file(args.file)
        except OSError as exc:
            print(f"Error reading file '{args.file}': {exc}", file=sys.stderr)
            return 1
        if args.less:
            return browse_with_less(swatches)
        return browse_swatches(
            swatches,
            page_size=max(1, args.page_size),
            clear_screen=not args.no_clear,
        )

    if args.color:
        try:
            (r, g, b), label = parse_color_tokens(
                args.color, force_cube=bool(args.cube)
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(format_swatch(r, g, b, label))
        return 0

    batches: int = max(1, args.batches)
    count: int = max(1, args.count)
    for _ in range(batches):
        showcolor(count)
        try:
            ans: str = input("continue ? ")
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if ans.strip().lower() != "y":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
