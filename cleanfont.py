#!/data/data/com.termux/files/usr/bin/env python
"""Subset fonts in place, dropping CJK glyphs.
Usage: cleanfont [FILE_OR_DIR ...] Each font is rewritten in place (atomically, via a temp file in the same directory).
Directories are walked recursively; only .ttf/.otf/.woff/.woff2 files are picked up from directories (explicitly named files are attempted regardless of extension).
With no arguments, processes the current directory ('.').
Examples: $ cleanfont # walk '.' recursively $ cleanfont NotoSans.ttf # single file $ cleanfont fonts/ ~/Downloads # several dirs/files
"""

import contextlib
import os
import shutil
import sys
import tempfile
from typing import Any

from fontTools import subset

# Unicode blocks that carry Chinese (and CJK-shared) glyphs.
CJK_BLOCKS: list[tuple[Any, ...]] = [
    (0x2E80, 0x2EFF),  # CJK Radicals Supplement
    (0x2F00, 0x2FDF),  # Kangxi Radicals
    (0x3000, 0x303F),  # CJK Symbols and Punctuation
    (0x31C0, 0x31EF),  # CJK Strokes
    (0x3200, 0x32FF),  # Enclosed CJK Letters and Months
    (0x3300, 0x33FF),  # CJK Compatibility
    (0x3400, 0x4DBF),  # Unified Ideographs Ext. A
    (0x4E00, 0x9FFF),  # Unified Ideographs
    (0xF900, 0xFAFF),  # Compatibility Ideographs
    (0xFE30, 0xFE4F),  # CJK Compatibility Forms
    (0xFF00, 0xFFEF),  # Halfwidth/Fullwidth Forms
    (0x20000, 0x2A6DF),  # Ext. B
    (0x2A700, 0x2EBEF),  # Ext. C–F
    (0x2F800, 0x2FA1F),  # Compatibility Ideographs Supplement
    (0x30000, 0x323AF),  # Ext. G–H
]
# Add these if you also want Japanese/Korean out:
# (0x3040,0x30FF) kana, (0x3100,0x312F) bopomofo, (0xAC00,0xD7AF) hangul

FONT_EXTS: set[str] = {".ttf", ".otf", ".woff", ".woff2"}


def is_cjk(cp: Any) -> bool:
    return any(lo <= cp <= hi for lo, hi in CJK_BLOCKS)


def make_options():
    opts = subset.Options()
    opts.layout_features = ["*"]  # keep GSUB/GPOS rules that survive
    opts.name_IDs = ["*"]  # keep full name table
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.recommended_glyphs = True  # space, nbsp, .notdef
    opts.glyph_names = True
    return opts


def subset_inplace(path: str) -> None:
    opts = make_options()
    font = subset.load_font(path, opts)
    tmp = None
    try:
        cmap = font.getBestCmap()
        keep = sorted(cp for cp in cmap if not is_cjk(cp))
        dropped = len(cmap) - len(keep)
        if dropped == 0:
            print(f"{path}: no CJK glyphs, skipping")
            return

        print(f"{path}: keeping {len(keep):,} code points (dropping {dropped:,})")
        sub = subset.Subsetter(options=opts)
        sub.populate(unicodes=keep)
        sub.subset(font)

        ext = os.path.splitext(path)[1]
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".cleanfont-", suffix=ext, dir=d)
        os.close(fd)

        subset.save_font(font, tmp, opts)
        font.close()
        font = None

        shutil.copymode(path, tmp)  # preserve original permissions
        os.replace(tmp, path)  # atomic in-place update
        tmp = None
    finally:
        if font is not None:
            font.close()
        if tmp is not None and os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def collect_targets(args: list[Any]):
    if not args:
        args = ["."]
    seen, out = set(), []
    for a in args:
        if os.path.isdir(a):
            for root, _dirs, names in os.walk(a):
                for n in names:
                    if os.path.splitext(n)[1].lower() in FONT_EXTS:
                        p = os.path.join(root, n)
                        rp = os.path.realpath(p)
                        if rp not in seen:
                            seen.add(rp)
                            out.append(p)
        else:
            rp = os.path.realpath(a)
            if rp not in seen:
                seen.add(rp)
                out.append(a)
    return out


def main(argv: list[Any]) -> int:
    files = collect_targets(argv)
    if not files:
        print("no font files found", file=sys.stderr)
        return 1

    failures = 0
    for p in files:
        try:
            subset_inplace(p)
        except Exception as e:
            print(f"{p}: ERROR: {e}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
