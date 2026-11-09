#!/data/data/com.termux/files/usr/bin/env python
"""Text-encoding repair kit: convert to UTF-8 and decode escapes.

Analysis / deduplication
------------------------
- ``toutf8.py`` (chardet-detect + convert) and ``iso8859toutf8.py``
  (ISO-8859-1 -> UTF-8 for a hardcoded ``script.sh``) are one
  ``to-utf8`` command; ``try_decode.py`` (brute-force every codec and
  ask) is ``to-utf8 --probe`` / the ``probe`` subcommand. Detection now
  tries chardet if installed, else a strict decode probe over the
  common encodings, so stdlib alone is enough.
- ``decode_unicode.py`` (really ``bytes.decode('unicode_escape')`` -
  the ``.encode`` detour was a bug) and ``ufa.py`` (decode ``\\uXXXX``
  lines) share the ``unescape`` subcommand (``--kind codes`` for ufa).
- ``to_unicode.py`` (literal ``\\x`` hex -> chars) is
  ``unescape --kind hex``; ``decode_phonetic.py`` (a grand name for
  ``html.unescape``) is ``unescape --kind html``.

Examples
--------
  python3 encoding_tool.py to-utf8 legacy/
  python3 encoding_tool.py to-utf8 legacy/a.txt --from iso-8859-1
  python3 encoding_tool.py probe mystery.txt
  python3 encoding_tool.py unescape dumped.txt --kind unicode
"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import html
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


SAMPLE_SIZE: int = 100_000
DEFAULT_WORKERS: int = 8
COMMON_ENCODINGS: tuple[str, ...] = (
    "utf-8",
    "utf-8-sig",
    "cp1252",
    "cp1251",
    "cp1256",
    "cp437",
    "cp850",
    "cp866",
    "iso-8859-1",
    "iso-8859-2",
    "iso-8859-5",
    "iso-8859-6",
    "iso-8859-7",
    "iso-8859-8",
    "iso-8859-9",
    "mac_roman",
    "utf-16",
    "utf-32",
    "gbk",
    "big5",
    "shift_jis",
    "euc_jp",
    "euc_kr",
    "latin-1",
)
HEX_ESCAPE: re.Pattern[str] = re.compile(r"\\x([0-9a-fA-F]{2})")


def is_binary(data: bytes) -> bool:
    """Heuristic binary check on already-read bytes."""
    return b"\x00" in data[:8192]


def strict_decodings(data: bytes) -> list[str]:
    """Return common encodings that decode *data* without errors."""
    ok: list[str] = []
    for encoding in COMMON_ENCODINGS:
        try:
            data.decode(encoding)
            ok.append(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ok


def detect_encoding(data: bytes) -> str | None:
    """Best-guess encoding: strict UTF-8, then chardet, then first strict hit."""
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        import chardet

        guess: object = chardet.detect(data[:SAMPLE_SIZE]).get("encoding")
        if isinstance(guess, str) and guess:
            return guess
    except ImportError:
        pass
    candidates: list[str] = strict_decodings(data)
    return candidates[0] if candidates else None


def convert_one(job: tuple[Path, str | None, bool]) -> tuple[Path, str]:
    """Convert one file to UTF-8 in place; return (path, status line)."""
    path, source_encoding, dry_run = job
    try:
        data: bytes = path.read_bytes()
    except OSError as exc:
        return path, f"error: {exc}"
    if is_binary(data):
        return path, "skipped (binary)"
    encoding: str | None = source_encoding or detect_encoding(data)
    if encoding is None:
        return path, "failed (encoding undetectable)"
    if encoding.lower().replace("_", "-") in ("utf-8", "utf8", "ascii"):
        return path, "already utf-8"
    try:
        text: str = data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        return path, f"failed ({encoding}: {exc})"
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return path, f"{'would convert' if dry_run else 'converted'} ({encoding} -> utf-8)"


def collect_files(inputs: Sequence[Path]) -> list[Path]:
    """Collect files from file/dir arguments."""
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            found.add(root)
        elif root.is_dir():
            found.update(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    return sorted(found)


def cmd_to_utf8(args: argparse.Namespace) -> int:
    """Run the ``to-utf8`` subcommand."""
    files: list[Path] = collect_files(args.paths)
    if not files:
        print("No files found.")
        return 0
    jobs: list[tuple[Path, str | None, bool]] = [(p, args.from_encoding, args.dry_run) for p in files]
    changed: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for path, status in pool.map(convert_one, jobs):
            changed += 1 if status.startswith(("converted", "would")) else 0
            if args.verbose or not status.startswith(("already", "skipped")):
                print(f"{path}: {status}")
    print(f"Done: {changed} converted / {len(files)} file(s).")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """List encodings that can decode a file (optionally write UTF-8)."""
    data: bytes = args.file.read_bytes()
    candidates: list[str] = strict_decodings(data)
    print(f"{args.file}: {len(data)} bytes")
    if not candidates:
        print("No common encoding decodes this file strictly.")
        return 1
    for encoding in candidates:
        preview: str = data.decode(encoding, errors="replace")[: args.preview].replace("\n", " ")
        print(f"  {encoding:15} {preview!r}")
    if args.output is not None:
        chosen: str = args.encoding or candidates[0]
        args.output.write_text(data.decode(chosen), encoding="utf-8")
        print(f"Wrote UTF-8 ({chosen}) to {args.output}")
    return 0


def decode_hex_escapes(text: str) -> str:
    """Turn literal ``\\x41`` sequences into ``A``."""

    def repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return HEX_ESCAPE.sub(repl, text)


def decode_unicode_escapes(text: str) -> str:
    """Turn literal ``\\u00e9`` / ``\\n`` escapes into real characters."""
    return text.encode("utf-8").decode("unicode_escape")


def decode_code_lines(text: str) -> str:
    """Decode lines that each hold one bare hex code like ``0627``."""
    out: list[str] = []
    for line in text.splitlines():
        code: str = line.strip()
        out.append(chr(int(code, 16)) if code else "")
    return "\n".join(out) + "\n"


UNESCAPE_KINDS: dict[str, Any] = {
    "unicode": decode_unicode_escapes,
    "hex": decode_hex_escapes,
    "html": html.unescape,
    "codes": decode_code_lines,
}


def cmd_unescape(args: argparse.Namespace) -> int:
    """Decode escape sequences / HTML entities in place (or to stdout)."""
    decoder = UNESCAPE_KINDS[args.kind]
    for path in args.files:
        if not path.is_file():
            print(f"Error: file not found: {path}", file=sys.stderr)
            return 1
        result: str = decoder(path.read_text(encoding="utf-8", errors="replace"))
        if args.stdout:
            print(result, end="")
        else:
            path.write_text(result, encoding="utf-8")
            print(f"{path}: decoded ({args.kind})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Encoding detection, UTF-8 conversion and unescaping.")
    sub = parser.add_subparsers(dest="command", required=True)

    tu = sub.add_parser("to-utf8", help="Convert text files to UTF-8 in place")
    tu.add_argument("paths", nargs="*", type=Path, help="Files or dirs (default: .)")
    tu.add_argument("--from", dest="from_encoding", default=None, help="Source encoding (default: auto-detect)")
    tu.add_argument("-n", "--dry-run", action="store_true")
    tu.add_argument("-v", "--verbose", action="store_true")
    tu.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS)
    tu.set_defaults(handler=cmd_to_utf8)

    pr = sub.add_parser("probe", help="Show encodings that decode a file")
    pr.add_argument("file", type=Path)
    pr.add_argument("--preview", type=int, default=80, help="Preview chars per encoding (default: 80)")
    pr.add_argument("--encoding", default=None, help="Encoding to use with -o (default: first match)")
    pr.add_argument("-o", "--output", type=Path, default=None, help="Write UTF-8 conversion here")
    pr.set_defaults(handler=cmd_probe)

    un = sub.add_parser("unescape", help="Decode escapes/entities in files")
    un.add_argument("files", nargs="+", type=Path)
    un.add_argument(
        "--kind",
        choices=sorted(UNESCAPE_KINDS),
        default="unicode",
        help="unicode: \\uXXXX/\\n escapes; hex: \\xXX; html: entities; codes: bare hex per line",
    )
    un.add_argument("--stdout", action="store_true", help="Print instead of rewriting")
    un.set_defaults(handler=cmd_unescape)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
