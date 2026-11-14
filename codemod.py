#!/data/data/com.termux/files/usr/bin/env python
"""Codemod: cprint('text', 'cyan') → cprint('text', color='cyan')."""

from __future__ import annotations

import argparse
from pathlib import Path

import libcst as cst

LEGACY_COLORS = frozenset({
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "grey",
    "gray",
    "light_red",
    "light_green",
    "light_yellow",
    "light_blue",
    "light_magenta",
    "light_cyan",
    "dark_grey",
    "light_grey",
})


class _Codemod(cst.CSTTransformer):
    def __init__(self) -> None:
        self.changes = 0

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.Call:
        f = updated.func
        name = f.value if isinstance(f, cst.Name) else (f.attr.value if isinstance(f, cst.Attribute) else None)
        if name != "cprint":
            return updated

        pos = [a for a in updated.args if a.keyword is None]
        kws = [a for a in updated.args if a.keyword is not None]
        if not (2 <= len(pos) <= 3):
            return updated
        second = pos[1]
        if not isinstance(second.value, cst.SimpleString):
            return updated
        v2 = second.value.evaluated_value
        if not isinstance(v2, str) or v2.lower() not in LEGACY_COLORS:
            return updated

        new_args: list[cst.Arg] = [
            pos[0],
            second.with_changes(keyword=cst.Name("color")),
        ]
        if len(pos) == 3:
            third = pos[2]
            if not isinstance(third.value, cst.SimpleString):
                return updated
            v3 = third.value.evaluated_value
            if not isinstance(v3, str):
                return updated
            bg_name = v3[3:] if v3.lower().startswith("on_") else v3
            if bg_name.lower() not in LEGACY_COLORS:
                return updated
            new_args.append(
                third.with_changes(
                    keyword=cst.Name("bg"),
                    value=cst.SimpleString(repr(bg_name)),
                )
            )
        new_args.extend(kws)
        self.changes += 1
        return updated.with_changes(args=new_args)


def _process(path: Path, *, write: bool) -> int:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    try:
        tree = cst.parse_module(src)
    except cst.ParserSyntaxError:
        print(f"  skip (parse error): {path}")
        return 0
    mod = _Codemod()
    out = tree.visit(mod).code
    if mod.changes == 0:
        return 0
    if write and out != src:
        path.write_text(out, encoding="utf-8")
    tag = "rewrote" if write else "would rewrite"
    print(f"{tag} {mod.changes} call(s): {path}")
    return mod.changes


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--write", action="store_true", help="Apply (default is dry-run)")
    args = p.parse_args()

    total = 0
    for root in args.paths:
        files = [root] if root.is_file() else list(root.rglob("*.py"))
        for f in files:
            total += _process(f, write=args.write)
    print(f"total: {total} rewrite(s)" + ("" if args.write else "  (dry-run)"))
    return 0
