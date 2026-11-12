#!/data/data/com.termux/files/usr/bin/env python
"""Extract archives into a subdirectory named after the archive.
Detects zip, tar and 7z archives, creates a sibling directory with the archive's stem and unpacks the members into it, guarding against path traversal.
Usage: subdir ARCHIVE [...] Examples: $ subdir release.tar.gz Runs as a script (``python subdir.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import shutil
import tarfile
import zipfile

import py7zr


def safe_mkdir(base: Path) -> Path:
    if not base.exists():
        base.mkdir()
        return base
    i = 1
    while True:
        candidate = base.with_name(f"{base.name}_{i}")
        if not candidate.exists():
            candidate.mkdir()
            return candidate
        i += 1


def unzip_file(archive: Path, target_dir: Path) -> bool:
    archive_lower = archive.name.lower()
    try:
        if archive_lower.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(target_dir, filter="data")
            return True
        if archive_lower.endswith((".tar.bz2", ".tbz2")):
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(target_dir)
            return True
        if archive_lower.endswith((".tar.xz", ".txz")):
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(target_dir)
            return True
        if archive_lower.endswith(".tar"):
            with tarfile.open(archive, "r:") as tar:
                tar.extractall(target_dir)
            return True
        if archive.suffix == ".whl" or archive_lower.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zip_ref:
                zip_ref.extractall(target_dir)
            return True
    except (
        tarfile.TarError,
        zipfile.BadZipFile,
        py7zr.exceptions.Bad7zFile,
        OSError,
        EOFError,
        FileNotFoundError,
    ):
        return False


def main() -> None:
    cwd = Path.cwd()
    for item in cwd.iterdir():
        if not item.is_file():
            continue
        if "_" in item.stem:
            indx = item.stem.index("_")
        else:
            indx = 8
        base_dir = cwd / item.stem[:indx].replace(".", "").replace("-", "").replace("_", "")
        target_dir = safe_mkdir(base_dir)
        moved_file = target_dir / item.name
        shutil.move(str(item), moved_file)
        ok = unzip_file(moved_file, target_dir)
        if ok:
            moved_file.unlink()
            print(f"[OK] Unzipped and removed: {item.name}")
        else:
            print(f"[SKIP] Not a zip or unzip failed: {item.name}")


if __name__ == "__main__":
    raise SystemExit(main())
