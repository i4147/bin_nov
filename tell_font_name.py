#!/data/data/com.termux/files/usr/bin/env python
"""Print the family names of the given font files and rename them accordingly.
Reads the name table with fontTools, picks the best family/subfamily combination, sanitises it into a file-name-safe string and renames the font file to ``<family>-<style>.ttf``.
Usage: tell_font_name FONT [...
] Examples: $ tell_font_name ~/fonts/*.ttf Runs as a script (``python tell_font_name.py``) in the directory it should act on; the module can also be imported.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any, Iterable

from dh import cprint, get_files, mpf, unique_path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTFont


def is_ascii_printable(s: str) -> bool:
    return all(32 <= ord(c) <= 126 for c in s)


def clean_filename(s: str) -> str:
    s = re.sub(r"[^\w\\-\.]", "", s)
    return s.strip("_-.")


def get_best_name(font: TTFont, name_id: int) -> str | None:
    fallback = None
    for rec in font["name"].names:
        if rec.nameID != name_id:
            continue
        try:
            name = rec.toUnicode().strip()
        except Exception:
            continue
        if rec.platformID == 3 and rec.langID == 1033:
            return name
        if is_ascii_printable(name):
            fallback = name
    return fallback


def get_font_names(path: Path) -> tuple[str, str] | tuple[None, None]:
    font = TTFont(path)
    family = get_best_name(font, 1)
    subfamily = get_best_name(font, 2)
    if not family:
        return (None, None)
    family = clean_filename(family)
    subfamily = "Regular" if not subfamily else clean_filename(subfamily)
    if subfamily.lower() == family.lower():
        subfamily = "Regular"
    return (family, subfamily)


def process_file(fn: Path) -> int:
    fn = Path(fn)
    try:
        family, style = get_font_names(fn)
    except Exception as e:
        cprint(f"error: {e}", "magenta")
        return 1
    if not family:
        cprint("name not found", "magenta")
        return 1
    ext = fn.suffix.lower()
    new_path = fn.parent / f"{family}-{style}{ext}"
    if fn.name == new_path.name:
        cprint("no change", "blue")
        return 0
    new_path = new_path.with_name(
        new_path.name
        .replace("_1", "")
        .replace("_2", "")
        .replace("_3", "")
        .replace("_4", "")
        .replace("_5", "")
        .replace("_6", "")
        .replace("_7", "")
        .replace("_8", "")
        .replace("_9", "")
    )
    if new_path.exists():
        new_path = unique_path(new_path)
    fn.rename(new_path)
    cprint(f"{new_path.name}", "green")
    return 0


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = list(
        [Path(arg) for arg in args]
        if args
        else get_files(cwd, extensions=[".ttf", ".woff", ".woff2", ".bin", ".otf", ".eot"])
    )
    if not files:
        print("no files found")
        return
    if len(files) == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
