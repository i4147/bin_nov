#!/data/data/com.termux/files/usr/bin/env python
"""Render Markdown or text (file, URL, stdin) in the terminal with rich and a pager."""

from pathlib import Path
import sys
from typing import Annotated
from urllib.request import urlopen

from loguru import logger
from pygments.styles import get_all_styles
from rich.console import Console
from rich.markdown import Markdown
from rich.pager import SystemPager
from rich.syntax import Syntax
import typer


def _read_source(source: str) -> str:
    if source in ("", "-"):
        return sys.stdin.read()
    if source.startswith(("http://", "https://")):
        with urlopen(source) as resp:
            return resp.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def _parse_blocks(content: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    lang = ""
    buf: list[str] = []
    inside = False
    for line in content.splitlines():
        if line.startswith("```"):
            if inside:
                blocks.append((lang, "\n".join(buf)))
                buf = []
                inside = False
            else:
                inside = True
                lang = line[3:].strip()
        elif inside:
            buf.append(line)
    if inside:
        blocks.append((lang, "\n".join(buf)))
    return blocks


def _render(
    console: Console,
    content: str,
    syntax_theme: str,
    line_numbers: bool,
    highlight: bool,
    default_language: str,
    code_only: bool,
    soft_wrap: bool,
    strip: bool,
) -> None:
    if code_only:
        for lang, code in _parse_blocks(content):
            if highlight:
                console.print(
                    Syntax(
                        code,
                        lang or default_language,
                        theme=syntax_theme,
                        line_numbers=line_numbers,
                        word_wrap=soft_wrap,
                    )
                )
            else:
                console.print(code)
        return

    lines = content.splitlines()
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    text_buf: list[str] = []

    def flush_text() -> None:
        if not text_buf:
            return
        chunk = "\n".join(text_buf).strip("\n")
        if chunk:
            if strip:
                console.print(chunk, markup=False, highlight=False)
            else:
                console.print(Markdown(chunk))
        text_buf.clear()

    for line in lines:
        if line.startswith("```"):
            if in_code:
                flush_text()
                body = "\n".join(code_buf)
                if highlight:
                    console.print(
                        Syntax(
                            body,
                            code_lang or default_language,
                            theme=syntax_theme,
                            line_numbers=line_numbers,
                            word_wrap=soft_wrap,
                        )
                    )
                else:
                    console.print(body)
                code_buf = []
                in_code = False
            else:
                flush_text()
                in_code = True
                code_lang = line[3:].strip() or default_language
        elif in_code:
            code_buf.append(line)
        else:
            text_buf.append(line)

    if in_code:
        body = "\n".join(code_buf)
        if highlight:
            console.print(
                Syntax(
                    body,
                    code_lang or default_language,
                    theme=syntax_theme,
                    line_numbers=line_numbers,
                    word_wrap=soft_wrap,
                )
            )
        else:
            console.print(body)
    flush_text()


def main(
    source: Annotated[str | None, "Markdown file path, URL, or '-' for stdin"] = None,
    pager: Annotated[bool, "Page output when it exceeds terminal height"] = False,
    pager_cmd: Annotated[str | None, "Custom pager command (e.g. 'less -R')"] = None,
    syntax_theme: Annotated[str, "Pygments theme for code blocks"] = "monokai",
    line_numbers: Annotated[bool, "Render line numbers in code blocks"] = True,
    highlight: Annotated[bool, "Enable syntax highlighting for code blocks"] = True,
    code_only: Annotated[bool, "Only print fenced code blocks"] = False,
    default_language: Annotated[str, "Fallback language for un-tagged code fences"] = "python",
    width: Annotated[int | None, "Console width override"] = None,
    color: Annotated[bool, "Enable color output"] = True,
    hyperlinks: Annotated[bool, "Enable terminal hyperlinks"] = True,
    soft_wrap: Annotated[bool, "Word-wrap long lines inside code blocks"] = False,
    tab_size: Annotated[int, "Tab expansion width"] = 4,
    strip: Annotated[bool, "Render plain text (no markdown styling)"] = False,
    output: Annotated[Path | None, "Write rendered output to file"] = None,
    list_syntax_themes: Annotated[bool, "List available Pygments themes and exit"] = False,
    verbose: Annotated[bool, "Verbose logging to stderr"] = False,
) -> None:
    if verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")

    if list_syntax_themes:
        for name in sorted(get_all_styles()):
            typer.echo(name)
        return

    try:
        content = _read_source(source or "-")
    except Exception:
        logger.opt(exception=True).error("Failed to read input")
        raise typer.Exit(1)

    out_handle = None
    try:
        if output is not None:
            out_handle = output.open("w", encoding="utf-8", newline="")

        console = Console(
            file=out_handle if out_handle is not None else None,
            width=width,
            no_color=not color,
            color_system=None if not color else "auto",
            soft_wrap=soft_wrap,
            tab_size=tab_size,
            hyperlinks=hyperlinks,
            highlight=False,
            pager=SystemPager() if pager else None,
        )

        render_kwargs = dict(
            syntax_theme=syntax_theme,
            line_numbers=line_numbers,
            highlight=highlight,
            default_language=default_language,
            code_only=code_only,
            soft_wrap=soft_wrap,
            strip=strip,
        )

        if pager and out_handle is None and pager_cmd is None:
            with console.pager(styles=True, links=hyperlinks):
                _render(console, content, **render_kwargs)
        elif pager_cmd and out_handle is None:
            import io
            import shlex
            import subprocess

            buf = io.StringIO()
            capture = Console(
                file=buf,
                width=width,
                no_color=not color,
                color_system=None if not color else "auto",
                soft_wrap=soft_wrap,
                tab_size=tab_size,
                hyperlinks=hyperlinks,
                highlight=False,
            )
            _render(capture, content, **render_kwargs)
            proc = subprocess.Popen(shlex.split(pager_cmd), stdin=subprocess.PIPE, text=True)
            proc.communicate(buf.getvalue())
        else:
            _render(console, content, **render_kwargs)
    except Exception:
        logger.opt(exception=True).error("Rendering failed")
        raise typer.Exit(1)
    finally:
        if out_handle is not None:
            out_handle.close()


if __name__ == "__main__":
    typer.run(main)
