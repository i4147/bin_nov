#!/data/data/com.termux/files/usr/bin/env python
"""Edit Persian text with a live ``faprint`` preview.
Opens the file in urwid, curses, prompt_toolkit or textual (``-b``), shows the reshaped Persian rendering of the lines around the cursor and saves and quits with Ctrl+Q.
Usage: faedit FILE [-b urwid|curses|prompt_toolkit|textual] Positional arguments: file Options: -b, --backend Examples: $ faedit notes.txt -b textual Command-line script: run ``python faedit.py`` or the ``faedit`` command.
The module is importable as well."""

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Sequence

try:
    from faprint import format_persian
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core import format_persian
CTX: int = 2


def around(s: Sequence[Any], pos: Any, n=CTX):
    a = s.rfind("\n", 0, pos) + 1
    for _ in range(n):
        if a == 0:
            break
        a = s.rfind("\n", 0, a - 1) + 1
    b = pos
    for _ in range(n + 1):
        j = s.find("\n", b)
        if j < 0:
            b = len(s)
            break
        b = j + 1
    return format_persian(s[a:b])


def run_urwid(text: str):
    import urwid

    pv = urwid.Text(around(text, 0))

    class Ed(urwid.Edit):
        def keypress(self, size: int, key: str):
            if key == "ctrl q":
                raise urwid.ExitMainLoop
            key = super().keypress(size, key)
            pv.set_text(around(self.edit_text, self.edit_pos))
            return key

    ed = Ed("", text, multiline=True, allow_tab=True, edit_pos=0)
    root = urwid.Pile([
        ("weight", 2, urwid.Filler(ed, "top")),
        ("pack", urwid.Divider("─")),
        ("weight", 1, urwid.Filler(pv, "top")),
    ])
    scr = urwid.display.raw.Screen()
    scr.tty_signal_keys(start="undefined", stop="undefined")
    urwid.MainLoop(root, screen=scr).run()
    return ed.edit_text


def run_curses(text: str) -> str | None:
    os.environ.setdefault("ESCDELAY", "25")
    import curses

    lines = text.split("\n")
    st = {"y": 0, "x": 0, "top": 0}

    def main(scr: Any) -> None:
        curses.raw()
        scr.keypad(True)
        while True:
            y, x, top = st["y"], st["x"], st["top"]
            h, w = scr.getmaxyx()
            eh = max(1, h * 2 // 3)
            top = min(max(top, y - eh + 1), y)
            st["top"] = top
            left = max(0, x - w + 2)
            scr.erase()
            for i in range(eh):
                if top + i < len(lines):
                    scr.addnstr(i, 0, lines[top + i][left:], w - 1)
            if eh < h:
                scr.hline(eh, 0, curses.ACS_HLINE, w)
            pv = format_persian(lines[y])
            for i in range(h - eh - 1):
                chunk = pv[i * (w - 1) : (i + 1) * (w - 1)]
                if not chunk:
                    break
                scr.addnstr(eh + 1 + i, 0, chunk, w - 1)
            scr.move(y - top, x - left)
            k = scr.get_wch()
            if k == "\x11":
                return
            if k in ("\n", "\r", curses.KEY_ENTER):
                lines[y : y + 1] = [lines[y][:x], lines[y][x:]]
                st["y"], st["x"] = y + 1, 0
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if x:
                    lines[y] = lines[y][: x - 1] + lines[y][x:]
                    st["x"] = x - 1
                elif y:
                    st["x"] = len(lines[y - 1])
                    lines[y - 1 : y + 1] = [lines[y - 1] + lines[y]]
                    st["y"] = y - 1
            elif k == curses.KEY_DC:
                if x < len(lines[y]):
                    lines[y] = lines[y][:x] + lines[y][x + 1 :]
                elif y + 1 < len(lines):
                    lines[y : y + 2] = [lines[y] + lines[y + 1]]
            elif k == curses.KEY_LEFT:
                if x:
                    st["x"] = x - 1
                elif y:
                    st["y"], st["x"] = y - 1, len(lines[y - 1])
            elif k == curses.KEY_RIGHT:
                if x < len(lines[y]):
                    st["x"] = x + 1
                elif y + 1 < len(lines):
                    st["y"], st["x"] = y + 1, 0
            elif k in (
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_PPAGE,
                curses.KEY_NPAGE,
            ):
                d = {
                    curses.KEY_UP: -1,
                    curses.KEY_DOWN: 1,
                    curses.KEY_PPAGE: -eh,
                    curses.KEY_NPAGE: eh,
                }[k]
                st["y"] = min(max(y + d, 0), len(lines) - 1)
                st["x"] = min(x, len(lines[st["y"]]))
            elif k == curses.KEY_HOME:
                st["x"] = 0
            elif k == curses.KEY_END:
                st["x"] = len(lines[y])
            elif k == "\t":
                lines[y] = lines[y][:x] + "    " + lines[y][x:]
                st["x"] = x + 4
            elif isinstance(k, str) and k.isprintable():
                lines[y] = lines[y][:x] + k + lines[y][x:]
                st["x"] = x + 1

    curses.wrapper(main)
    return "\n".join(lines)


def run_prompt_toolkit(text: str):
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    buf = Buffer(multiline=True, document=Document(text, 0))
    edit = Window(BufferControl(buf), wrap_lines=True, height=Dimension(weight=2))
    prev = Window(
        FormattedTextControl(lambda: around(buf.text, buf.cursor_position)),
        wrap_lines=True,
        height=Dimension(weight=1),
    )
    kb = KeyBindings()

    @kb.add("c-q")
    def _(event: Any) -> None:
        event.app.exit()

    root = HSplit([edit, Window(height=1, char="─"), prev])
    Application(Layout(root, focused_element=edit), key_bindings=kb, full_screen=True).run()
    return buf.text


def run_textual(text: str):
    from rich.text import Text
    from textual.app import App
    from textual.binding import Binding
    from textual.widgets import Static, TextArea

    class FaEdit(App):
        CSS = "TextArea{height:2fr} #pv{height:1fr;border-top:solid gray}"
        BINDINGS = [Binding("ctrl+q", "save_quit", "", priority=True, show=False)]

        def compose(self):
            yield TextArea(text, id="ed")
            yield Static("", id="pv")

        def on_mount(self) -> None:
            self.query_one("#ed").focus()
            self.refresh_pv()

        def on_text_area_selection_changed(self, event: Any) -> None:
            self.refresh_pv()

        def refresh_pv(self) -> None:
            ta = self.query_one("#ed", TextArea)
            r = ta.cursor_location[0]
            lo, hi = max(0, r - CTX), min(ta.document.line_count, r + CTX + 1)
            s = "\n".join(ta.document.get_line(i) for i in range(lo, hi))
            self.query_one("#pv", Static).update(Text(format_persian(s)))

        def action_save_quit(self) -> None:
            self.exit(self.query_one("#ed", TextArea).text)

    return FaEdit().run() or text


BACKENDS: dict[str, Any] = {
    "urwid": run_urwid,
    "curses": run_curses,
    "prompt_toolkit": run_prompt_toolkit,
    "textual": run_textual,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Edit Persian text with live faprint preview (Ctrl+Q saves & quits)")
    ap.add_argument("file")
    ap.add_argument("-b", "--backend", choices=BACKENDS, default="urwid")
    args = ap.parse_args()
    path = Path(args.file)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    new = BACKENDS[args.backend](old or "")
    if new != (old or "") or old is None:
        path.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    main()
