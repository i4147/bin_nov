#!/data/data/com.termux/files/usr/bin/env python
"""Sort the archives in the current directory into folders and extract them.

Derives a directory name from each archive's stem (splitting on the first underscore), moves the archive into it, extracts it (zip/tar variants) and deletes the archive on success.
"""

from __future__ import annotations
from pathlib import Path
import shutil
import tarfile
import zipfile

from loguru import logger


TAR_MODES: dict[tuple[str, ...], str] = {
    (".tar.gz", ".tgz"): "r:gz",
    (".tar.bz2", ".tbz2"): "r:bz2",
    (".tar.xz", ".txz"): "r:xz",
    (".tar",): "r:",
}

ZIP_SUFFIXES: frozenset[str] = frozenset({".zip", ".whl"})


def safe_mkdir(base: Path) -> Path:
    """Create a directory, appending _1, _2, ... if the name is taken."""
    candidate: Path = base
    i: int = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{i}")
        i += 1
    candidate.mkdir()
    return candidate


def extract_archive(archive: Path, target_dir: Path) -> bool:
    """Extract a tar or zip archive into target_dir. Return True on success."""
    name: str = archive.name.lower()
    try:
        for suffixes, mode in TAR_MODES.items():
            if name.endswith(suffixes):
                with tarfile.open(archive, mode) as tar:
                    tar.extractall(target_dir, filter="data")
                return True

        if archive.suffix.lower() in ZIP_SUFFIXES:
            with zipfile.ZipFile(archive, "r") as zip_ref:
                zip_ref.extractall(target_dir)
            return True
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        logger.exception(f"Failed to extract archive: {archive}")
        return False

    return False


def main() -> None:
    cwd: Path = Path.cwd()

    for item in cwd.iterdir():
        if not item.is_file():
            continue

        stem: str = item.stem
        indx: int = stem.index("_") if "_" in stem else 8
        base_name: str = stem[:indx].replace(".", "").replace("-", "").replace("_", "")

        try:
            target_dir: Path = safe_mkdir(cwd / base_name)
            moved_file: Path = target_dir / item.name
            shutil.move(item, moved_file)
        except OSError:
            logger.exception(f"Failed to move file: {item}")
            print(f"[SKIP] Move failed: {item.name}")
            continue

        if extract_archive(moved_file, target_dir):
            moved_file.unlink()
            print(f"[OK] Unzipped and removed: {item.name}")
        else:
            print(f"[SKIP] Not a supported archive or unzip failed: {item.name}")


if __name__ == "__main__":
    raise SystemExit(main())
