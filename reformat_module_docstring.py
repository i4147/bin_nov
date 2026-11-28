#!/data/data/com.termux/files/usr/bin/env python
"""Reformat module docstrings into a canonical layout.

libcst-based codemod that reflows each module docstring to the target width (textwrap) and rewrites the file; built on the parallel_runner two-pass harness.
"""

from __future__ import annotations
import difflib
from functools import partial
import os
import re
import sys
import textwrap
from typing import TYPE_CHECKING

from dh import SKIP_DIRS, atomic_write
import libcst as cst
from parallel_runner import (
    FileFilter,
    RunnerConfig,
    TaskOutcome,
    build_base_parser,
    normalize_extensions,
    run_two_pass,
)


if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Sequence
    from pathlib import Path


# Section headers recognised inside a docstring (matched as "Header:").
SECTION_HEADERS: tuple[str, ...] = (
    "Subcommands",
    "Usage",
    "Positional arguments",
    "Optional arguments",
    "Options",
    "Arguments",
    "Examples",
    "Example",
    "Command-line script",
    "Notes",
    "Note",
    "Returns",
    "Raises",
    "See also",
    "Attributes",
    "Parameters",
    "Environment variables",
)

# File suffixes treated as text files when --text-mode is enabled.
# Extensionless files are always eligible in text mode; dot-prefixed files are skipped.
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".text",
        ".rst",
        ".md",
        ".markdown",
        ".doc",
    }
)

_HEADER_RE: re.Pattern[str] = re.compile(
    r"(?<![\w-])("
    + "|".join(re.escape(h) for h in sorted(SECTION_HEADERS, key=len, reverse=True))
    + r"):(?=\s|$)"
)

# Splits a flattened section body before every "``name``" item that starts a new entry.
_ITEM_START_RE: re.Pattern[str] = re.compile(r"(?=``[^`]+``(?:\s*[,/]\s*``[^`]+``)*\s)")
# Captures the item name (one or more ``...`` names joined by / or ,) and its description.
_ITEM_RE: re.Pattern[str] = re.compile(
    r"^((?:``[^`]+``)(?:\s*[,/]\s*``[^`]+``)*)\s+(.*)$",
    re.DOTALL,
)
_TRAILING_SEP_RE: re.Pattern[str] = re.compile(r"[,;]\s*$")
# Breaks text after sentence-ending punctuation when the next sentence starts with a
# capital letter, digit, backtick, quote, bracket, or parenthesis.
_SENTENCE_BOUNDARY_RE: re.Pattern[str] = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(\[])")

# Maximum length of any line in the rendered docstring.
MAX_DOCSTRING_LINE_LENGTH: int = 70
DEFAULT_MAX_FILE_BYTES: int = 10 * 1024 * 1024  # 10 MiB safety guard


def _collapse(text: str) -> str:
    """Collapse all runs of whitespace (including newlines) into single spaces."""
    return " ".join(text.split())


def _split_sentences(text: str) -> list[str]:
    """Split collapsed text into sentences, one per list element."""
    return [
        sentence for sentence in _SENTENCE_BOUNDARY_RE.split(text.strip()) if sentence
    ]


def _indent_sentences(text: str, indent: str) -> str:
    """Place each sentence on its own block of lines, wrapped to the line limit.

    Every line, including wrapped continuation lines, starts with indent and
    stays within MAX_DOCSTRING_LINE_LENGTH characters where possible.
    """
    blocks: list[str] = [
        textwrap.fill(
            sentence,
            width=MAX_DOCSTRING_LINE_LENGTH,
            initial_indent=indent,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        for sentence in _split_sentences(text)
    ]
    return "\n".join(blocks)


def _split_sections(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a raw docstring into its introduction and (header, body) pairs."""
    parts: list[str] = _HEADER_RE.split(raw)
    intro: str = parts[0]
    sections: list[tuple[str, str]] = [
        (parts[i], parts[i + 1]) for i in range(1, len(parts), 2)
    ]
    return intro, sections


def _intro_paragraphs(intro: str) -> list[str]:
    """Return the introduction as paragraphs, each sentence on its own line."""
    return [
        _indent_sentences(_collapse(paragraph), "")
        for paragraph in re.split(r"\n\s*\n", intro.strip())
        if paragraph.strip()
    ]


def _render_section(header: str, body: str) -> str:
    """Render one section in the target layout, with one sentence per line."""
    flat: str = _collapse(body)
    if not flat:
        return f"{header}:"

    if flat.startswith("``"):
        items: list[tuple[str, str]] = []
        for piece in _ITEM_START_RE.split(flat):
            cleaned: str = _TRAILING_SEP_RE.sub("", piece.strip()).strip()
            if not cleaned:
                continue
            match: re.Match[str] | None = _ITEM_RE.match(cleaned)
            if match:
                items.append((match.group(1), match.group(2)))
            else:
                items.append((cleaned, ""))

        item_blocks: list[str] = []
        for name, desc in items:
            display_name: str = re.sub(r"\s*,\s*", "/", name.replace("``", ""))
            block: str = f"    {display_name}:"
            if desc:
                block += "\n" + _indent_sentences(desc, " " * 8)
            item_blocks.append(block)
        return f"{header}:\n\n" + "\n\n".join(item_blocks)

    return f"{header}:\n" + _indent_sentences(flat, " " * 4)


def format_docstring(raw: str) -> str:
    """Return the docstring/text content in the target layout."""
    intro, sections = _split_sections(raw)
    blocks: list[str] = _intro_paragraphs(intro)
    blocks.extend(_render_section(header, body) for header, body in sections)
    body_text: str = "\n\n".join(blocks).replace("``", "")
    return "\n" + body_text + "\n\n"


def _render_literal(text: str) -> str:
    escaped: str = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')  # editor """
    return f'"""{escaped}"""'


def _is_already_formatted(raw: str) -> bool:
    """Heuristic: docstrings in the target layout start and end with blank lines."""
    return raw.startswith("\n") and raw.endswith("\n\n")


class ModuleDocstringTransformer(cst.CSTTransformer):
    """Rewrite a Python module's leading docstring using LibCST."""

    def leave_Module(
        self, original_node: cst.Module, updated_node: cst.Module
    ) -> cst.Module:
        if not updated_node.body:
            return updated_node

        first: cst.BaseStatement = updated_node.body[0]
        if not isinstance(first, cst.SimpleStatementLine) or not first.body:
            return updated_node

        expr: cst.BaseSmallStatement = first.body[0]
        if not isinstance(expr, cst.Expr) or not isinstance(
            expr.value, cst.SimpleString
        ):
            return updated_node

        raw: object = expr.value.evaluated_value
        if not isinstance(raw, str) or _is_already_formatted(raw):
            return updated_node

        new_value: str = format_docstring(raw)
        if new_value == raw:
            return updated_node

        new_string: cst.SimpleString = cst.SimpleString(_render_literal(new_value))
        new_first: cst.SimpleStatementLine = first.with_changes(
            body=[expr.with_changes(value=new_string), *first.body[1:]]
        )
        return updated_node.with_changes(body=[new_first, *updated_node.body[1:]])


def _format_text_content(source: str) -> str:
    """Format standalone extracted text content."""
    if not source.strip():
        return source
    if _is_already_formatted(source) and source.count("\n\n") >= 1:
        candidate: str = format_docstring(source)
        return candidate if candidate != source else source
    return format_docstring(source)


def build_file_filter(args: argparse.Namespace) -> FileFilter:
    """Construct a FileFilter from parsed CLI arguments."""
    extra_exts: frozenset[str] = normalize_extensions(args.ext)
    extra_dirs: frozenset[str] = frozenset(
        d.strip() for d in args.exclude_dir if d.strip()
    )
    excluded_dirs: frozenset[str] = SKIP_DIRS | extra_dirs

    if args.text_mode:
        return FileFilter(
            extensions=TEXT_EXTENSIONS | extra_exts,
            allow_extensionless=True,
            match_any_when_empty=False,
            excluded_dirs=excluded_dirs,
            skip_hidden_files=True,
            skip_symlinks=True,
        )

    return FileFilter(
        extensions=frozenset({".py"}) | extra_exts,
        allow_extensionless=False,
        match_any_when_empty=False,
        excluded_dirs=excluded_dirs,
        skip_hidden_files=True,
        skip_symlinks=True,
    )


def process_file(
    path: Path,
    root: Path,
    text_mode: bool,
    dry_run: bool,
    check: bool,
    show_diff: bool,
    max_file_bytes: int,
) -> TaskOutcome[None]:
    """Reformat one Python file or extracted text file inside a worker process."""
    rel: str = str(path.relative_to(root.resolve()))

    try:
        file_size: int = path.stat().st_size
        if max_file_bytes > 0 and file_size > max_file_bytes:
            return TaskOutcome(
                rel_path=rel,
                error=f"skipped: size {file_size} exceeds limit {max_file_bytes} bytes",
            )
        original: bytes = path.read_bytes()
        if b"\x00" in original:
            return TaskOutcome(
                rel_path=rel,
                error="skipped: binary file (NUL byte detected)",
            )
        source: str = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return TaskOutcome(rel_path=rel, error=f"read error: {exc}")

    new_source: str
    if text_mode:
        new_source = _format_text_content(source)
    else:
        try:
            module: cst.Module = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            return TaskOutcome(rel_path=rel, error=f"syntax error: {exc}")
        new_source = module.visit(ModuleDocstringTransformer()).code

    if new_source == source:
        return TaskOutcome(rel_path=rel, changed=False, bytes_delta=0)

    new_bytes: bytes = new_source.encode("utf-8")
    bytes_delta: int = len(new_bytes) - len(original)

    diff: str = ""
    preview_only: bool = dry_run or check
    if preview_only and show_diff:
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                new_source.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )

    if not preview_only:
        try:
            atomic_write(path, new_bytes)
        except OSError as exc:
            return TaskOutcome(rel_path=rel, error=f"write error: {exc}")

    return TaskOutcome(
        rel_path=rel,
        changed=True,
        bytes_delta=bytes_delta,
        diff=diff,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Extend the shared runner parser with docstring/text-mode flags."""
    parser: argparse.ArgumentParser = build_base_parser(
        "Reformat module docstrings or extracted docstring text files recursively."
    )
    parser.add_argument(
        "-t",
        "--text-mode",
        action="store_true",
        help="process extracted text files (including extensionless files and .txt/.rst/.md) instead of .py files",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        metavar="BYTES",
        help=f"skip files larger than BYTES (default: {DEFAULT_MAX_FILE_BYTES}; 0 for unlimited)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and run the two-pass docstring/text formatter."""
    args: argparse.Namespace = _build_parser().parse_args(argv)

    config: RunnerConfig = RunnerConfig(
        root=args.root,
        workers=max(1, int(args.workers)),
        dry_run=bool(args.dry_run),
        check=bool(args.check),
        show_diff=not bool(args.no_diff),
        unchanged=bool(args.unchanged),
        item_label="text files" if args.text_mode else "Python files",
    )
    file_filter: FileFilter = build_file_filter(args)
    handler: Callable[[Path], TaskOutcome[None]] = partial(
        process_file,
        root=config.root,
        text_mode=bool(args.text_mode),
        dry_run=config.dry_run,
        check=config.check,
        show_diff=config.show_diff,
        max_file_bytes=max(0, int(args.max_size)),
    )

    exit_code, _ = run_two_pass(
        config=config,
        file_filter=file_filter,
        handler=handler,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
