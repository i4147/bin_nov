#!/data/data/com.termux/files/usr/bin/env python
"""Browse the RGB/256-colour palette in an interactive terminal grid.
Draws a paged grid of colour cells with their codes, let the arrow keys/page keys move around and prints the selected colour values; ``--demo`` cycles through the palette automatically.
Usage: pyrgb [--demo] Examples: $ pyrgb Runs as a script (``python pyrgb.py``) in the directory it should act on; the module can also be imported."""

import os
import select
import sys
import termios
import tty


RESET = "\033[0m"
SEQ_MAP = {
    b"\x1b[5~": "pgup",
    b"\x1b[6~": "pgdn",
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
}
CELL_W = 15
CELL_PITCH = 17


def parse_args(argv):
    demo = False
    step = 17
    step_given = False
    positional = []
    i = 0
    args = list(argv)
    while i < len(args):
        a = args[i]
        if a in ("-d", "--demo"):
            demo = True
        elif a == "--step":
            i += 1
            if i >= len(args):
                sys.exit("error: --step requires a value")
            try:
                step = int(args[i])
            except ValueError:
                sys.exit(f"error: --step needs an integer, got {args[i]!r}")
            if step < 1:
                sys.exit("error: --step must be >= 1")
            step_given = True
        elif a in ("-h", "--help"):
            print_usage()
            sys.exit(0)
        else:
            positional.append(a)
        i += 1
    if demo:
        return {"demo": True, "step": step}
    if step_given:
        sys.exit("error: --step is only used with -d/--demo")
    if len(positional) != 3:
        sys.exit("usage: rgb.py R G B   or   rgb.py -d [--step N]")
    try:
        r, g, b = (int(v) for v in positional)
    except ValueError:
        sys.exit(f"error: not integers: {' '.join(positional)}")
    for name, v in zip("RGB", (r, g, b)):
        if not 0 <= v <= 255:
            sys.exit(f"error: {name}={v} out of range 0-255")
    return {"demo": False, "rgb": (r, g, b)}


def print_usage():
    print("usage: rgb.py R G B           show one color")
    print("       rgb.py -d [--step N]    browse all colors page by page")
    print()
    print("options:")
    print("  -d, --demo     page through the RGB color space")
    print("  --step N       demo resolution step (default 17)")
    print("                   51 → 216 web-safe colors, usually 1 page")
    print("                   17 → 4096 colors, ~7-50 pages depending on screen")
    print("                    1 → all 16,777,216 colors")
    print("  -h, --help     show this help")
    print()
    print("keys in demo mode:")
    print("  PgUp / PgDn    previous / next page")
    print("  ↑ / ↓          previous / next page")
    print("  b / space      previous / next page")
    print("  g / G          first / last page")
    print("  q / Esc        quit")


def get_terminal_size():
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24


def compute_layout():
    term_w, term_h = get_terminal_size()
    cols = max(1, term_w // CELL_PITCH)
    rows = max(1, term_h - 4)
    return cols, cols * rows


def show_color(r, g, b):
    hexcode = f"#{r:02x}{g:02x}{b:02x}"
    fg = f"\033[38;2;{r};{g};{b}m"
    bg = f"\033[48;2;{r};{g};{b}m"
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    text = "\033[38;2;0;0;0m" if lum > 128 else "\033[38;2;255;255;255m"
    print(f"RGB   {r:>3} {g:>3} {b:>3}")
    print(f"HEX   {hexcode}")
    print()
    print(f"  {bg}                    {RESET}  solid block")
    print(f"  {bg}{text}    {hexcode}    {RESET}  labeled block")
    print(f"  {fg}████████████████████{RESET}  foreground text")
    print()


def get_values(step):
    vals = list(range(0, 256, step))
    if vals[-1] != 255:
        vals.append(255)
    return vals


def color_at(idx, vals):
    n = len(vals)
    return vals[idx // (n * n)], vals[(idx // n) % n], vals[idx % n]


def render_page(vals, page, total_pages, step, cols, page_size):
    n = len(vals)
    total = n**3
    start = page * page_size
    end = min(start + page_size, total)
    out = ["\033[H\033[2J"]
    out.append(
        f"\033[1mRGB Demo\033[0m  —  page {page + 1} / {total_pages}"
        f"  —  colors {start}..{end - 1} of {total:,}"
        f"  (step {step})"
    )
    out.append("")
    idx = start
    while idx < end:
        cells = []
        for _ in range(cols):
            if idx >= end:
                break
            r, g, b = color_at(idx, vals)
            cells.append(f"\033[38;2;{r};{g};{b}m███\033[0m {r:3d} {g:3d} {b:3d}")
            idx += 1
        out.append("  ".join(cells).rstrip())
    out.append("")
    out.append("\033[2m[PgUp/PgDn] page  [↑/↓] page  [g] first  [G] last  [q] quit\033[0m")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def read_key(fd):
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        if not select.select([fd], [], [], 0.02)[0]:
            return "esc"
        ch2 = os.read(fd, 1)
        if ch2 == b"[":
            seq = b"["
            while True:
                if not select.select([fd], [], [], 0.02)[0]:
                    return "unknown"
                b = os.read(fd, 1)
                seq += b
                if 0x40 <= b[0] <= 0x7E:
                    break
            return SEQ_MAP.get(b"\x1b" + seq, "unknown")
        if ch2 == b"O":
            if not select.select([fd], [], [], 0.02)[0]:
                return "unknown"
            b = os.read(fd, 1)
            return SEQ_MAP.get(b"\x1bO" + b, "unknown")
        return "unknown"
    try:
        return ch.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"


def run_demo(step):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("error: demo mode needs an interactive terminal")
    vals = get_values(step)
    n = len(vals)
    total = n**3
    cols, page_size = compute_layout()
    total_pages = (total + page_size - 1) // page_size
    if total_pages > 1000:
        print(f"--step {step} → {total:,} colors, {total_pages:,} pages.", file=sys.stderr)
        print("That's a lot to page through. Try --step 17 instead.", file=sys.stderr)
        print("Continue anyway? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\n")
            return
        if ans != "y":
            return
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    page = 0
    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        while True:
            render_page(vals, page, total_pages, step, cols, page_size)
            key = read_key(fd)
            if key in ("q", "Q", "esc"):
                break
            if key in ("pgup", "up", "b", "B"):
                page = max(0, page - 1)
            elif key in ("pgdn", "down", " ", "n", "N"):
                page = min(total_pages - 1, page + 1)
            elif key == "g":
                page = 0
            elif key == "G":
                page = total_pages - 1
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def main(argv):
    opts = parse_args(argv)
    if opts["demo"]:
        run_demo(opts["step"])
    else:
        show_color(*opts["rgb"])


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        sys.exit(130)
