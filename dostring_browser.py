#!/data/data/com.termux/files/usr/bin/env python
"""Browse the module docstrings of a directory in a paged Markdown view.
Extracts the docstring of each ``.py`` file with ast, renders the documents with rich and pages through them with the space/arrow keys, ``n``/``p`` for the next or previous file and ``q`` to quit.
Usage: dostring_browser [directory] Positional arguments: directory Directory to scan for .py files (default: current directory) Examples: $ dostring_browser ~/bin Command-line script: run ``python dostring_browser.py`` or the ``dostring_browser`` command.
The module is importable as well."""

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown


try:
    from readchar import (
        key as RKEY,
        readkey,
    )

    HAVE_READCHAR = True
except Exception:
    HAVE_READCHAR = False


def _key_set(*names: str) -> set[str]:
    keys: set[str] = set()
    for name in names:
        value = getattr(RKEY, name, None) if HAVE_READCHAR else None
        if isinstance(value, str):
            keys.add(value)
    return keys


NEXT_KEYS = {" ", "j", "f", "\n", "\r"} | _key_set("SPACE", "ENTER", "PAGE_DOWN", "RIGHT", "DOWN", "TAB")
PREV_KEYS = {"b", "k"} | _key_set("PAGE_UP", "LEFT", "UP", "BACKSPACE")
NEXT_FILE_KEYS: set[str] = {"n", "]", ">"}
PREV_FILE_KEYS: set[str] = {"p", "[", "<"}
FIRST_KEYS: set[str] = {"g"}
LAST_KEYS: set[str] = {"G"}
QUIT_KEYS: set[str] = {"q", "Q", "\x03", "\x04", "\x1b"}


def classify(key: str) -> str | None:
    if key == "":
        return "next_page"
    if key in NEXT_KEYS:
        return "next_page"
    if key in PREV_KEYS:
        return "prev_page"
    if key in NEXT_FILE_KEYS:
        return "next_file"
    if key in PREV_FILE_KEYS:
        return "prev_file"
    if key in FIRST_KEYS:
        return "first"
    if key in LAST_KEYS:
        return "last"
    if key in QUIT_KEYS:
        return "quit"
    return None


def read_action() -> str:
    if HAVE_READCHAR:
        try:
            return classify(readkey()) or ""
        except (EOFError, KeyboardInterrupt):
            return "quit"
    try:
        line = input("  [enter]=next  b=back  n/p=file  q=quit > ").strip()
    except (EOFError, KeyboardInterrupt):
        return "quit"
    return classify(line[:1] if line else "") or ""


@dataclass
class Document:
    name: str
    docstring: str | None = None
    error: str | None = None


def extract_docstring(path: Path) -> Document:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Document(path.name, error="not a valid UTF-8 text file")
    except OSError as error:
        return Document(path.name, error=str(error))
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return Document(
            path.name,
            error=f"syntax error on line {error.lineno}: {error.msg}",
        )
    except ValueError as error:
        return Document(path.name, error=str(error))
    return Document(path.name, docstring=ast.get_docstring(tree))


def render_document(console: Console, document: Document, width: int) -> list[str]:
    lines = [f"  {document.name}  ", ""]
    if document.error is not None:
        lines.append(f"  (could not read module docstring: {document.error})")
        return lines
    if document.docstring is None or not document.docstring.strip():
        lines.append("  (this module has no docstring)")
        return lines
    previous_width = console.width
    console.width = width
    try:
        with console.capture() as capture:
            console.print(Markdown(document.docstring))
    finally:
        console.width = previous_width
    lines.extend(capture.get().splitlines())
    return lines


def paginate(lines: list[str], height: int) -> list[list[str]]:
    height = max(height, 1)
    pages = [lines[i : i + height] for i in range(0, len(lines), height)]
    return pages or [[]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pydocview",
        description=(
            "Browse the module docstrings of the .py files in a directory, "
            "rendered as Markdown and paged in the terminal."
        ),
        epilog=(
            "Controls: space/right/down = next page (or next file), "
            "left/up/b = previous page (or previous file), n = next file, "
            "p = previous file, g/G = first/last file, q = quit. "
            "If single-key input is unavailable the viewer falls back to "
            "line-based paging: press Enter to continue, or type a command."
        ),
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan for .py files (default: current directory)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    console = Console()
    err_console = Console(stderr=True)
    directory = Path(args.directory)
    try:
        if not directory.is_dir():
            err_console.print(f"[red]Error:[/red] not a directory: {directory}")
            return 1
        paths = sorted(p for p in directory.glob("*.py") if p.is_file())
    except OSError as error:
        err_console.print(f"[red]Error:[/red] {error}")
        return 1
    if not paths:
        console.print(f"[yellow]No .py files found in[/yellow] {directory.resolve()}")
        return 0
    documents = [extract_docstring(path) for path in paths]
    rendered: dict[tuple[int, int], list[str]] = {}

    def lines_for(index: int, width: int) -> list[str]:
        key = (index, width)
        cached = rendered.get(key)
        if cached is None:
            cached = render_document(console, documents[index], width)
            rendered[key] = cached
        return cached

    doc_index = 0
    page_index = 0
    message = ""
    while True:
        width = max(console.size.width, 20)
        height = max(console.size.height - 2, 1)
        body = lines_for(doc_index, width)
        pages = paginate(body, height)
        page_index = max(0, min(page_index, len(pages) - 1))
        status = (
            f" {documents[doc_index].name}"
            f"  ·  file {doc_index + 1}/{len(documents)}"
            f"  ·  page {page_index + 1}/{len(pages)}"
            f"  ·  q=quit "
        )[:width]
        console.clear()
        frame = [status, *pages[page_index]]
        if message:
            frame.append(message)
            message = ""
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()
        action = read_action()
        if action == "quit":
            break
        if action == "next_page":
            if page_index < len(pages) - 1:
                page_index += 1
            elif doc_index < len(documents) - 1:
                doc_index += 1
                page_index = 0
            else:
                message = "(end of last file)"
        elif action == "prev_page":
            if page_index > 0:
                page_index -= 1
            elif doc_index > 0:
                doc_index -= 1
                page_index = len(paginate(lines_for(doc_index, width), height)) - 1
            else:
                message = "(start of first file)"
        elif action == "next_file":
            if doc_index < len(documents) - 1:
                doc_index += 1
                page_index = 0
            else:
                message = "(last file)"
        elif action == "prev_file":
            if doc_index > 0:
                doc_index -= 1
                page_index = 0
            else:
                message = "(first file)"
        elif action == "first":
            doc_index = 0
            page_index = 0
        elif action == "last":
            doc_index = len(documents) - 1
            page_index = 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
