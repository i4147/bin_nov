#!/data/data/com.termux/files/usr/bin/env python
"""Browse the files of the current directory with a curses preview.
Lists the directory in a curses window, shows the first 20 lines of the highlighted file and lets you move with the arrow keys and quit with ``q``.
Usage: quickview Examples: $ quickview Runs as a script (``python quickview.py``) in the directory it should act on; the module can also be imported.
"""

import curses
from pathlib import Path
from typing import Any

LINES_PER_FILE: int = 20


def list_files() -> list[Path]:
    return sorted([p for p in Path().iterdir() if p.is_file()], key=lambda p: p.name.lower())


def head_lines(path: Path, n: int) -> list[str]:
    lines = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for _ in range(n):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
    except Exception as e:
        lines = [f"[Error reading file: {e}]"]
    return lines


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)


def draw(stdscr: Any, files: list[Path], idx: int) -> None:
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    header = f"File {idx + 1}/{len(files)}: {files[idx].name}"
    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addnstr(0, 0, header, w - 1)
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
    lines = head_lines(files[idx], LINES_PER_FILE)
    for i, line in enumerate(lines, start=2):
        if i >= h - 1:
            break
        if line.startswith("[Error"):
            stdscr.attron(curses.color_pair(4))
            stdscr.addnstr(i, 0, line, w - 1)
            stdscr.attroff(curses.color_pair(4))
        else:
            stdscr.attron(curses.color_pair(2))
            stdscr.addnstr(i, 0, line, w - 1)
            stdscr.attroff(curses.color_pair(2))
    footer = "PgDown: next | PgUp: prev | q: quit"
    stdscr.attron(curses.color_pair(3))
    stdscr.addnstr(h - 1, 0, footer, w - 1)
    stdscr.attroff(curses.color_pair(3))
    stdscr.refresh()


def main(stdscr: Any) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()
    files = list_files()
    if not files:
        stdscr.addstr(0, 0, "No files in directory.")
        stdscr.getch()
        return
    idx = 0
    draw(stdscr, files, idx)
    while True:
        key = stdscr.getch()
        if key in {ord("q"), ord("Q")}:
            break
        if key == curses.KEY_NPAGE:
            if idx < len(files) - 1:
                idx += 1
                draw(stdscr, files, idx)
        elif key == curses.KEY_PPAGE and idx > 0:
            idx -= 1
            draw(stdscr, files, idx)


if __name__ == "__main__":
    curses.wrapper(main)
