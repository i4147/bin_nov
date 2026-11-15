#!/data/data/com.termux/files/usr/bin/env python
"""Remove every ``from __future__ import ...`` statement from Python files.

Performance model
-----------------
* ``from __future__ import ...`` always contains the bytes ``__future__``,
  so a byte-substring test skips untouched files *before* decoding or
  parsing.  On a clean tree this is the only per-file cost.
* Files that pass the byte test are parsed with :mod:`ast` (not a regex),
  so every syntactic form is handled: single name, comma list,
  parenthesised / multi-line list, trailing comments, aliases.
* Work is farmed out to a :class:`concurrent.futures.ProcessPoolExecutor`;
  each worker handles a batch of paths end-to-end.

Requires Python 3.10+.
"""

import ast
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys
from typing import Iterator, Sequence

from atomicio import atomic_write
from dh import get_pyfiles


# ---------------------------------------------------------------------------
# core transformer (pure, testable)
# ---------------------------------------------------------------------------


def strip_future_imports(text: str) -> str:
    """Remove every ``from __future__ import ...`` statement from *text*.

    Raises :class:`SyntaxError` if *text* is not valid Python.
    """
    tree = ast.parse(text)

    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            end = getattr(node, "end_lineno", None) or node.lineno
            ranges.append((node.lineno, end))

    if not ranges:
        return text

    ranges.sort()
    lines = text.split("\n")  # \n only: matches AST line numbering
    remove: set[int] = set()
    for start, end in ranges:
        remove.update(range(start - 1, end))
    new_lines = [ln for i, ln in enumerate(lines) if i not in remove]

    # Drop leading blank residue left by the removed import (and the blank
    # line after it), but only if the file originally started non-blank.
    if lines and lines[0].strip() and new_lines and not new_lines[0].strip():
        while new_lines and not new_lines[0].strip():
            new_lines.pop(0)

    return "\n".join(new_lines)


# ---------------------------------------------------------------------------
# per-file work (runs inside worker processes)
# ---------------------------------------------------------------------------

_FAST_MARKER: bytes = b"__future__"


def _process_one(path_str: str) -> tuple[str, int, int, str | None]:
    """Return ``(path, before_bytes, after_bytes, error_or_None)``.

    ``before == after`` iff the file was not modified.  ``error`` non-None
    means the file was skipped (never a partial write).
    """
    path = Path(path_str)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return path_str, 0, 0, f"read: {exc}"

    # ── fast path ──────────────────────────────────────────────────────
    # Bytes, not regex: `in` is a plain Boyer-Moore-ish scan in CPython and
    # beats `re.search` for a fixed literal.  Either would work; this is
    # the cheapest possible "should I bother?" test.
    if _FAST_MARKER not in raw:
        return path_str, len(raw), len(raw), None

    try:
        text = raw.decode("utf-8-sig")  # strips BOM if present
    except UnicodeDecodeError as exc:
        return path_str, len(raw), len(raw), f"decode: {exc}"

    try:
        updated = strip_future_imports(text)
    except SyntaxError as exc:
        return path_str, len(raw), len(raw), f"parse: {exc}"

    if updated == text:
        return path_str, len(raw), len(raw), None

    try:
        atomic_write(path, updated)
    except Exception as exc:  # noqa: BLE001
        return path_str, len(raw), len(raw), f"write: {exc}"

    return path_str, len(raw), len(updated.encode("utf-8")), None


def _process_batch(paths: list[str]) -> list[tuple[str, int, int, str | None]]:
    return [_process_one(p) for p in paths]


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

# Below this many files the ProcessPoolExecutor startup (~150 ms on Linux,
# more on Windows) costs more than sequential processing.  Tune to taste.
_PARALLEL_THRESHOLD: int = 100


def _iter_results(
    paths: list[str],
    workers: int,
) -> Iterator[tuple[str, int, int, str | None]]:
    """Yield per-file results, in walk order, sequential or parallel."""
    if len(paths) < _PARALLEL_THRESHOLD or workers <= 1:
        for p in paths:
            yield _process_one(p)
        return

    # Aim for ~4 batches per worker so we get load-balancing without
    # drowning in IPC.  Floor of 8 amortises pickling on tiny runs.
    target_batches = workers * 4
    batch_size = max(8, (len(paths) + target_batches - 1) // target_batches)
    batches = [paths[i : i + batch_size] for i in range(0, len(paths), batch_size)]

    workers = min(workers, len(batches))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        # ex.map preserves input order, so per-file output stays in walk
        # order and remains comparable across runs.
        for batch_result in ex.map(_process_batch, batches):
            yield from batch_result


def main(argv: Sequence[str]) -> int:
    roots = list(argv[1:]) or Path.cwd()
    paths = [str(p) for p in get_pyfiles(roots)]

    if not paths:
        print("scanned 0 python files, modified 0")
        return 0

    scanned = changed = 0
    for path_str, before, after, err in _iter_results(paths, 8):
        scanned += 1
        if err is not None:
            print(f"skip   {path_str}: {err}", file=sys.stderr)
            continue
        if after != before:
            changed += 1
            print(f"{path_str}    \x1b[38;5;10m{before - after}\x1b[0m")

    print(f"scanned {scanned} python files, modified {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
