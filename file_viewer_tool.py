#!/data/data/com.termux/files/usr/bin/env python
"""Terminal file viewer: cat files with optional syntax highlighting.
Analysis / deduplication ------------------------ - ``pbat.py`` is a 394-line ``bat`` clone (Pygments highlighting, line numbers, headers, ranges, paging, git change gutter).
It is the ``view`` command below, simplified to its useful core: numbers, header, ``-r START:END``, ``-H`` highlight lines, theme/language, paging and colour control.
Pygments is optional: without it you get plain ``cat`` with the same decorations.
- ``fv.py`` (page one file via ``pydoc``) is ``view file`` (auto-paging).
- ``qv.py`` (page every file in a dir) is ``browse``.
- ``pyhead.py`` (first KiB of a file) is ``head --bytes 1024``.
- ``quickview.py`` (curses browser, 20-line previews) is ``browse --interactive``.
- ``faprinter.py`` just passed lines through an external ``faprint`` package; that display nicety is dropped (``view`` covers printing).
Examples -------- python3 file_viewer_tool.py view script.py python3 file_viewer_tool.py view -r 10:40 -H 17 --paging never script.py python3 file_viewer_tool.py view --list-themes python3 file_viewer_tool.py head big.log -n 50 python3 file_viewer_tool.py browse src/ -r
"""

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import pydoc
import sys

RESET: str = "\x1b[0m"
NUM_COLOR: str = "\x1b[38;5;244m"
HEADER_COLOR: str = "\x1b[38;5;81m"
HIGHLIGHT_BG: str = "\x1b[48;5;236m"
DEFAULT_BYTES: int = 1024
PREVIEW_LINES: int = 20


def parse_range(spec: str | None) -> tuple[int | None, int | None]:
    """Parse ``START:END`` (either side optional) into 1-based bounds."""
    if not spec:
        return None, None
    left, _, right = spec.partition(":")
    start: int | None = int(left) if left.strip().isdigit() else None
    end: int | None = int(right) if right.strip().isdigit() else None
    return start, end


def highlight_text(text: str, filename: str, language: str | None, theme: str) -> str:
    """Syntax-highlight *text* with Pygments; fall back to plain text."""
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename, guess_lexer
        from pygments.util import ClassNotFound
    except ImportError:
        return text
    try:
        lexer = get_lexer_by_name(language) if language else get_lexer_for_filename(filename, text)
    except ClassNotFound:
        try:
            lexer = guess_lexer(text)
        except ClassNotFound:
            lexer = TextLexer()
    try:
        return str(highlight(text, lexer, Terminal256Formatter(style=theme)))
    except Exception:
        return text


def render(name: str, text: str, args: argparse.Namespace, color: bool) -> str:
    """Render one file: optional header, numbers, range and highlights."""
    lines: list[str] = text.splitlines()
    start, end = parse_range(args.line_range)
    lo: int = start or 1
    hi: int = len(lines) if end is None else min(end, len(lines))
    body: str = "\n".join(lines[lo - 1 : hi])
    if color and not args.plain:
        body = highlight_text(body + "\n", name, args.language, args.theme).rstrip("\n")
    shown: list[str] = body.splitlines()
    width: int = len(str(hi))
    highlighted: set[int] = set(args.highlight_line)
    out: list[str] = []
    if not args.plain:
        out.append(f"{HEADER_COLOR if color else ''}--- {name} ({len(lines)} lines){RESET if color else ''}")
    for offset, line in enumerate(shown):
        lineno: int = lo + offset
        expanded: str = line.expandtabs(args.tabs)
        if args.plain:
            out.append(expanded)
            continue
        gutter: str = f"{NUM_COLOR}{lineno:>{width}}{RESET} | " if color else f"{lineno:>{width}} | "
        bg: str = HIGHLIGHT_BG if color and lineno in highlighted else ""
        out.append(f"{gutter}{bg}{expanded}{RESET if bg else ''}")
    return "\n".join(out) + "\n"


def read_source(target: str) -> tuple[str, str]:
    """Return (display name, text) for a path or ``-`` (stdin)."""
    if target == "-":
        return "stdin", sys.stdin.read()
    path: Path = Path(target)
    return str(path), path.read_text(encoding="utf-8", errors="replace")


def emit(text: str, paging: str) -> None:
    """Print *text* directly or through a pager."""
    use_pager: bool = paging == "always" or (paging == "auto" and sys.stdout.isatty())
    if use_pager:
        pydoc.pager(text)
    else:
        print(text, end="")


def cmd_view(args: argparse.Namespace) -> int:
    """View one or more files (default: stdin)."""
    if args.list_themes or args.list_languages:
        try:
            from pygments.lexers import get_all_lexers
            from pygments.styles import get_all_styles

            if args.list_themes:
                print("\n".join(sorted(get_all_styles())))
            if args.list_languages:
                print("\n".join(sorted({alias for lexer in get_all_lexers() for alias in lexer[1]})))
            return 0
        except ImportError:
            print("Pygments not installed: pip install Pygments", file=sys.stderr)
            return 1
    color: bool = args.color == "always" or (
        args.color == "auto" and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    )
    chunks: list[str] = []
    status: int = 0
    for target in args.files or ["-"]:
        try:
            name, text = read_source(target)
        except OSError as exc:
            print(f"Error: cannot read {target}: {exc}", file=sys.stderr)
            status = 1
            continue
        chunks.append(render(name, text, args, color))
    emit("\n".join(chunks), args.paging)
    return status


def cmd_head(args: argparse.Namespace) -> int:
    """Print the first N lines (or bytes) of a file."""
    path: Path = args.file
    if args.bytes is not None:
        try:
            text: str = path.read_bytes()[: args.bytes].decode("utf-8", errors="replace")
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    try:
        lines: list[str] = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("".join(lines[: args.lines]), end="")
    return 0


def collect(root: Path, recursive: bool) -> list[Path]:
    """Collect files in *root* sorted case-insensitively."""
    paths = root.rglob("*") if recursive else root.iterdir()
    return sorted((p for p in paths if p.is_file()), key=lambda p: str(p).lower())


def browse_interactive(files: list[Path], preview_lines: int) -> None:
    """Curses file browser (quickview-style): j/k move, q quits."""
    import curses

    def draw(stdscr: curses.window, index: int) -> None:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        for row, path in enumerate(files[: height - preview_lines - 2]):
            marker: str = ">" if row == index else " "
            stdscr.addstr(row, 0, f"{marker} {path.name}"[: width - 1])
        if files:
            head: list[str] = files[index].read_text(errors="replace").splitlines()[:preview_lines]
            top: int = max(0, height - preview_lines - 1)
            stdscr.addstr(top, 0, "-" * min(width - 1, 60))
            for i, line in enumerate(head[: preview_lines - 1]):
                stdscr.addstr(top + 1 + i, 0, line[: width - 1])
        stdscr.refresh()

    def run(stdscr: curses.window) -> None:
        index: int = 0
        while True:
            draw(stdscr, index)
            key: int = stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key in (ord("j"), curses.KEY_DOWN):
                index = min(index + 1, len(files) - 1)
            if key in (ord("k"), curses.KEY_UP):
                index = max(index - 1, 0)

    curses.wrapper(run)


def cmd_browse(args: argparse.Namespace) -> int:
    """Page every file in a directory, or open the curses browser."""
    files: list[Path] = collect(args.root, args.recursive)
    if not files:
        print("No files found.")
        return 0
    if args.interactive:
        browse_interactive(files, args.preview_lines)
        return 0
    chunks: list[str] = []
    for path in files:
        try:
            content: str = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            content = f"[Could not read file: {exc}]"
        label: Path = path.relative_to(args.root) if args.root in path.parents else path
        chunks.append(f"\n{'=' * 40}\nFILE: {label}\n{'=' * 40}\n{content}")
    emit("\n".join(chunks), "always" if sys.stdout.isatty() else "never")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Terminal file viewer / pager.")
    sub = parser.add_subparsers(dest="command", required=True)
    vw = sub.add_parser("view", help="View files with numbers/highlighting")
    vw.add_argument("files", nargs="*", help="Files to view ('-' = stdin)")
    vw.add_argument("-l", "--language", default=None, help="Pygments lexer override")
    vw.add_argument("--theme", default="monokai", help="Pygments style (default: monokai)")
    vw.add_argument("-p", "--plain", action="store_true", help="No numbers/header/highlighting")
    vw.add_argument("-r", "--line-range", default=None, help="e.g. '30:40', '30:', ':40'")
    vw.add_argument("-H", "--highlight-line", action="append", default=[], type=int, metavar="N")
    vw.add_argument("--paging", choices=("auto", "always", "never"), default="auto")
    vw.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    vw.add_argument("--tabs", type=int, default=4, help="Tab width (default: 4)")
    vw.add_argument("--list-themes", action="store_true")
    vw.add_argument("--list-languages", action="store_true")
    vw.set_defaults(handler=cmd_view)
    hd = sub.add_parser("head", help="Print the start of a file")
    hd.add_argument("file", type=Path)
    hd.add_argument("-n", "--lines", type=int, default=10, help="Lines (default: 10)")
    hd.add_argument("-c", "--bytes", type=int, default=None, help="Bytes instead of lines")
    hd.set_defaults(handler=cmd_head)
    br = sub.add_parser("browse", help="Page/browse all files in a directory")
    br.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    br.add_argument("-r", "--recursive", action="store_true")
    br.add_argument("-i", "--interactive", action="store_true", help="Curses browser (j/k/q)")
    br.add_argument("--preview-lines", type=int, default=PREVIEW_LINES, help="Curses preview lines (default: 20)")
    br.set_defaults(handler=cmd_browse)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
