#!/data/data/com.termux/files/usr/bin/env python
"""Comment or un-comment a line range with the right token per language.
The ``comment`` subcommand picks the token from the extension (``#``, ``//``, ``--``, ``;`` ...) or ``--token``, can undo with ``--undo`` and offers ``--dry-run``, ``--stdout`` and ``--backup``.
Usage: cout2 comment FILE START [END] [--undo] [--token TOKEN] Positional arguments: file start First line (1-based) end Last line (default: EOF) Subcommands: comment Comment (or --undo) lines START..END Options: --token Comment token override, e.g.
--token // --undo Uncomment instead of commenting -n, --dry-run Only report how many lines change --stdout Print result instead of writing the file --backup Keep a .bak copy before writing Examples: $ cout2 comment main.rs 5 20 Command-line script: run ``python cout2.py`` or the ``cout2`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys

DEFAULT_COMMENT: str = "#"
COMMENT_TOKENS: dict[str, tuple[str, str]] = {
    ".py": ("#", ""),
    ".sh": ("#", ""),
    ".bash": ("#", ""),
    ".yaml": ("#", ""),
    ".yml": ("#", ""),
    ".toml": ("#", ""),
    ".rb": ("#", ""),
    ".pl": ("#", ""),
    ".js": ("//", ""),
    ".ts": ("//", ""),
    ".cpp": ("//", ""),
    ".cc": ("//", ""),
    ".c": ("//", ""),
    ".h": ("//", ""),
    ".cs": ("//", ""),
    ".java": ("//", ""),
    ".go": ("//", ""),
    ".rs": ("//", ""),
    ".lua": ("--", ""),
    ".sql": ("--", ""),
    ".vim": ('"', ""),
    ".html": ("<!--", "-->"),
    ".css": ("/*", "*/"),
}


def tokens_for(path: Path, override: str | None) -> tuple[str, str]:
    if override:
        return override, ""
    found: tuple[str, str] | None = COMMENT_TOKENS.get(path.suffix.lower())
    if found is None:
        print(f"warning: unknown extension {path.suffix!r}, using {DEFAULT_COMMENT!r}", file=sys.stderr)
        return DEFAULT_COMMENT, ""
    return found


def comment_line(line: str, open_token: str, close_token: str) -> str:
    stripped: str = line.lstrip()
    if not stripped or stripped.startswith(open_token):
        return line
    indent: str = line[: len(line) - len(stripped)]
    ending: str = "\n" if stripped.endswith("\n") else ""
    body: str = stripped.rstrip("\n")
    closer: str = f" {close_token}" if close_token else ""
    return f"{indent}{open_token} {body}{closer}{ending}"


def uncomment_line(line: str, open_token: str, close_token: str) -> str:
    stripped: str = line.lstrip()
    if not stripped.startswith(open_token):
        return line
    indent: str = line[: len(line) - len(stripped)]
    body: str = stripped[len(open_token) :].removeprefix(" ").rstrip("\n")
    ending: str = "\n" if stripped.endswith("\n") else ""
    if close_token and body.rstrip().endswith(close_token):
        body = body.rstrip()[: -len(close_token)].rstrip()
    return f"{indent}{body}{ending}"


def process_lines(lines: list[str], start: int, end: int | None, open_token: str, close_token: str, undo: bool) -> int:
    last: int = len(lines) if end is None else min(end, len(lines))
    changed: int = 0
    for index in range(start - 1, last):
        new: str = (uncomment_line if undo else comment_line)(lines[index], open_token, close_token)
        if new != lines[index]:
            lines[index] = new
            changed += 1
    return changed


def cmd_comment(args: argparse.Namespace) -> int:
    path: Path = args.file
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    if args.start < 1 or (args.end is not None and args.end < args.start):
        print("Error: lines are 1-based and end must be >= start.", file=sys.stderr)
        return 2
    lines: list[str] = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    if args.start > len(lines):
        print(f"Error: start line {args.start} exceeds file length ({len(lines)} lines).", file=sys.stderr)
        return 1
    open_token, close_token = tokens_for(path, args.token)
    changed: int = process_lines(lines, args.start, args.end, open_token, close_token, args.undo)
    verb: str = "Uncommented" if args.undo else "Commented"
    if args.stdout:
        print("".join(lines), end="")
        return 0
    if args.dry_run:
        print(f"Would change {changed} line(s) in {path} using {open_token!r}.")
        return 0
    if args.backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text("".join(lines), encoding="utf-8")
    print(f"{verb} {changed} line(s) in {path} using {open_token!r}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Comment/uncomment a line range in a file.")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("comment", help="Comment (or --undo) lines START..END")
    cmd.add_argument("file", type=Path)
    cmd.add_argument("start", type=int, help="First line (1-based)")
    cmd.add_argument("end", nargs="?", type=int, default=None, help="Last line (default: EOF)")
    cmd.add_argument("--token", default=None, help="Comment token override, e.g. --token //")
    cmd.add_argument("--undo", action="store_true", help="Uncomment instead of commenting")
    cmd.add_argument("-n", "--dry-run", action="store_true", help="Only report how many lines change")
    cmd.add_argument("--stdout", action="store_true", help="Print result instead of writing the file")
    cmd.add_argument("--backup", action="store_true", help="Keep a .bak copy before writing")
    cmd.set_defaults(handler=cmd_comment)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
