#!/data/data/com.termux/files/usr/bin/env python
"""Reflow long prompt text files to 80 columns on list and sentence boundaries.
Splits the content before numbered items, bullet points, bold headings and commas, re-wraps the lines that exceed ``MAX_LINE`` at semicolons and rewrites every ``.txt`` file below the current directory that changes.
Usage: prompt-reformatter Examples: $ prompt-reformatter Runs as a script (``python prompt-reformatter.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import re

from dh import SKIP_DIRS

_BEFORE_MARKER = re.compile(r"[ \t\r\n]+(?=(?:\d+[.)]|\(\d+\)|[-*\u2022]|\*\*[^*\n]+\*\*)\s)")
_COMMA_SPLIT = re.compile(r",[ \t]+")
_SEMICOLON_SPLIT = re.compile(r";[ \t]+")
MAX_LINE: int = 80
_STRING_HEAD = re.compile(r'^([rRbBuUfF]*)("""|\'\'\'|"|\')')


def reformat(text: str) -> str:
    if not text.strip():
        return text
    lead_match = re.match(r"^([ \t]*(?:\r?\n[ \t]*)*)", text)
    leading = lead_match.group(1) if lead_match else ""
    trail_match = re.search(r"([ \t]*(?:\r?\n[ \t]*)*)$", text)
    trailing = trail_match.group(1) if trail_match else ""
    indent = leading.rsplit("\n", 1)[-1] if "\n" in leading else ""
    core_end = len(text) - len(trailing) if trailing else len(text)
    core = text[len(leading) : core_end]
    core = _BEFORE_MARKER.sub("\n" + indent, core)
    core = _COMMA_SPLIT.sub(",\n" + indent, core)
    if MAX_LINE > 0:
        rebuilt = []
        for line in core.split("\n"):
            if len(line) > MAX_LINE and ";" in line:
                line = _SEMICOLON_SPLIT.sub(";\n" + indent, line)
            rebuilt.append(line)
        core = "\n".join(rebuilt)
    return leading + core + trailing


def process_file(path: Path) -> bool:
    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    code = reformat(source)
    path.write_text(code, encoding="utf-8")
    return True


def iter_files(root: Path):
    for path in sorted(root.rglob("*.txt")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file():
            yield path


def main(root: Path | None = None) -> int:
    root = (root or Path.cwd()).resolve()
    changed = 0
    for path in iter_files(root):
        if process_file(path):
            print(f"formatted: {path.name}")
            changed += 1
    print(f"{changed} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
