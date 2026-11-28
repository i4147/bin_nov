#!/data/data/com.termux/files/usr/bin/env python
"""Codemod dh.cprint calls with legacy color names to keyword arguments.

Targets the second positional argument (foreground) and an on_-prefixed third (background) of cprint calls when their value is a known legacy color name, rewriting them as color= keyword arguments. Default is dry-run; --write applies.
"""

from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from dh import atomic_write
import libcst as cst
import typer


if TYPE_CHECKING:
    from pathlib import Path

LEGACY_COLORS: frozenset[str] = frozenset(
    {
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
    }
)


class _Codemod(cst.CSTTransformer):
    def __init__(self) -> None:
        self.changes: int = 0

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.Call:
        f = updated.func
        name = (
            f.value
            if isinstance(f, cst.Name)
            else (f.attr.value if isinstance(f, cst.Attribute) else None)
        )
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


class _Result(NamedTuple):
    path: Path
    changes: int
    note: str | None  # e.g. "parse error"; None means processed normally


def _process(path: Path, *, write: bool) -> _Result:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _Result(path, 0, None)
    try:
        tree = cst.parse_module(src)
    except cst.ParserSyntaxError:
        return _Result(path, 0, "parse error")
    mod = _Codemod()
    out = tree.visit(mod).code
    if mod.changes == 0:
        return _Result(path, 0, None)
    if write and out != src:
        atomic_write(path, out, encoding="utf-8")
    return _Result(path, mod.changes, None)


def _collect(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in paths:
        candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in candidates:
            key = f.resolve()
            if key in seen:
                continue
            seen.add(key)
            files.append(f)
    return files


app = typer.Typer(add_completion=False)


@app.command()
def main(
    paths: list[Path] = typer.Argument(..., help="Files or directories to scan."),
    write: bool = typer.Option(
        False, "--write", help="Apply changes (default is dry-run)."
    ),
) -> None:
    files = _collect(paths)

    workers = 4
    workers = max(1, min(workers, len(files)))

    worker = partial(_process, write=write)
    results: list[_Result]
    if workers <= 1:
        results = [worker(f) for f in files]
    else:
        with ProcessPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(worker, files, chunksize=16))

    tag = "rewrote" if write else "would rewrite"
    total = 0
    for r in results:
        if r.note is not None:
            typer.echo(f"  skip ({r.note}): {r.path}")
            continue
        if r.changes:
            typer.echo(f"{tag} {r.changes} call(s): {r.path}")
            total += r.changes

    suffix = "" if write else "  (dry-run)"
    typer.echo(f"total: {total} rewrite(s){suffix}")


if __name__ == "__main__":
    app()
