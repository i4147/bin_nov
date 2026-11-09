#!/data/data/com.termux/files/usr/bin/env python
"""Minify the HTML files of a tree in place.
Runs the ``minify-html`` backend on each ``.html`` file after validating the markup, writes the result back and processes the files with the chosen pool method (``--pool-method``).
Usage: mhtm [paths ...] [-b BACKEND] [--pool-method MAP] Positional arguments: paths ...
Files or directories to process.
Defaults to the current directory, recursively.
Options: -b, --backend --pool-method Examples: $ mhtm dist/ Command-line script: run ``python mhtm.py`` or the ``mhtm`` command.
The module is importable as well."""

import argparse
from collections.abc import Callable, Generator, Iterable
from functools import partial
from html.parser import HTMLParser
import multiprocessing as mp
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from dh import MAX_WORKERS
import minify_html as mh


class _HTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self.errors: list[str] = []
        self._void = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in self._void:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag in self._void:
            return
        if not self._stack:
            self.errors.append(f"unexpected </{tag}>")
            return
        if self._stack[-1] == tag:
            self._stack.pop()
            return
        if tag in self._stack:
            while self._stack and self._stack[-1] != tag:
                self.errors.append(f"unclosed <{self._stack.pop()}>")
            if self._stack:
                self._stack.pop()
        else:
            self.errors.append(f"stray </{tag}>")

    def close(self) -> None:
        super().close()
        while self._stack:
            self.errors.append(f"unclosed <{self._stack.pop()}>")


def validate_html(data: str) -> list[str]:
    v = _HTMLValidator()
    try:
        v.feed(data)
        v.close()
    except Exception as e:
        return [f"parse error: {e}"]
    return v.errors


def _minify_html_backend(data: str) -> str:
    return mh.minify(data)


BACKENDS: dict[str, Callable[[str], str]] = {
    "minify-html": _minify_html_backend,
}
DEFAULT_BACKEND: str = "minify-html"
POOL_METHODS: tuple[str, ...] = ("map", "starmap", "apply_async", "imap_unordered")
DEFAULT_POOL_METHOD: str = "imap_unordered"


def process_file(path: Path, backend: str) -> tuple[str, int, int, str]:
    rel = str(path)
    minify_fn = BACKENDS[backend]
    try:
        data = path.read_text(encoding="utf-8")
    except OSError as e:
        return rel, 0, 0, f"read error: {e}"
    before = len(data.encode("utf-8"))
    try:
        minified = minify_fn(data)
    except Exception as e:
        return rel, before, before, f"minify error: {e}"
    errors = validate_html(minified)
    if errors:
        return rel, before, before, f"invalid html: {'; '.join(errors[:3])}"
    if minified == data:
        return rel, before, before, ""
    try:
        path.write_text(minified, encoding="utf-8")
    except OSError as e:
        return rel, before, before, f"write error: {e}"
    after = len(minified.encode("utf-8"))
    return rel, before, after, ""


def iter_html_files(targets: Iterable[Path]) -> Generator[Path, None, None]:
    seen: set[Path] = set()
    for target in targets:
        try:
            target = target.resolve()
        except OSError:
            continue
        if target.is_file():
            if target.suffix.lower() in (".html", ".htm") and target not in seen:
                seen.add(target)
                yield target
        elif target.is_dir():
            for ext in ("*.html", "*.htm"):
                for p in target.rglob(ext):
                    if p.is_file() and p not in seen:
                        seen.add(p)
                        yield p


def _starmap_adapter(backend: str) -> Callable[[Path], tuple[str, int, int, str]]:
    return partial(process_file, backend=backend)


def run_pool(
    files: list[Path],
    backend: str,
    pool_method: str,
    processes: int,
) -> Generator[tuple[str, int, int, str], None, None]:
    with mp.Pool(processes=processes) as pool:
        if pool_method == "map":
            args_iter = ((f, backend) for f in files)
            yield from pool.starmap(process_file, args_iter)
        elif pool_method == "starmap":
            args_iter = ((f, backend) for f in files)
            yield from pool.starmap(process_file, args_iter)
        elif pool_method == "apply_async":
            async_results = [pool.apply_async(process_file, (f, backend)) for f in files]
            for ar in async_results:
                yield ar.get()
        elif pool_method == "imap_unordered":
            yield from pool.imap_unordered(_starmap_adapter(backend), files)
        else:
            msg = f"unknown pool method: {pool_method}"
            raise ValueError(msg)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Minify HTML files in place using a pluggable backend.",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process. Defaults to the current directory, recursively.",
    )
    ap.add_argument(
        "-b",
        "--backend",
        choices=sorted(BACKENDS),
        default=DEFAULT_BACKEND,
        help=f"Minifier backend to use (default: {DEFAULT_BACKEND}).",
    )
    ap.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default=DEFAULT_POOL_METHOD,
        help=f"multiprocessing.Pool dispatch strategy (default: {DEFAULT_POOL_METHOD}).",
    )
    return ap.parse_args()


def _fmt_bytes(n: int) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            if unit == "B":
                return f"{sign}{n} {unit}"
            return f"{sign}{n:.2f} {unit}"
        n /= 1024
    return f"{sign}{n:.2f} GiB"


def main() -> int:
    args = parse_args()
    targets: list[Path] = args.paths or [Path.cwd()]
    files = list(iter_html_files(targets))
    if not files:
        print("no HTML files found", file=sys.stderr)
        return 1
    changed_count = 0
    error_count = 0
    total_before = 0
    total_after = 0
    for rel, before, after, err in run_pool(files, args.backend, args.pool_method, MAX_WORKERS):
        total_before += before
        total_after += after
        if err:
            error_count += 1
            print(f"{rel}: {err}", file=sys.stderr)
        elif after != before:
            changed_count += 1
            saved = before - after
            print(f"{rel}: minified {_fmt_bytes(before)} -> {_fmt_bytes(after)} (saved {_fmt_bytes(saved)})")
    total_saved = total_before - total_after
    print(f"\nTotal input size:  {_fmt_bytes(total_before)}")
    print(f"Total output size: {_fmt_bytes(total_after)}")
    print(f"Total space freed: {_fmt_bytes(total_saved)}")
    print(f"Done: {changed_count}/{len(files)} file(s) minified, {error_count} error(s).")
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    raise SystemExit(main())
