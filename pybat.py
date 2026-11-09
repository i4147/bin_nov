#!/data/data/com.termux/files/usr/bin/env python
"""A Python port of ``bat``: cat with syntax highlighting.
Same feature set as :doc:`pbat` (Pygments highlighting, line numbers, git markers, themes, line ranges and paging) with the streaming reader used here.
Usage: pybat [files ...] [-l LANG] [--theme THEME] [-r START:END] Positional arguments: files ...
Files to display (use '-' for stdin) Options: -l, --language Set the language for syntax highlighting --theme Color theme (see --list-themes) -p, --plain Plain output: no line numbers, header, grid or git markers --style Output style -r, --line-range Only show given line range, e.g.
'30:40', '30:', ':40' -H, --highlight-line Highlight a specific line (repeatable) --paging --color --tabs --list-languages ...
plus 1 more; run ``pybat --help`` for the full list.
Examples: $ pybat README.md -r 1:30 Command-line script: run ``python pybat.py`` or the ``pybat`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Optional

from pygments import highlight as pyg_highlight
from pygments.formatters import Terminal256Formatter, TerminalTrueColorFormatter
from pygments.lexers import TextLexer, get_all_lexers, get_lexer_by_name, get_lexer_for_filename, guess_lexer
from pygments.styles import get_all_styles, get_style_by_name
from pygments.util import ClassNotFound


RESET: str = "\x1b[0m"
GRID_COLOR: str = "\x1b[38;5;238m"
HEADER_COLOR: str = "\x1b[38;5;81m"
LINE_NUM_COLOR: str = "\x1b[38;5;244m"
ADDED_COLOR: str = "\x1b[38;5;150m"
MODIFIED_COLOR: str = "\x1b[38;5;222m"
REMOVED_COLOR: str = "\x1b[38;5;203m"
HIGHLIGHT_BG: str = "\x1b[48;5;236m"
BOX_H, BOX_V = "─", "│"
BOX_TL, BOX_TR, BOX_BL, BOX_BR = "╭", "╮", "╰", "╯"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class BatConfig:
    files: list = field(default_factory=list)
    language: Optional[str] = None
    theme: str = "monokai"
    show_numbers: bool = True
    show_grid: bool = True
    show_header: bool = True
    show_changes: bool = True
    plain: bool = False
    line_range: Optional[tuple] = None
    highlight_lines: set = field(default_factory=set)
    paging: str = "auto"
    tab_width: int = 4
    color: str = "auto"

    def apply_plain(self) -> None:
        if self.plain:
            self.show_numbers = False
            self.show_grid = False
            self.show_header = False
            self.show_changes = False


class GitDiffCalculator:
    def __init__(self, filepath: Path) -> None:
        self.filepath = filepath
        self.added: set = set()
        self.modified: set = set()
        self.removed_before: dict = {}
        self._compute()

    def _compute(self) -> None:
        if not shutil.which("git"):
            return
        try:
            top = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.filepath.parent),
                    "rev-parse",
                    "--show-toplevel",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if top.returncode != 0:
                return
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.filepath.parent),
                    "diff",
                    "--no-color",
                    "-U0",
                    "--",
                    self.filepath.name,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout:
                self._parse_hunks(result.stdout)
        except Exception:
            pass

    def _parse_hunks(self, diff_text: str) -> None:
        for line in diff_text.splitlines():
            if not line.startswith("@@"):
                continue
            try:
                parts = line.split()
                old_part = parts[1].lstrip("-")
                new_part = parts[2].lstrip("+")
                old_count = int(old_part.split(",")[1]) if "," in old_part else 1
                new_start_s, *rest = new_part.split(",")
                new_start = int(new_start_s)
                new_count = int(rest[0]) if rest else 1
            except Exception:
                continue
            if old_count > 0 and new_count > 0:
                for i in range(new_count):
                    self.modified.add(new_start + i)
            elif old_count > 0 and new_count == 0:
                key = new_start + 1
                self.removed_before[key] = self.removed_before.get(key, 0) + old_count
            elif old_count == 0 and new_count > 0:
                for i in range(new_count):
                    self.added.add(new_start + i)

    def status_for(self, line_no: int) -> Optional[str]:
        if line_no in self.added:
            return "added"
        if line_no in self.modified:
            return "modified"
        return None

    def removed_marker(self, line_no: int) -> int:
        return self.removed_before.get(line_no, 0)


class Printer:
    def __init__(self, config: BatConfig) -> None:
        self.config = config

    def print_file(self, filepath: Optional[Path], content: str) -> str:
        lines = content.splitlines()
        total = len(lines)
        lexer = self._get_lexer(filepath, content)
        formatter = self._get_formatter()
        git = GitDiffCalculator(filepath) if (filepath and self.config.show_changes) else None
        start, end = self._resolve_range(total)
        width = shutil.get_terminal_size((100, 24)).columns
        out = io.StringIO()
        if self.config.show_header:
            self._print_header(out, filepath, width)
        elif self.config.show_grid:
            out.write(f"{GRID_COLOR}{BOX_H * width}{RESET}\n")
        highlighted = self._highlight_lines(lines, lexer, formatter)
        for idx in range(start, end + 1):
            if 1 <= idx <= total:
                text = highlighted[idx - 1] if idx - 1 < len(highlighted) else ""
                self._print_line(out, idx, text, git)
        if self.config.show_grid:
            out.write(f"{GRID_COLOR}{BOX_H * width}{RESET}\n")
        return out.getvalue()

    def _get_lexer(self, filepath: Path, content: str):
        if self.config.language:
            try:
                return get_lexer_by_name(self.config.language, stripnl=False, tabsize=self.config.tab_width)
            except ClassNotFound:
                pass
        if filepath:
            try:
                return get_lexer_for_filename(str(filepath), content, stripnl=False, tabsize=self.config.tab_width)
            except ClassNotFound:
                pass
        try:
            return guess_lexer(content, stripnl=False)
        except ClassNotFound:
            return TextLexer(stripnl=False)

    def _get_formatter(self):
        if self.config.color == "never":
            return None
        try:
            style = get_style_by_name(self.config.theme)
        except ClassNotFound:
            style = get_style_by_name("default")
        if "truecolor" in os.environ.get("COLORTERM", "") or "24bit" in os.environ.get("COLORTERM", ""):
            return TerminalTrueColorFormatter(style=style)
        return Terminal256Formatter(style=style)

    def _highlight_lines(self, lines: list[Any], lexer: Any, formatter: Any):
        if self.config.plain or formatter is None:
            return lines
        code = "\n".join(lines) + "\n"
        highlighted = pyg_highlight(code, lexer, formatter).split("\n")
        if highlighted and highlighted[-1] == "":
            highlighted.pop()
        return highlighted

    def _resolve_range(self, total: Any):
        if self.config.line_range:
            s, e = self.config.line_range
            e = e if e is not None else total
            return max(1, s), min(total, e)
        return 1, total

    def _print_header(self, out: Path, filepath: Path, width: int) -> None:
        name = str(filepath) if filepath else "STDIN"
        title = f" {name} "
        left = BOX_H * 2
        right = BOX_H * max(width - len(left) - len(title) - 2, 0)
        out.write(f"{GRID_COLOR}{BOX_TL}{left}{RESET}{HEADER_COLOR}{title}{RESET}{GRID_COLOR}{right}{BOX_TR}{RESET}\n")

    def _print_line(self, out: Path, line_no: int, text: str, git: Any) -> None:
        prefix_parts = []
        if self.config.show_changes:
            marker, color = " ", ""
            if git:
                if git.removed_marker(line_no):
                    marker, color = "‾", REMOVED_COLOR
                else:
                    status = git.status_for(line_no)
                    if status == "added":
                        marker, color = "+", ADDED_COLOR
                    elif status == "modified":
                        marker, color = "~", MODIFIED_COLOR
            prefix_parts.append(f"{color}{marker}{RESET}" if color else marker)
        if self.config.show_numbers:
            prefix_parts.append(f"{LINE_NUM_COLOR}{line_no:>4}{RESET}")
        if self.config.show_grid:
            prefix_parts.append(f"{GRID_COLOR}{BOX_V}{RESET}")
        prefix = " ".join(prefix_parts)
        line_out = f"{HIGHLIGHT_BG}{text}{RESET}" if line_no in self.config.highlight_lines else text
        out.write(f"{prefix} {line_out}\n" if prefix else f"{line_out}\n")


def parse_line_range(s: str):
    if ":" in s:
        a, b = s.split(":", 1)
        return (int(a) if a else 1, int(b) if b else None)
    n = int(s)
    return (n, n)


def read_input(path: str):
    if path == "-":
        return None, sys.stdin.read()
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        print(f"pybat: {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if b"\x00" in data[:8192]:
        print(
            f"pybat: {path}: binary file detected, use --plain to force display",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return p, data.decode("utf-8")
    except UnicodeDecodeError:
        return p, data.decode("utf-8", errors="replace")


def list_languages() -> None:
    for name, aliases, _, _ in sorted(get_all_lexers(), key=lambda x: x[0].lower()):
        print(f"{name:30} {', '.join(aliases)}")


def list_themes() -> None:
    for style in sorted(get_all_styles()):
        print(style)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pybat",
        description="A Python port of the Rust `bat` crate — cat clone with syntax highlighting.",
    )
    p.add_argument("files", nargs="*", help="Files to display (use '-' for stdin)")
    p.add_argument("-l", "--language", help="Set the language for syntax highlighting")
    p.add_argument("--theme", default="monokai", help="Color theme (see --list-themes)")
    p.add_argument(
        "-p",
        "--plain",
        action="store_true",
        help="Plain output: no line numbers, header, grid or git markers",
    )
    p.add_argument(
        "--style",
        default="full",
        choices=["full", "plain", "numbers", "grid", "header", "changes"],
        help="Output style",
    )
    p.add_argument(
        "-r",
        "--line-range",
        help="Only show given line range, e.g. '30:40', '30:', ':40'",
    )
    p.add_argument(
        "-H",
        "--highlight-line",
        action="append",
        default=[],
        type=int,
        help="Highlight a specific line (repeatable)",
    )
    p.add_argument("--paging", default="auto", choices=["auto", "always", "never"])
    p.add_argument("--color", default="auto", choices=["auto", "always", "never"])
    p.add_argument("--tabs", type=int, default=4, dest="tab_width")
    p.add_argument("--list-languages", action="store_true")
    p.add_argument("--list-themes", action="store_true")
    return p


def config_from_args(args: list[Any]) -> BatConfig:
    cfg = BatConfig(
        files=args.files,
        language=args.language,
        theme=args.theme,
        paging=args.paging,
        tab_width=args.tab_width,
        color=args.color,
        plain=args.plain,
        highlight_lines=set(args.highlight_line),
    )
    style_reset = {
        "show_numbers": False,
        "show_grid": False,
        "show_header": False,
        "show_changes": False,
    }
    if args.style == "plain":
        cfg.plain = True
    elif args.style in ("numbers", "grid", "header", "changes"):
        for k, v in style_reset.items():
            setattr(cfg, k, v)
        setattr(cfg, f"show_{args.style}", True)
    cfg.apply_plain()
    if args.line_range:
        cfg.line_range = parse_line_range(args.line_range)
    return cfg


def should_page(cfg: BatConfig, line_count: int) -> bool:
    if cfg.paging == "always":
        return True
    if cfg.paging == "never":
        return False
    if not sys.stdout.isatty():
        return False
    return line_count > shutil.get_terminal_size((100, 24)).lines


def page_output(text: str) -> None:
    pager = os.environ.get("PAGER", "less")
    try:
        proc = subprocess.Popen([pager, "-R"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-8"))
    except FileNotFoundError:
        sys.stdout.write(text)


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.list_languages:
        list_languages()
        return 0
    if args.list_themes:
        list_themes()
        return 0
    if not args.files:
        args.files = ["-"]
    cfg = config_from_args(args)
    printer = Printer(cfg)
    chunks = []
    for f in cfg.files:
        filepath, content = read_input(f)
        chunks.append(printer.print_file(filepath, content))
    final_text = "".join(chunks)
    if cfg.color == "never":
        final_text = ANSI_RE.sub("", final_text)
    if should_page(cfg, final_text.count("\n")):
        page_output(final_text)
    else:
        sys.stdout.write(final_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
