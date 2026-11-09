#!/data/data/com.termux/files/usr/bin/env python
"""Convert the text files below the current directory to UTF-8 with LF line endings.
Decodes each file as UTF-8 (falling back to Latin-1), normalizes CRLF and lone CR to ``\n`` and rewrites the file only when the bytes change.
Files are processed in a worker pool.
Usage: dos2utf8 [paths ...] Examples: $ dos2utf8 Runs as a script (``python dos2utf8.py``) in the directory it should act on; the module can also be imported."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import sys
from typing import Iterator

from dh import SKIP_DIRS, is_binary
from loguru import logger


logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
)


def iter_files(root: Path) -> Iterator[Path]:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def process_file(path: Path) -> None:
    try:
        if is_binary(str(path)):
            return
        data = path.read_bytes()
    except OSError as exc:
        logger.error(f"Cannot read {path}: {exc}")
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        logger.debug(f"Fell back to latin-1 for {path}")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    new_data = normalized.encode("utf-8")
    if new_data != data:
        try:
            path.write_bytes(new_data)
            logger.info(f"Converted → UTF-8 + LF: {path}")
        except OSError as exc:
            logger.error(f"Cannot write {path}: {exc}")


def main() -> None:
    root = Path.cwd()
    files = list(iter_files(root))
    if not files:
        logger.info("No eligible files found.")
        return
    logger.info(f"Processing {len(files)} files under {root} …")
    max_workers = min(32, len(files) or 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_file, f): f for f in files}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                path = futures[future]
                logger.error(f"Unexpected error on {path}: {exc}")
    logger.info("Done.")


if __name__ == "__main__":
    main()
