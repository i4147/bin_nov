#!/data/data/com.termux/files/usr/bin/env python
"""Render a PDF in the terminal with PyMuPDF.
Draws the current page as text or as coloured half-blocks with the zoom factor, and handles the paging, the scrolling and the quit keys in the alternate screen.
Usage: pdfviewer FILE.pdf [-p PAGE] [-z ZOOM] Positional arguments: file Path to the PDF file Options: -p, --page Page to open first (1-based, default 1) -z, --zoom Initial zoom factor (default 1.0) Examples: $ pdfviewer manual.pdf Command-line script: run ``python pdfviewer.py`` or the ``pdfviewer`` command.
The module is importable as well."""

import argparse
import math
import os
from pathlib import Path
import select
import shutil
import sys
import termios
import tty


try:
    import fitz
except ImportError:
    try:
        import pymupdf as fitz
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")
RESET = "\x1b[0m"
HOME = "\x1b[H"
CLEAR = "\x1b[2J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ENTER_ALT = "\x1b[?1049h"
LEAVE_ALT = "\x1b[?1049l"
REVERSE = "\x1b[7m"
HALF_BLOCK = "\u2580"


def read_key(fd: int, timeout: float | None = None) -> str | None:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    first = os.read(fd, 1)
    if not first:
        return "q"
    if first != b"\x1b":
        return first.decode("utf-8", "replace")
    seq = bytearray(first)
    while len(seq) < 8:
        ready, _, _ = select.select([fd], [], [], 0.03)
        if not ready:
            break
        seq += os.read(fd, 1)
        if seq[-1:].isalpha() or seq[-1:] == b"~":
            break
    return seq.decode("latin-1")


class Viewer:
    MIN_ZOOM = 0.25
    MAX_ZOOM = 8.0
    ZOOM_STEP = 1.25
    CACHE_LIMIT = 6

    def __init__(self, path: str, page: int = 1, zoom: float = 1.0) -> None:
        self.path = path
        self.doc = fitz.open(path)
        if self.doc.page_count == 0:
            msg = "document contains no pages"
            raise ValueError(msg)
        self.page_index = max(0, min(page - 1, self.doc.page_count - 1))
        self.zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        self.x = 0
        self.y = 0
        self.running = True
        self._cache: dict[tuple[int, int], tuple[int, int, bytes]] = {}

    def term_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines

    def view_rows(self) -> int:
        _, lines = self.term_size()
        return max(1, lines - 1)

    def render_width(self) -> int:
        cols, _ = self.term_size()
        return max(1, round(cols * self.zoom))

    def page_px_size(self, index: int | None = None) -> tuple[int, int]:
        index = self.page_index if index is None else index
        width = self.render_width()
        rect = self.doc[index].rect
        scale = width / rect.width if rect.width else 1.0
        return width, max(1, math.ceil(rect.height * scale))

    def pixmap(self, index: int, width: int) -> tuple[int, int, bytes]:
        key = (index, width)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        page = self.doc[index]
        scale = width / page.rect.width if page.rect.width else 1.0
        pm = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csRGB,
            alpha=False,
        )
        data = (pm.width, pm.height, pm.samples)
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = data
        return data

    @staticmethod
    def _paint_row(data: bytes, w: int, h: int, top: int, x0: int, cols: int) -> str:
        bottom = top + 1
        have_top = 0 <= top < h
        have_bot = 0 <= bottom < h
        base_t = top * w * 3
        base_b = bottom * w * 3
        out: list[str] = []
        last_fg: tuple[int, int, int] | None = None
        last_bg: tuple[int, int, int] | None = None
        blank = False
        for i in range(cols):
            x = x0 + i
            if x >= w:
                if not blank:
                    out.append(RESET)
                    last_fg = last_bg = None
                    blank = True
                out.append(" ")
                continue
            blank = False
            if have_top:
                p = base_t + x * 3
                fg = (data[p], data[p + 1], data[p + 2])
            else:
                fg = (0, 0, 0)
            if have_bot:
                p = base_b + x * 3
                bg = (data[p], data[p + 1], data[p + 2])
            else:
                bg = (0, 0, 0)
            if fg != last_fg:
                out.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
                last_fg = fg
            if bg != last_bg:
                out.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                last_bg = bg
            out.append(HALF_BLOCK)
        out.append(RESET)
        return "".join(out)

    def _status(self, cols: int) -> str:
        name = Path(self.path).name
        total = self.doc.page_count
        _, h = self.page_px_size()
        max_y = max(0, h - self.view_rows() * 2)
        pct = 100 if max_y == 0 else round(100 * self.y / max_y)
        left = f" {name}  {self.page_index + 1}/{total}  {pct:3d}%  {self.zoom:.2f}x "
        right = " q quit  n/p page  j/k scroll  +/- zoom "
        if len(left) + len(right) <= cols:
            line = left + " " * (cols - len(left) - len(right)) + right
        else:
            line = left
        return REVERSE + line[:cols].ljust(cols) + RESET

    def draw(self) -> None:
        cols, _ = self.term_size()
        rows = self.view_rows()
        width = self.render_width()
        w, h, data = self.pixmap(self.page_index, width)
        max_y = max(0, h - rows * 2)
        self.y = max(0, min(self.y, max_y))
        max_x = max(0, w - cols)
        self.x = max(0, min(self.x, max_x))
        buf = [HOME]
        for row in range(rows):
            top = self.y + row * 2
            buf.append(self._paint_row(data, w, h, top, self.x, cols))
            if row != rows - 1:
                buf.append("\r\n")
        buf.append(RESET)
        buf.append(self._status(cols))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def goto_page(self, index: int) -> None:
        if 0 <= index < self.doc.page_count:
            self.page_index = index
            self.x = self.y = 0

    def screen_down(self) -> None:
        rows = self.view_rows()
        step = rows * 2
        _, h = self.page_px_size()
        max_y = max(0, h - step)
        if self.y >= max_y:
            if self.page_index + 1 < self.doc.page_count:
                self.page_index += 1
                self.x = self.y = 0
        else:
            self.y = min(self.y + step, max_y)

    def screen_up(self) -> None:
        rows = self.view_rows()
        step = rows * 2
        if self.y <= 0:
            if self.page_index > 0:
                self.page_index -= 1
                self.x = 0
                _, h = self.page_px_size()
                self.y = max(0, h - step)
        else:
            self.y = max(0, self.y - step)

    def set_zoom(self, value: float) -> None:
        value = max(self.MIN_ZOOM, min(self.MAX_ZOOM, value))
        if value == self.zoom:
            return
        _, old_h = self.page_px_size()
        frac = self.y / old_h if old_h else 0.0
        self.zoom = value
        _, new_h = self.page_px_size()
        self.y = int(frac * new_h)

    def handle(self, key: str) -> None:
        if key in ("q", "Q", "\x03") or key == "\x1b":
            self.running = False
        elif key in ("j", "\x1b[B", "\n", "\r"):
            self.y += 2
        elif key in ("k", "\x1b[A"):
            self.y -= 2
        elif key in ("h", "\x1b[D"):
            self.x -= 4
        elif key in ("l", "\x1b[C"):
            self.x += 4
        elif key in (" ", "\x1b[6~", "f", "J"):
            self.screen_down()
        elif key in ("b", "\x1b[5~", "K"):
            self.screen_up()
        elif key == "n":
            self.goto_page(self.page_index + 1)
        elif key in ("N", "p"):
            self.goto_page(self.page_index - 1)
        elif key in ("g", "\x1b[H", "\x1b[1~", "\x1b[7~", "\x1bOH"):
            self.x = self.y = 0
        elif key in ("G", "\x1b[F", "\x1b[4~", "\x1b[8~", "\x1bOF"):
            self.y = 1 << 30
        elif key in ("+", "="):
            self.set_zoom(self.zoom * self.ZOOM_STEP)
        elif key in ("-", "_"):
            self.set_zoom(self.zoom / self.ZOOM_STEP)
        elif key == "0":
            self.zoom = 1.0
            self.x = self.y = 0

    def run(self, fd: int) -> None:
        dirty = True
        last_size = (0, 0)
        while self.running:
            size = self.term_size()
            if size != last_size:
                last_size = size
                self._cache.clear()
                dirty = True
            if dirty:
                self.draw()
                dirty = False
            key = read_key(fd, 0.25)
            if key is None:
                continue
            self.handle(key)
            dirty = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tpv",
        description="View a PDF file in the terminal.",
    )
    parser.add_argument("file", help="path to the PDF file")
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        default=1,
        help="page to open first (1-based, default 1)",
    )
    parser.add_argument(
        "-z",
        "--zoom",
        type=float,
        default=1.0,
        help="initial zoom factor (default 1.0)",
    )
    args = parser.parse_args(argv)
    if not Path(args.file).is_file():
        parser.error(f"no such file: {args.file}")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("must be run from an interactive terminal")
    try:
        viewer = Viewer(args.file, args.page, args.zoom)
    except Exception as exc:
        sys.exit(f"tpv: could not open {args.file!r}: {exc}")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write(ENTER_ALT + HIDE_CURSOR + CLEAR + HOME)
        sys.stdout.flush()
        viewer.run(fd)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write(RESET + SHOW_CURSOR + LEAVE_ALT)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
