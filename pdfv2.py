#!/data/data/com.termux/files/usr/bin/env python
"""View a PDF in the terminal, rendered as an image.
Renders the pages through an external ``pdftoppm``-style helper into PPM, draws them in the alternate screen with the ANSI colour blocks and lets you scroll, change the page and zoom (``-z``) with the keyboard.
Usage: pdfv2 FILE.pdf [-p PAGE] [-z ZOOM] Positional arguments: file Path to the PDF file Options: -p, --page Page to open first (1-based, default 1) -z, --zoom Initial zoom factor (default 1.0) Examples: $ pdfv2 slides.pdf -z 1.5 Command-line script: run ``python pdfv2.py`` or the ``pdfv2`` command.
The module is importable as well."""

import argparse
import contextlib
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import tty
from typing import Any, Sequence

RESET: str = "\x1b[0m"
HOME: str = "\x1b[H"
CLEAR: str = "\x1b[2J"
HIDE_CURSOR: str = "\x1b[?25l"
SHOW_CURSOR: str = "\x1b[?25h"
ENTER_ALT: str = "\x1b[?1049h"
LEAVE_ALT: str = "\x1b[?1049l"
REVERSE: str = "\x1b[7m"
HALF_BLOCK: str = "\u2580"
DEVNULL = subprocess.DEVNULL
WHITESPACE: bytes = b" \t\r\n\v\f"
PAGES_RE = re.compile(rb"^Pages:\s*(\d+)", re.MULTILINE)


def parse_ppm(data: bytes) -> tuple[int, int, bytes]:
    if len(data) < 2 or data[:2] != b"P6":
        msg = "renderer did not produce a P6 PPM image"
        raise RuntimeError(msg)
    pos = 2
    n = len(data)
    fields: list[int] = []
    while len(fields) < 3:
        while pos < n and data[pos] in WHITESPACE:
            pos += 1
        if pos >= n:
            msg = "truncated PPM header"
            raise RuntimeError(msg)
        if data[pos] == 0x23:
            while pos < n and data[pos] != 0x0A:
                pos += 1
            continue
        start = pos
        while pos < n and data[pos] not in WHITESPACE:
            pos += 1
        try:
            fields.append(int(data[start:pos]))
        except ValueError:
            msg = "malformed PPM header"
            raise RuntimeError(msg) from None
    pos += 1
    w, h, maxval = fields
    if maxval != 255:
        msg = f"unsupported PPM maxval {maxval}"
        raise RuntimeError(msg)
    need = w * h * 3
    raster = data[pos : pos + need]
    if len(raster) != need:
        msg = "truncated PPM raster"
        raise RuntimeError(msg)
    return w, h, raster


class RenderError(RuntimeError):
    pass


class Renderer:
    CACHE_LIMIT = 6

    def __init__(self, path: str) -> None:
        self.path = path
        self.tool = self._detect_tool()
        self._tmp = tempfile.mkdtemp(prefix="tpv-")
        self._cache: dict[tuple[int, int], tuple[int, int, bytes]] = {}
        self._count: int | None = None

    @staticmethod
    def _detect_tool() -> str:
        if shutil.which("pdftoppm"):
            return "pdftoppm"
        if shutil.which("mutool"):
            return "mutool"
        sys.exit(
            "tpv: no PDF rasteriser found.\n"
            "Install one of these Termux packages and retry:\n"
            "    pkg install poppler        (provides pdftoppm + pdfinfo)\n"
            "    pkg install mupdf-tools    (provides mutool)"
        )

    def close(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def page_count(self) -> int:
        if self._count is not None:
            return self._count
        for argv in (["pdfinfo", self.path], ["mutool", "info", self.path]):
            if not shutil.which(argv[0]):
                continue
            try:
                res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=DEVNULL, timeout=60)
            except (OSError, subprocess.SubprocessError):
                continue
            if res.returncode != 0:
                continue
            m = PAGES_RE.search(res.stdout)
            if m:
                self._count = int(m.group(1))
                return self._count
        msg = "could not determine the page count (install poppler's pdfinfo or mupdf's mutool)"
        raise RenderError(msg)

    def _wipe_tmp(self) -> None:
        for name in os.listdir(self._tmp):
            with contextlib.suppress(OSError):
                os.unlink(Path(self._tmp).joinpath(name))

    def _run_pdftoppm(self, index: int, width: int) -> bytes:
        self._wipe_tmp()
        root = Path(self._tmp).joinpath("page")
        argv = [
            "pdftoppm",
            "-f",
            str(index + 1),
            "-l",
            str(index + 1),
            "-scale-to-x",
            str(width),
            "-scale-to-y",
            "-1",
            "-singlefile",
            self.path,
            root,
        ]
        res = subprocess.run(argv, stdout=DEVNULL, stderr=subprocess.PIPE, timeout=300)
        if res.returncode != 0:
            raise RenderError(res.stderr.decode("utf-8", "replace").strip() or "pdftoppm failed")
        out = root + ".ppm"
        if not Path(out).exists():
            leftovers = [Path(self._tmp).joinpath(f) for f in sorted(os.listdir(self._tmp))]
            if not leftovers:
                msg = "pdftoppm produced no output"
                raise RenderError(msg)
            out = leftovers[0]
        with open(out, "rb") as fh:
            return fh.read()

    def _run_mutool(self, index: int, width: int) -> bytes:
        out = Path(self._tmp).joinpath("page.ppm")
        argv = [
            "mutool",
            "draw",
            "-F",
            "ppm",
            "-o",
            out,
            "-w",
            str(width),
            self.path,
            str(index + 1),
        ]
        res = subprocess.run(argv, stdout=DEVNULL, stderr=subprocess.PIPE, timeout=300)
        if res.returncode != 0:
            raise RenderError(res.stderr.decode("utf-8", "replace").strip() or "mutool draw failed")
        try:
            with open(out, "rb") as fh:
                return fh.read()
        except OSError:
            msg = "mutool draw produced no output"
            raise RenderError(msg) from None

    def page(self, index: int, width: int) -> tuple[int, int, bytes]:
        key = (index, width)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            if self.tool == "pdftoppm":
                raw = self._run_pdftoppm(index, width)
            else:
                raw = self._run_mutool(index, width)
        except subprocess.TimeoutExpired:
            msg = "renderer timed out"
            raise RenderError(msg) from None
        result = parse_ppm(raw)
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = result
        return result


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
    MAX_RENDER_WIDTH = 4000

    def __init__(self, path: str, page: int = 1, zoom: float = 1.0) -> None:
        self.path = path
        self.renderer = Renderer(path)
        self.page_count = self.renderer.page_count()
        if self.page_count == 0:
            msg = "document contains no pages"
            raise RenderError(msg)
        self.page_index = max(0, min(page - 1, self.page_count - 1))
        self.zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        self.x = 0
        self.y = 0
        self.running = True

    def term_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines

    def view_rows(self) -> int:
        _, lines = self.term_size()
        return max(1, lines - 1)

    def render_width(self) -> int:
        cols, _ = self.term_size()
        return max(1, min(self.MAX_RENDER_WIDTH, round(cols * self.zoom)))

    def page_px_size(self) -> tuple[int, int]:
        w, h, _ = self.renderer.page(self.page_index, self.render_width())
        return w, h

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

    def _status(self, cols: int, note: str = "") -> str:
        name = Path(self.path).name
        if note:
            line = f" {note} "
        else:
            try:
                _, h = self.page_px_size()
            except RenderError:
                h = 0
            rows = self.view_rows()
            max_y = max(0, h - rows * 2)
            pct = 100 if max_y == 0 else round(100 * self.y / max_y)
            line = f" {name}  {self.page_index + 1}/{self.page_count}  {pct:3d}%  {self.zoom:.2f}x "
        hint = " q quit  n/p page  j/k scroll  +/- zoom "
        if len(line) + len(hint) <= cols:
            line = line + " " * (cols - len(line) - len(hint)) + hint
        return REVERSE + line[:cols].ljust(cols) + RESET

    def draw(self) -> None:
        cols, _ = self.term_size()
        rows = self.view_rows()
        width = self.render_width()
        error: str | None = None
        try:
            w, h, data = self.renderer.page(self.page_index, width)
        except RenderError as exc:
            error = str(exc)
            w = h = 1
            data = b"\x00\x00\x00"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            w = h = 1
            data = b"\x00\x00\x00"
        if error is None:
            max_y = max(0, h - rows * 2)
            self.y = max(0, min(self.y, max_y))
            max_x = max(0, w - cols)
            self.x = max(0, min(self.x, max_x))
        buf = [HOME]
        if error:
            buf.append(RESET)
            buf.append(f" {error} ".ljust(cols)[:cols])
            for _ in range(rows - 1):
                buf.append("\r\n")
        else:
            for row in range(rows):
                buf.append(self._paint_row(data, w, h, self.y + row * 2, self.x, cols))
                buf.append("\r\n")
        buf.append(RESET)
        buf.append(self._status(cols, error or ""))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def goto_page(self, index: int) -> None:
        if 0 <= index < self.page_count:
            self.page_index = index
            self.x = self.y = 0

    def screen_down(self) -> None:
        step = self.view_rows() * 2
        _, h = self.page_px_size()
        max_y = max(0, h - step)
        if self.y >= max_y:
            if self.page_index + 1 < self.page_count:
                self.page_index += 1
                self.x = self.y = 0
        else:
            self.y = min(self.y + step, max_y)

    def screen_up(self) -> None:
        step = self.view_rows() * 2
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
        old_w = self.render_width()
        _, old_h = self.page_px_size()
        frac = self.y / old_h if old_h else 0.0
        self.zoom = value
        new_w = self.render_width()
        new_h = max(1, round(old_h * new_w / old_w))
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
                self.renderer._cache.clear()
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
    parser.add_argument("-p", "--page", type=int, default=1, help="page to open first (1-based, default 1)")
    parser.add_argument("-z", "--zoom", type=float, default=1.0, help="initial zoom factor (default 1.0)")
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
        viewer.renderer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
