#!/data/data/com.termux/files/usr/bin/env python
"""Line-oriented file editing operations.
Subcommands: ``prefix``, ``append``, ``unescape-nl``/``escape-nl``, ``wrap``, ``get-range``, ``join``, ``move-range``, ``sort-length``, ``strip-ws``, ``tabs`` and ``space-out`` transform the lines of one file or of a whole tree, in place, to ``-o`` or to stdout.
Usage: line_edit_tool {prefix,append,unescape-nl,escape-nl,wrap,get-range,join,move-range,sort-length,strip-ws,tabs,space-out} [options] Positional arguments: file text paths ...
start end src dest Subcommands: prefix Prefix non-empty lines append Append text to files (default: *.py in .) unescape-nl Literal \n -> newlines (tree) escape-nl Newlines -> literal \n wrap Wrap text to a width get-range Extract lines START..END join Join non-empty lines into one line move-range Move lines START..END to another file sort-length Sort lines by length strip-ws Strip whitespace from each line tabs Tabs -> spaces (tree) space-out Blank lines between every line Options: -o, --output Write here instead of in place --stdout Print the result --no-space No space after the prefix --text Text to append --text-file Append this file's contents --glob Glob when paths are dirs (default: *.py) -w, --width Default: terminal width -i, --inplace --auto-name Write to START[_END].ext (original) --sep Separator (original default: none) ...
plus 5 more; run ``line_edit_tool --help`` for the full list.
Examples: $ line_edit_tool strip-ws -o clean.txt notes.txt $ line_edit_tool wrap -w 80 notes.txt Command-line script: run ``python line_edit_tool.py`` or the ``line_edit_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys
import textwrap


DEFAULT_APPEND_TEXT: str = "\n#migrate from logging with print and standard logging module to loguru\n"
DEFAULT_TAB_WIDTH: int = 4
DEFAULT_BLANKS: int = 4


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def emit(text: str, source: Path, output: Path | None, to_stdout: bool) -> None:
    if to_stdout:
        print(text, end="" if text.endswith("\n") or not text else "\n")
    elif output is not None:
        output.write_text(text, encoding="utf-8")
        print(f"Wrote {output}")
    else:
        source.write_text(text, encoding="utf-8")
        print(f"{source} updated")


def collect_text_files(inputs: Sequence[Path], glob: str = "*") -> list[Path]:
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob(glob) if root.is_dir() else iter((root,))
        for path in candidates:
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                if b"\x00" in path.read_bytes()[:8192]:
                    continue
            except OSError:
                continue
            found.add(path)
    return sorted(found)


def transform_files(inputs: Sequence[Path], fn: object, label: str, glob: str = "*") -> int:
    changed: int = 0
    for path in collect_text_files(inputs, glob):
        original: str = path.read_text(encoding="utf-8", errors="replace")
        updated: str = fn(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
            print(f"{path.name} ({label})")
    print(f"{changed} file(s) changed.")
    return 0


def cmd_prefix(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.file)
    joiner: str = "" if args.no_space else " "
    out: str = "".join(f"{args.text}{joiner}{line}" if line.strip() else line for line in lines)
    emit(out, args.file, args.output, args.stdout)
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    addition: str = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
    files: list[Path] = (
        collect_text_files(args.paths, glob=args.glob) if args.paths else sorted(Path.cwd().glob("*.py"))
    )
    for path in files:
        try:
            path.write_text(path.read_text(encoding="utf-8") + addition, encoding="utf-8")
            print(f"Updated: {path.name}")
        except OSError as exc:
            print(f"Error: {path.name} - {exc}", file=sys.stderr)
    return 0


def cmd_unescape_nl(args: argparse.Namespace) -> int:
    return transform_files(args.paths, lambda t: t.replace("\\n", "\n"), "unescaped")


def cmd_escape_nl(args: argparse.Namespace) -> int:
    text: str = args.file.read_text(encoding="utf-8")
    emit(text.replace("\n", "\\n"), args.file, args.output, args.stdout)
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    width: int = max(args.width or shutil.get_terminal_size().columns, 20)
    content: str = args.file.read_text(encoding="utf-8")
    wrapped: list[str] = []
    for paragraph in content.split("\n\n"):
        block_lines: list[str] = []
        for line in paragraph.splitlines():
            if line.strip():
                block_lines.append(
                    textwrap.fill(
                        line, width=width, break_long_words=False, break_on_hyphens=False, replace_whitespace=False
                    )
                )
            else:
                block_lines.append("")
        wrapped.append("\n".join(block_lines))
    result: str = "\n\n".join(wrapped) + "\n"
    if args.stdout:
        print(result, end="")
    elif args.inplace:
        args.file.write_text(result, encoding="utf-8")
        print(f"Wrapped ({width} cols) in place: {args.file}")
    else:
        dest: Path = args.file.with_name(f"{args.file.stem}_wrapped{args.file.suffix}")
        dest.write_text(result, encoding="utf-8")
        print(f"Wrote wrapped text ({width} cols) to {dest}")
    return 0


def cmd_get_range(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.file)
    end: int = len(lines) if args.end is None else args.end
    selected: str = "".join(lines[args.start - 1 : end])
    if args.auto_name:
        name: str = (
            f"{args.start}{args.file.suffix}" if args.end is None else f"{args.start}_{args.end}{args.file.suffix}"
        )
        Path(name).write_text(selected, encoding="utf-8")
        print(f"Saved to {name}")
    elif args.output is not None:
        args.output.write_text(selected, encoding="utf-8")
        print(f"Saved to {args.output}")
    else:
        print(selected, end="")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.file)
    joined: str = args.sep.join(line.rstrip("\n") for line in lines if line.strip()) + "\n"
    emit(joined, args.file, args.output, args.stdout)
    return 0


def cmd_move_range(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.src)
    moved: list[str] = lines[args.start - 1 : args.end]
    kept: list[str] = lines[: args.start - 1] + lines[args.end :]
    if args.copy:
        kept = lines
    else:
        args.src.write_text("".join(kept), encoding="utf-8")
    mode: str = "a" if args.append else "w"
    with args.dest.open(mode, encoding="utf-8") as sink:
        sink.writelines(moved)
    verb: str = "Copied" if args.copy else "Moved"
    print(f"{verb} {len(moved)} line(s) from {args.src} to {args.dest}")
    return 0


def cmd_sort_length(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.file)
    ordered: list[str] = sorted(lines, key=lambda ln: len(ln.rstrip("\n")), reverse=args.reverse)
    emit("".join(ordered), args.file, args.output, args.stdout)
    return 0


def cmd_strip_ws(args: argparse.Namespace) -> int:
    lines: list[str] = read_lines(args.file)
    cleaned: str = "".join(line.strip() + "\n" for line in lines)
    emit(cleaned, args.file, args.output, args.stdout)
    return 0


def cmd_tabs(args: argparse.Namespace) -> int:
    spaces: str = " " * args.width
    return transform_files(args.paths, lambda t: t.replace("\t", spaces), "tabs expanded")


def cmd_space_out(args: argparse.Namespace) -> int:
    lines: list[str] = args.file.read_text(encoding="utf-8").splitlines()
    separator: str = "\n" * (args.blanks + 1)
    emit(separator.join(lines) + "\n", args.file, args.output, args.stdout)
    return 0


def add_io(child: argparse.ArgumentParser) -> None:
    child.add_argument("-o", "--output", type=Path, default=None, help="Write here instead of in place")
    child.add_argument("--stdout", action="store_true", help="Print the result")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Line-oriented file editing operations.")
    sub = parser.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("prefix", help="Prefix non-empty lines")
    pr.add_argument("file", type=Path)
    pr.add_argument("text")
    pr.add_argument("--no-space", action="store_true", help="No space after the prefix")
    add_io(pr)
    pr.set_defaults(handler=cmd_prefix)

    ap = sub.add_parser("append", help="Append text to files (default: *.py in .)")
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--text", default=DEFAULT_APPEND_TEXT, help="Text to append")
    ap.add_argument("--text-file", type=Path, default=None, help="Append this file's contents")
    ap.add_argument("--glob", default="*.py", help="Glob when paths are dirs (default: *.py)")
    ap.set_defaults(handler=cmd_append)

    un = sub.add_parser("unescape-nl", help=r"Literal \n -> newlines (tree)")
    un.add_argument("paths", nargs="*", type=Path)
    un.set_defaults(handler=cmd_unescape_nl)

    es = sub.add_parser("escape-nl", help=r"Newlines -> literal \n")
    es.add_argument("file", type=Path)
    add_io(es)
    es.set_defaults(handler=cmd_escape_nl)

    wr = sub.add_parser("wrap", help="Wrap text to a width")
    wr.add_argument("file", type=Path)
    wr.add_argument("-w", "--width", type=int, default=None, help="Default: terminal width")
    wr.add_argument("-i", "--inplace", action="store_true")
    wr.add_argument("--stdout", action="store_true")
    wr.set_defaults(handler=cmd_wrap)

    gr = sub.add_parser("get-range", help="Extract lines START..END")
    gr.add_argument("file", type=Path)
    gr.add_argument("start", type=int)
    gr.add_argument("end", nargs="?", type=int, default=None)
    gr.add_argument("-o", "--output", type=Path, default=None)
    gr.add_argument("--auto-name", action="store_true", help="Write to START[_END].ext (original)")
    gr.set_defaults(handler=cmd_get_range)

    jo = sub.add_parser("join", help="Join non-empty lines into one line")
    jo.add_argument("file", type=Path)
    jo.add_argument("--sep", default="", help="Separator (original default: none)")
    add_io(jo)
    jo.set_defaults(handler=cmd_join)

    mv = sub.add_parser("move-range", help="Move lines START..END to another file")
    mv.add_argument("src", type=Path)
    mv.add_argument("start", type=int)
    mv.add_argument("end", type=int)
    mv.add_argument("dest", type=Path)
    mv.add_argument("--copy", action="store_true", help="Keep lines in the source")
    mv.add_argument("--append", action="store_true", help="Append to dest instead of overwriting")
    mv.set_defaults(handler=cmd_move_range)

    so = sub.add_parser("sort-length", help="Sort lines by length")
    so.add_argument("file", type=Path)
    so.add_argument("-r", "--reverse", action="store_true", help="Longest first")
    add_io(so)
    so.set_defaults(handler=cmd_sort_length)

    st = sub.add_parser("strip-ws", help="Strip whitespace from each line")
    st.add_argument("file", type=Path)
    add_io(st)
    st.set_defaults(handler=cmd_strip_ws)

    ta = sub.add_parser("tabs", help="Tabs -> spaces (tree)")
    ta.add_argument("paths", nargs="*", type=Path)
    ta.add_argument("--width", type=int, default=DEFAULT_TAB_WIDTH)
    ta.set_defaults(handler=cmd_tabs)

    sp = sub.add_parser("space-out", help="Blank lines between every line")
    sp.add_argument("file", type=Path)
    sp.add_argument("--blanks", type=int, default=DEFAULT_BLANKS, help="Blank lines (original: 4)")
    add_io(sp)
    sp.set_defaults(handler=cmd_space_out)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
