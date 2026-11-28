#!/data/data/com.termux/files/usr/bin/env python
"""Watch a path and copy archives (.zip/.whl/.tar.*) into ~/tmp/tgz.

Performs an initial filesystem walk, then watches with watchfiles (falling back to watchdog) copying every archive that appears; errors are logged per file.
"""

from __future__ import annotations
import os
import pathlib
from pathlib import Path
import shutil
import sys
import time
from typing import Any


# Prefer watchfiles; fall back to watchdog if unavailable.
try:
    from watchfiles import (
        Change,
        watch as wf_watch,
    )

    _BACKEND = "watchfiles"
except ImportError:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        _BACKEND = "watchdog"
    except ImportError:
        msg = "Neither 'watchfiles' nor 'watchdog' is installed. Install one of them, e.g. `pip install watchfiles`."
        raise ImportError(msg)


DEST_DIR = pathlib.Path("~/tmp/tgz").expanduser()
ALLOWED_EXT: tuple[str, ...] = (
    ".tar.gz",
    ".whl",
    ".tar.xz",
    ".zip",
    ".tar.bz2",
)


def copy_if_match(src_path: Path | str) -> None:
    if str(src_path).endswith(ALLOWED_EXT):
        try:
            pathlib.Path(DEST_DIR).mkdir(exist_ok=True, parents=True)
            dest = Path(DEST_DIR).joinpath(pathlib.Path(src_path).name)
            shutil.copy2(src_path, dest)
            print(src_path)
        except Exception as e:
            print(f"Failed to copy {src_path}: {e}")


def startup_scan(fpath: str) -> None:
    for root, _dirs, files in os.walk(fpath):
        for f in files:
            full_path = Path(root).joinpath(f)
            copy_if_match(full_path)


def run_watchfiles(path: str) -> None:
    """Watch using watchfiles (default backend)."""
    try:
        for changes in wf_watch(path, recursive=True):
            for change, changed_path in changes:
                if change in (Change.added, Change.modified):
                    copy_if_match(changed_path)
    except KeyboardInterrupt:
        pass


if _BACKEND == "watchdog":

    class CopyEventHandler(FileSystemEventHandler):
        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                copy_if_match(event.src_path)

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                copy_if_match(event.src_path)

    def run_watchdog(path: str) -> None:
        """Watch using watchdog (fallback backend)."""
        event_handler = CopyEventHandler()
        observer = Observer()
        observer.schedule(event_handler, path, recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/data/data/com.termux/files/usr/tmp"
    startup_scan(path)
    if _BACKEND == "watchfiles":
        run_watchfiles(path)
    else:
        run_watchdog(path)
