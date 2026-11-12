#!/data/data/com.termux/files/usr/bin/env python
"""Convert between Jupyter notebooks, scripts and Markdown.
Subcommands: ``to-py`` exports a notebook as a script (``--hoist-imports``, ``--wrap-main``, magic stripping), ``to-md`` exports it as Markdown, ``to-ipynb`` rebuilds a notebook from a script (split by defs/imports, blanks or one cell), ``strip`` clears the outputs and ``meta-to-ipynb`` turns a Markdown document into cells.
Usage: notebook_convert_tool {to-py,to-md,to-ipynb,strip,meta-to-ipynb} [options] [paths ...] Positional arguments: paths ...
input Subcommands: to-py .ipynb -> .py to-md .ipynb -> .md to-ipynb .py -> .ipynb strip Clear outputs/execution counts meta-to-ipynb Markdown doc -> notebook Options: --hoist-imports Move imports/env lines to top (nb2py) --wrap-main Indent body under __main__ (nb2py) --magics -o, --output Single-file output name --split Auto: def/import breaks; single: one cell; blank: blank-run breaks -n, --dry-run Examples: $ notebook_convert_tool to-py analysis.ipynb $ notebook_convert_tool strip notebooks/ Command-line script: run ``python notebook_convert_tool.py`` or the ``notebook_convert_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import re
import sys

from dh import MAX_WORKERS

MAGIC_PREFIXES: tuple[str, ...] = ("%", "!", "%%")
CELL_MARKER: re.Pattern[str] = re.compile(r"^#\s*%%.*$")
CODE_FENCE: re.Pattern[str] = re.compile(r"```(?:python|py|shell|bash|sh)\s*\n(.*?)```", re.DOTALL)
Cell = dict[str, object]


def load_notebook(path: Path) -> dict[str, object]:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
        msg = f"not a notebook (no 'cells' list): {path}"
        raise ValueError(msg)
    return data


def save_notebook(notebook: dict[str, object], path: Path) -> None:
    path.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cell_source(cell: Cell) -> str:
    source: object = cell.get("source", "")
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source)


def new_notebook(cells: list[Cell]) -> dict[str, object]:
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def code_cell(source: str) -> Cell:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source,
        "id": f"cell-{abs(hash(source)) % 10**8:08d}",
    }


def md_cell(source: str) -> Cell:
    return {"cell_type": "markdown", "metadata": {}, "source": source, "id": f"cell-{abs(hash(source)) % 10**8:08d}"}


def find_files(inputs: Sequence[Path], suffix: str) -> list[Path]:
    roots: list[Path] = list(inputs) or [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        candidates = root.rglob(f"*{suffix}") if root.is_dir() else iter((root,))
        for path in candidates:
            if path.is_file() and ".ipynb_checkpoints" not in path.parts:
                found.add(path)
    return sorted(found)


def strip_magics(source: str, mode: str) -> str:
    out: list[str] = []
    for line in source.splitlines():
        stripped: str = line.lstrip()
        if stripped.startswith(MAGIC_PREFIXES):
            if mode == "comment":
                out.append(f"# [MAGIC] {line.rstrip()}")
            elif mode == "drop":
                continue
            else:
                out.append(line)
        else:
            out.append(line)
    return "\n".join(out)


def notebook_to_py(notebook: dict[str, object], hoist: bool, wrap_main: bool, magic_mode: str) -> str:
    imports: list[str] = []
    body: list[str] = []
    cells: object = notebook["cells"]
    for raw_cell in cells if isinstance(cells, list) else []:
        if not isinstance(raw_cell, dict):
            continue
        cell: Cell = raw_cell
        source: str = cell_source(cell)
        if cell.get("cell_type") == "markdown":
            body.append("\n".join(f"# {ln}".rstrip() for ln in source.splitlines()) + "\n")
        elif cell.get("cell_type") == "code":
            cleaned: str = strip_magics(source, magic_mode)
            if hoist:
                for ln in cleaned.splitlines():
                    if ln.strip().startswith("!nb2py"):
                        continue
                    if ln.startswith(("import ", "from ")):
                        imports.append(ln)
                    elif ln.startswith(("os.environ", "sys.path")):
                        imports.append(ln)
                    else:
                        body.append(ln)
                body.append("")
            else:
                body.append(cleaned + "\n")
    header: str = "\n".join(dict.fromkeys(imports)) + "\n\n" if imports else ""
    main_text: str = "\n".join(body).rstrip() + "\n"
    if wrap_main:
        indented: str = "\n".join(("    " + ln) if ln.strip() else ln for ln in main_text.splitlines())
        main_text = f"if __name__ == '__main__':\n{indented}\n"
    return header + main_text


def cmd_to_py(args: argparse.Namespace) -> int:
    return run_each(
        find_files(args.paths, ".ipynb"),
        lambda p: p.with_suffix(".py"),
        lambda p: notebook_to_py(load_notebook(p), args.hoist_imports, args.wrap_main, args.magics),
    )


def run_each(files: list[Path], dest_of: object, convert: object) -> int:
    if not files:
        print("No matching files found.")
        return 0
    failed: int = 0
    for path in files:
        try:
            dest: Path = dest_of(path)
            dest.write_text(convert(path), encoding="utf-8")
            print(f"{path.name} -> {dest.name}")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"failed: {path}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def notebook_to_md(notebook: dict[str, object]) -> str:
    out: list[str] = []
    cells: object = notebook["cells"]
    for raw_cell in cells if isinstance(cells, list) else []:
        if not isinstance(raw_cell, dict):
            continue
        source: str = cell_source(raw_cell)
        if raw_cell.get("cell_type") == "markdown":
            out.append(source.rstrip() + "\n")
        elif raw_cell.get("cell_type") == "code" and source.strip():
            out.append(f"```python\n{source.rstrip()}\n```\n")
    return "\n".join(out)


def cmd_to_md(args: argparse.Namespace) -> int:
    return run_each(
        find_files(args.paths, ".ipynb"), lambda p: p.with_suffix(".md"), lambda p: notebook_to_md(load_notebook(p))
    )


def split_py(code: str, mode: str) -> list[str]:
    if mode == "single":
        return [code.strip()] if code.strip() else []
    if CELL_MARKER.search(code):
        chunks: list[str] = [c.strip() for c in CELL_MARKER.split(code)]
        return [c for c in chunks if c]
    if mode == "blank":
        return [c.strip() for c in re.split(r"\n\s*\n\s*\n", code) if c.strip()]
    cells: list[str] = []
    current: list[str] = []
    lines: list[str] = code.splitlines()
    for index, line in enumerate(lines):
        starts_def: bool = line.startswith(("def ", "class ", "async def "))
        import_break: bool = (
            line.startswith(("import ", "from ")) and bool(current) and not current[-1].startswith(("import ", "from "))
        )
        blank_gap: bool = (
            not line.strip()
            and bool(current)
            and index + 1 < len(lines)
            and bool(lines[index + 1].strip())
            and not lines[index + 1].startswith((" ", "\t"))
        )
        if current and (starts_def or import_break or blank_gap):
            cells.append("\n".join(current).strip())
            current = []
        current.append(line.rstrip())
    if current:
        cells.append("\n".join(current).strip())
    return [c for c in cells if c]


def cmd_to_ipynb(args: argparse.Namespace) -> int:
    failed: int = 0
    for path in find_files(args.paths, ".py"):
        dest: Path = args.output if args.output and len(args.paths) == 1 else path.with_suffix(".ipynb")
        chunks: list[str] = split_py(path.read_text(encoding="utf-8"), args.split)
        save_notebook(new_notebook([code_cell(c) for c in chunks]), dest)
        print(f"{path.name} -> {dest.name} ({len(chunks)} cell(s))")
    return 1 if failed else 0


def strip_one(job: tuple[Path, bool]) -> tuple[Path, bool, str]:
    path, dry_run = job
    try:
        notebook: dict[str, object] = load_notebook(path)
    except (ValueError, json.JSONDecodeError) as exc:
        return path, False, str(exc)
    changed: bool = False
    cells: object = notebook["cells"]
    for cell in cells if isinstance(cells, list) else []:
        if isinstance(cell, dict) and cell.get("cell_type") == "code":
            if cell.get("outputs"):
                cell["outputs"] = []
                changed = True
            if cell.get("execution_count") is not None:
                cell["execution_count"] = None
                changed = True
    if changed and not dry_run:
        save_notebook(notebook, path)
    note: str = ("would strip" if dry_run else "stripped") if changed else "already clean"
    return path, changed, note


def cmd_strip(args: argparse.Namespace) -> int:
    files: list[Path] = find_files(args.paths, ".ipynb")
    jobs: list[tuple[Path, bool]] = [(p, args.dry_run) for p in files]
    stripped: int = 0
    with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        for path, changed, note in pool.map(strip_one, jobs):
            stripped += 1 if changed else 0
            print(f"{path}: {note}")
    print(f"{'Would strip' if args.dry_run else 'Stripped'}: {stripped}/{len(files)} notebook(s).")
    return 0


def md_to_cells(text: str) -> list[Cell]:
    cells: list[Cell] = []
    position: int = 0
    for match in CODE_FENCE.finditer(text):
        before: str = text[position : match.start()].strip()
        if before:
            cells.append(md_cell(before))
        code: str = match.group(1).strip()
        if code:
            cells.append(code_cell(code))
        position = match.end()
    tail: str = text[position:].strip()
    if tail:
        cells.append(md_cell(tail))
    return cells or [md_cell(text.strip())]


def cmd_meta_to_ipynb(args: argparse.Namespace) -> int:
    text: str = args.input.read_text(encoding="utf-8", errors="replace")
    dest: Path = args.output or args.input.with_suffix(".ipynb")
    cells: list[Cell] = md_to_cells(text)
    save_notebook(new_notebook(cells), dest)
    print(f"{args.input} -> {dest} ({len(cells)} cell(s))")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Notebook <-> script/markdown conversion.")
    sub = parser.add_subparsers(dest="command", required=True)
    tp = sub.add_parser("to-py", help=".ipynb -> .py")
    tp.add_argument("paths", nargs="*", type=Path)
    tp.add_argument("--hoist-imports", action="store_true", help="Move imports/env lines to top (nb2py)")
    tp.add_argument("--wrap-main", action="store_true", help="Indent body under __main__ (nb2py)")
    tp.add_argument("--magics", choices=("comment", "drop", "keep"), default="comment")
    tp.set_defaults(handler=cmd_to_py)
    tm = sub.add_parser("to-md", help=".ipynb -> .md")
    tm.add_argument("paths", nargs="*", type=Path)
    tm.set_defaults(handler=cmd_to_md)
    ti = sub.add_parser("to-ipynb", help=".py -> .ipynb")
    ti.add_argument("paths", nargs="+", type=Path)
    ti.add_argument("-o", "--output", type=Path, default=None, help="Single-file output name")
    ti.add_argument(
        "--split",
        choices=("auto", "single", "blank"),
        default="auto",
        help="auto: def/import breaks; single: one cell; blank: blank-run breaks",
    )
    ti.set_defaults(handler=cmd_to_ipynb)
    st = sub.add_parser("strip", help="Clear outputs/execution counts")
    st.add_argument("paths", nargs="*", type=Path)
    st.add_argument("-n", "--dry-run", action="store_true")
    st.set_defaults(handler=cmd_strip)
    mt = sub.add_parser("meta-to-ipynb", help="Markdown doc -> notebook")
    mt.add_argument("input", type=Path)
    mt.add_argument("-o", "--output", type=Path, default=None)
    mt.set_defaults(handler=cmd_meta_to_ipynb)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
