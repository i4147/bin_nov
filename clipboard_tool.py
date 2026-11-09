#!/data/data/com.termux/files/usr/bin/env python
"""Clipboard helpers: file -> clipboard, clipboard -> script file.
Analysis / deduplication ------------------------ - ``l2c.py`` (copy one 0-based line; it never actually sent the text - the ``communicate()`` call was missing) and ``tcs.py`` (copy a range / selected lines) are one ``copy`` command using 1-based lines.
Original ``tcs -s`` selection is ``copy --lines 3,7,9``.
- ``tcg.py`` vs ``tcg3.py`` both write the clipboard to a new script, fix the shebang and add an extension-less symlink.
``tcg3`` detects the language from content when the extension is unknown and has the correct python shebang, so its behaviour (plus ``tcg``'s rust support and ``-a`` archiving) is the merged ``paste`` command.
- ``f2l.py`` rewrites a file's lines as ``{"a", "b"}`` and copies it: the ``to-list`` command.
Clipboard commands default to Termux (``termux-clipboard-get/set``) as in the originals; on desktop Linux pass ``--get-cmd wl-paste`` / ``--set-cmd wl-copy`` or ``xclip -o`` / ``xclip -selection clipboard``.
Examples -------- python3 clipboard_tool.py copy notes.txt # whole file python3 clipboard_tool.py copy notes.txt 10 20 # lines 10-20 python3 clipboard_tool.py copy notes.txt --lines 1,5 python3 clipboard_tool.py paste myscript.py python3 clipboard_tool.py to-list names.txt"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


DEFAULT_GET_CMD: str = "termux-clipboard-get"
DEFAULT_SET_CMD: str = "termux-clipboard-set"
SHEBANG_PREFIX: str = "#!/data/data/com.termux/files/usr/bin"
SHEBANGS: dict[str, str] = {
    "python": f"{SHEBANG_PREFIX}/python",
    "bash": f"{SHEBANG_PREFIX}/bash",
    "sh": f"{SHEBANG_PREFIX}/sh",
    "rust": "#!/data/data/com.termux/files/home/.cargo/bin/rust-script",
}
EXT_TO_LANG: dict[str, str] = {".py": "python", ".pyw": "python", ".sh": "bash", ".bash": "bash", ".rs": "rust"}
DEFAULT_SCRIPT_DIRS: tuple[Path, ...] = (
    Path.home() / "bin",
    Path.home() / "bashbin",
    Path.home() / ".local" / "bin",
    Path.home() / ".cargo" / "bin",
)
DEFAULT_ARCHIVE_DIR: Path = Path.home() / "isaac" / "may" / "scripts"


def clipboard_get(cmd: str) -> str:
    """Return the current clipboard text."""
    proc = run_clipboard(cmd, None)
    return proc.stdout


def clipboard_set(text: str, cmd: str) -> None:
    """Put *text* onto the clipboard."""
    run_clipboard(cmd, text)


def run_clipboard(cmd: str, stdin_text: str | None) -> subprocess.CompletedProcess[str]:
    """Run a clipboard command, exiting helpfully when it is missing."""
    argv: list[str] = shlex.split(cmd)
    try:
        return subprocess.run(argv, input=stdin_text, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        msg = f"Clipboard command not found: {argv[0]} (Termux: install termux-api; desktop: use --get-cmd/--set-cmd)"
        raise SystemExit(msg) from None
    except subprocess.CalledProcessError as exc:
        msg = f"Clipboard command failed: {exc.stderr.strip()}"
        raise SystemExit(msg) from None


def select_lines(lines: list[str], start: int | None, end: int | None, wanted: list[int] | None) -> str:
    """Pick 1-based lines by explicit list or by inclusive range."""
    total: int = len(lines)
    if wanted:
        chosen: list[str] = [lines[i] for i in wanted if 1 <= i <= total]
        return "".join(line if line.endswith("\n") else line + "\n" for line in chosen)
    if start is None:
        return "".join(lines)
    lo: int = min(max(start, 1), total)
    hi: int = total if end is None else min(max(end, lo), total)
    return "".join(lines[lo - 1 : hi])


def parse_line_list(spec: str) -> list[int]:
    """Parse ``"1, 5,9"`` into a list of 1-based line numbers."""
    return [int(part) for part in spec.replace(" ", "").split(",") if part]


def cmd_copy(args: argparse.Namespace) -> int:
    """Copy a file (or part of it) to the clipboard."""
    if not args.path.is_file():
        print(f"Error: file not found: {args.path}", file=sys.stderr)
        return 1
    lines: list[str] = args.path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    wanted: list[int] | None = parse_line_list(args.lines) if args.lines else None
    content: str = select_lines(lines, args.start, args.end, wanted)
    if not content:
        print("Nothing selected to copy.", file=sys.stderr)
        return 1
    if args.stdout:
        print(content, end="")
    else:
        clipboard_set(content, args.set_cmd)
        print(f"Copied {len(content.splitlines())} line(s) from {args.path}")
    return 0


def detect_language(filename: str, content: str) -> str:
    """Guess python/bash/rust from the extension, then from the content."""
    ext: str = Path(filename).suffix.lower()
    if ext in EXT_TO_LANG:
        return EXT_TO_LANG[ext]
    first: str = content.lstrip().split("\n", 1)[0] if content.strip() else ""
    if first.startswith("#!") and "python" in first.lower():
        return "python"
    preview: str = content[:500].lower()
    python_hints: tuple[str, ...] = ("import ", "from ", "def ", "class ", "if __name__", "print(", "async def")
    bash_hints: tuple[str, ...] = ("echo ", "export ", "if [", "then", "fi", "done", "esac", "$(", "${")
    score_py: int = sum(hint in preview for hint in python_hints)
    score_sh: int = sum(hint in preview for hint in bash_hints)
    return "python" if score_py > score_sh else "bash"


def with_shebang(content: str, language: str, prefix: str) -> str:
    """Return *content* with its shebang replaced by the right one."""
    shebang: str = SHEBANGS[language].replace(SHEBANG_PREFIX, prefix) if language != "rust" else SHEBANGS[language]
    lines: list[str] = content.splitlines()
    while lines and lines[0].startswith("#!"):
        lines.pop(0)
    result: str = "\n".join([shebang, *lines])
    return result if result.endswith("\n") else result + "\n"


def unique_path(path: Path) -> Path:
    """Return *path* or ``name_N.suffix`` when it already exists."""
    counter: int = 1
    candidate: Path = path
    while candidate.exists():
        candidate = path.parent / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    return candidate


def cmd_paste(args: argparse.Namespace) -> int:
    """Write the clipboard to a new (script) file."""
    output: Path = Path(args.filename)
    script_dirs: set[Path] = set(DEFAULT_SCRIPT_DIRS)
    is_script_dir: bool = Path.cwd() in script_dirs or Path.cwd().name == "bin" or args.script
    if args.archive and output.exists():
        args.archive_dir.mkdir(parents=True, exist_ok=True)
        dest: Path = unique_path(args.archive_dir / output.name)
        shutil.move(str(output), str(dest))
        print(f"Archived previous file to {dest}")
    content: str = clipboard_get(args.get_cmd)
    if not content.strip():
        print("Clipboard is empty; creating an empty file.")
        content = "\n"
    if is_script_dir:
        language: str = detect_language(args.filename, content)
        content = with_shebang(content, language, args.shebang_prefix)
        print(f"Language: {language}")
    output.write_text(content, encoding="utf-8")
    print(f"Created: {output}")
    if is_script_dir:
        output.chmod(0o755)
        if output.suffix and not args.no_symlink:
            link: Path = output.parent / output.stem
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to(output)
                print(f"Symlink: {link}")
    return 0


def cmd_to_list(args: argparse.Namespace) -> int:
    """Rewrite a file as ``{"line1", "line2"}`` and copy it."""
    if not args.path.is_file():
        print(f"Error: file not found: {args.path}", file=sys.stderr)
        return 1
    lines: list[str] = [line.strip() for line in args.path.read_text(encoding="utf-8").splitlines()]
    items: list[str] = [f"'{line}'" if '"' in line else f'"{line}"' for line in lines if line]
    formatted: str = "{" + ", ".join(items) + "}"
    if not args.no_write:
        args.path.write_text(formatted, encoding="utf-8")
    clipboard_set(formatted, args.set_cmd)
    print(formatted)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Clipboard helpers (Termux by default).")
    parser.add_argument("--get-cmd", default=DEFAULT_GET_CMD, help="Clipboard read command")
    parser.add_argument("--set-cmd", default=DEFAULT_SET_CMD, help="Clipboard write command")
    sub = parser.add_subparsers(dest="command", required=True)

    cp = sub.add_parser("copy", help="Copy a file / line range to the clipboard")
    cp.add_argument("path", type=Path)
    cp.add_argument("start", nargs="?", type=int, default=None, help="First line (1-based)")
    cp.add_argument("end", nargs="?", type=int, default=None, help="Last line (1-based)")
    cp.add_argument("--lines", default="", help='Explicit 1-based lines, e.g. "1,5,9"')
    cp.add_argument("--stdout", action="store_true", help="Print instead of copying")
    cp.set_defaults(handler=cmd_copy)

    pa = sub.add_parser("paste", help="Write the clipboard to a new script file")
    pa.add_argument("filename", help="Output filename, e.g. myscript.py")
    pa.add_argument("-a", "--archive", action="store_true", help="Archive an existing file first")
    pa.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    pa.add_argument("--script", action="store_true", help="Force script mode (shebang+chmod+symlink)")
    pa.add_argument("--no-symlink", action="store_true", help="Skip the extension-less symlink")
    pa.add_argument(
        "--shebang-prefix", default=SHEBANG_PREFIX, help="Interpreter prefix for shebangs (desktop: /usr/bin)"
    )
    pa.set_defaults(handler=cmd_paste)

    tl = sub.add_parser("to-list", help='Turn file lines into {"a", "b"} and copy')
    tl.add_argument("path", type=Path)
    tl.add_argument("--no-write", action="store_true", help="Do not rewrite the input file")
    tl.set_defaults(handler=cmd_to_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
