#!/data/data/com.termux/files/usr/bin/env python
"""Unpack the ``.whl``/``.zip`` archives of a directory in place.
Extracts every wheel below the given directory into the same folder and deletes the archive, keeping the original when it is not a valid ZIP or cannot be written.
Usage: uzz [DIRECTORY] Examples: $ uzz ~/wheels Runs as a script (``python uzz.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from dh import get_files
from loguru import logger


def process_wheels(cwd: Path = Path.cwd()) -> None:
    wheel_files = list(get_files(cwd, ext=[".zip", ".whl"]))
    if not wheel_files:
        logger.warning("No .whl files found in {}", cwd.resolve())
        return
    logger.info("Found {} .whl file(s) in {}", len(wheel_files), cwd.resolve())
    for wheel_path in wheel_files:
        if not wheel_path.is_file():
            logger.debug("Skipping non-file entry: {}", wheel_path)
            continue
        logger.info("Processing {}", wheel_path.name)
        try:
            with ZipFile(wheel_path, "r") as archive:
                archive.extractall(path=wheel_path.parent)
            wheel_path.unlink()
            logger.success("Extracted and removed {}", wheel_path.name)
        except BadZipFile:
            logger.exception("{} is not a valid ZIP/wheel file; original kept", wheel_path)
        except PermissionError:
            logger.exception("Permission error on {}; original kept", wheel_path)
        except OSError:
            logger.exception("OS error while processing {}; original kept", wheel_path)
        except Exception:
            logger.exception("Unexpected error on {}; original kept", wheel_path)
    logger.info("Done.")


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path()
    process_wheels(target)
