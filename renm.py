#!/data/data/com.termux/files/usr/bin/env python
"""Rename the non-English file names below a directory to their English translation.
Detects names that contain non-ASCII characters, translates the stem with ``deep_translator`` in a worker pool, resolves collisions and renames the files and directories (deepest paths first).
``--directory`` selects the root, ``--pool-method`` the multiprocessing style.
Usage: renm [--directory DIR] [--pool-method map|starmap|imap_unordered|apply_async] Options: --directory Directory to scan and rename.
--pool-method Multiprocessing pool method to use for translation.
Examples: $ renm --directory ~/downloads Command-line script: run ``python renm.py`` or the ``renm`` command.
The module is importable as well."""

import argparse
from collections.abc import Iterator, Sequence
from multiprocessing.pool import AsyncResult, Pool
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Final, TypeAlias

from deep_translator import GoogleTranslator
from dh import MAX_WORKERS, unique_path
from loguru import logger
from tqdm import tqdm

DIRECTORY: Final[str] = "."
POOL_METHODS: Final[tuple[str, ...]] = (
    "map",
    "starmap",
    "imap_unordered",
    "apply_async",
)
NON_ENGLISH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\x00-\x7F]")
NamePair: TypeAlias = tuple[str, str]


def walk_files(directory: str) -> Iterator[str]:
    for root, _dirs, files in os.walk(directory):
        for name in files:
            yield Path(root).joinpath(name)


def is_english(text: str) -> bool:
    return NON_ENGLISH_PATTERN.search(text) is None


def translate_name(name: str) -> NamePair:
    path = Path(name)
    stem = path.stem
    suffix = path.suffix
    if is_english(stem):
        return (name, name)
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(stem)
        if not translated:
            return (name, name)
        return (name, translated + suffix)
    except Exception as exc:
        logger.debug(f"Translation failed for '{stem}': {exc}")
        return (name, name)


def _translate_name_tuple(item: tuple[str]) -> NamePair:
    return translate_name(item[0])


def _run_pool(names: Sequence[str], method: str) -> list[NamePair]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "map":
            return pool.map(_translate_name_tuple, [(name,) for name in names])
        if method == "starmap":
            return pool.starmap(translate_name, [(name,) for name in names])
        if method == "imap_unordered":
            return list(pool.imap_unordered(_translate_name_tuple, [(n,) for n in names]))
        if method == "apply_async":
            async_results: list[AsyncResult[NamePair]] = [pool.apply_async(translate_name, (name,)) for name in names]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def _build_translation_map(paths: Sequence[Path], pool_method: str) -> dict[str, str]:
    unique_names: list[str] = sorted({path.name for path in paths if not is_english(path.name)})
    if not unique_names:
        return {}
    translation_map: dict[str, str] = {}
    for original, translated in tqdm(
        _run_pool(unique_names, pool_method),
        total=len(unique_names),
        desc="Translating filenames",
    ):
        translation_map[original] = translated
    return translation_map


def rename_files(directory: Path, pool_method: str) -> None:
    if not directory.exists() or not directory.is_dir():
        logger.error(f"Not a directory: {directory}")
        return
    paths: list[Path] = [Path(p) for p in walk_files(str(directory))]
    if not paths:
        logger.info("No files found.")
        return
    translation_map = _build_translation_map(paths, pool_method)
    if not translation_map:
        logger.info("No non-English filenames found.")
        return
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        new_name = translation_map.get(path.name)
        if new_name is None or new_name == path.name:
            continue
        new_path = unique_path(path.with_name(new_name))
        try:
            path.rename(new_path)
            logger.info(f"Renamed: {path.name} -> {new_path.name}")
        except OSError as exc:
            logger.error(f"Error renaming {path.name}: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default=DIRECTORY,
        help="Directory to scan and rename.",
    )
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="map",
        help="Multiprocessing pool method to use for translation.",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    pool_method: str = args.pool_method
    directory: Path = Path(args.directory)
    rename_files(directory, pool_method)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
