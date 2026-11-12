#!/data/data/com.termux/files/usr/bin/env python
"""Turn the module docstrings of a source tree into prompt files.
Extracts the module docstring of every Python file with ast, reformats it into 80-column prose without its ``LiveDoc:`` line and writes one ``.txt`` per module into the prompt directory, optionally also a ``modules.json`` index; ``--remove`` strips the docstring from the source file instead.
Usage: xm [--root DIR] [--out-dir DIR] [-j] [-r] Options: -j, --json Additionally write a single aggregated modules.json.
-r, --remove Remove the module docstring from each source .py file in place.
--root Directory to scan recursively (default: cwd).
--out-dir Examples: $ xm --root src --out-dir prompts -j Command-line script: run ``python xm.py`` or the ``xm`` command.
The module is importable as well."""

import argparse
import ast
import concurrent.futures as cf
import json
from pathlib import Path
import re
import textwrap

from dh import MAX_WORKERS

OUTPUT_DIR: Path = Path.home() / "isaac" / "may" / "q1" / "prompts"
MAX_LINE: int = 80
LIVEDOC_RE: re.Pattern[str] = re.compile(r"^LiveDoc:\s*https?://\S+\s*$")
JSON_INDEX_NAME: str = "modules.json"


def strip_quotes(doc: str) -> str:
    doc = doc.strip()
    for quote in ('"""', "'''", '"', "'"):  # editor: this is for editor compat ''', """
        if doc.startswith(quote) and doc.endswith(quote) and len(doc) > 2 * len(quote):
            doc = doc[len(quote) : -len(quote)]
            break
    return doc.strip()


def extract_module_docstring(path: Path) -> str | None:
    try:
        source: str = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    try:
        tree: ast.Module = ast.parse(source)
    except SyntaxError:
        return None
    doc: str | None = ast.get_docstring(tree, clean=False)
    if not doc:
        return None
    return strip_quotes(doc)


def strip_trailing_livedoc(doc: str) -> str:
    lines: list[str] = doc.rstrip().splitlines()
    if len(lines) >= 2 and lines[-2].strip() == "---" and LIVEDOC_RE.match(lines[-1].strip()):
        lines = lines[:-2]
    return "\n".join(lines).rstrip()


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts: list[str] = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


def format_docstring(doc: str, width: int = MAX_LINE) -> str:
    lines: list[str] = []
    for sentence in split_sentences(doc):
        wrapped: list[str] = textwrap.wrap(
            sentence,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        lines.extend(wrapped)
    return "\n".join(lines)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem: str = path.stem
    suffix: str = path.suffix
    parent: Path = path.parent
    counter: int = 1
    while True:
        candidate: Path = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def remove_module_docstring(path: Path) -> bool:
    try:
        source: str = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    try:
        tree: ast.Module = ast.parse(source)
    except SyntaxError:
        return False
    if not tree.body:
        return False
    first: ast.stmt = tree.body[0]
    if not (
        isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
    ):
        return False
    lines: list[str] = source.splitlines(keepends=True)
    start: int = first.lineno - 1
    end: int = first.end_lineno
    new_source: str = "".join(lines[:start] + lines[end:])
    if not new_source.strip():
        new_source = "pass\n"
    path.write_text(new_source, encoding="utf-8")
    return True


def process_one(
    py_file: Path,
    out_dir: Path,
    remove: bool,
) -> tuple[Path, Path | None, dict[str, str] | None, bool]:
    doc: str | None = extract_module_docstring(py_file)
    if doc:
        doc = strip_trailing_livedoc(doc)
    out_path: Path | None = None
    json_entry: dict[str, str] | None = None
    if doc:
        formatted: str = format_docstring(doc)
        target: Path = out_dir / f"{py_file.stem}.txt"
        out_path = unique_path(target)
        out_path.write_text(formatted + "\n", encoding="utf-8")
        one_line: str = " ".join(formatted.split())
        json_entry = {py_file.name: one_line}
    removed: bool = False
    if remove:
        removed = remove_module_docstring(py_file)
    return py_file, out_path, json_entry, removed


def process(
    root: Path,
    out_dir: Path,
    json_out: bool,
    remove: bool,
    workers: int = MAX_WORKERS,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = sorted(root.rglob("*.py"))
    if not files:
        return
    json_entries: dict[Path, dict[str, str]] = {}
    with cf.InterpreterPoolExecutor(max_workers=workers) as ex:
        futures: dict[cf.Future[tuple[Path, Path | None, dict[str, str] | None, bool]], Path] = {
            ex.submit(process_one, f, out_dir, remove): f for f in files
        }
        for fut in cf.as_completed(futures):
            py_file: Path = futures[fut]
            try:
                _, out_path, json_entry, removed = fut.result()
            except Exception as exc:
                print(f"[!] {py_file.name}: {exc!r}")
                continue
            if out_path is not None:
                print(f"[+] {py_file.name} -> {out_path.name}")
            if json_entry is not None:
                json_entries[py_file] = json_entry
            if removed:
                print(f"[-] removed module docstring from {py_file.name}")
    if json_out and json_entries:
        ordered: list[dict[str, str]] = [json_entries[p] for p in sorted(json_entries)]
        json_path: Path = unique_path(out_dir / JSON_INDEX_NAME)
        json_path.write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[+] wrote {json_path.name} ({len(ordered)} entries)")


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Additionally write a single aggregated modules.json.",
    )
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove the module docstring from each source .py file in place.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory to scan recursively (default: cwd).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Output directory (default: {OUTPUT_DIR}).",
    )
    args: argparse.Namespace = parser.parse_args()
    process(
        args.root.resolve(),
        args.out_dir,
        args.json,
        args.remove,
        MAX_WORKERS,
    )


if __name__ == "__main__":
    main()
