#!/data/data/com.termux/files/usr/bin/env python
"""Re-encode every text file below the current directory as UTF-8 with LF line endings.
Walks plain files and the members of the archives it can open (through ``archive_walker``), skips binary data (NUL bytes), decodes as UTF-8 with a Latin-1 fallback, normalizes CRLF/CR to LF and writes the file back only when something changed.
Usage: dos2utf-8 Examples: $ dos2utf-8 Runs as a script (``python dos2utf-8.py``) in the directory it should act on; the module can also be imported.
"""

import sys

from archive_walker import AdvancedWalker, VirtualFile
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


def process_entry(entry: VirtualFile) -> None:
    try:
        data = entry.read_bytes()
        if b"\0" in data[:8192]:
            return
    except Exception as exc:
        logger.error(f"Cannot read {entry.path}: {exc}")
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    new_data = normalized.encode("utf-8")
    if new_data != data:
        try:
            entry.write_bytes(new_data)
            logger.info(f"Converted → UTF-8 + LF: {entry.path}")
        except Exception as exc:
            logger.error(f"Cannot write {entry.path}: {exc}")


def main() -> None:
    with AdvancedWalker(".", max_archive_depth=3) as walker:
        entries = list(walker)
        for entry in entries:
            process_entry(entry)
    logger.info("Done (archives rewritten if any changes were made).")


if __name__ == "__main__":
    main()
