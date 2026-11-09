#!/data/data/com.termux/files/usr/bin/env python
"""Copy the Colab site-packages into Google Drive as files and zips.
Mounts Drive, then copies the top-level files of ``site.getsitepackages()[0]`` to ``MyDrive/wheels`` and packs each top-level package directory into a deflate-compressed ``.zip`` (``.pyc`` files skipped, ``setuptools``/``pip`` excluded).
Meant to be pasted into a Colab notebook cell.
Usage: python colab1.py # from a Colab notebook cell Examples: %run colab1.py Run as a script; it needs a Colab runtime because it mounts ``/content/drive``."""

from pathlib import Path
import shutil
import site
import zipfile

from google.colab import drive

EXCLUDE_PREFIXES = "setuptools", "pip"


def excluded(name: str) -> bool:
    return name.startswith(EXCLUDE_PREFIXES)


def main() -> None:
    drive.mount("/content/drive")
    site_pkgs = Path(site.getsitepackages()[0])
    out_dir = Path("/content/drive/MyDrive/wheels")
    out_dir.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    zipped_dirs = 0
    for entry in site_pkgs.iterdir():
        name = entry.name
        if excluded(name):
            continue
        if entry.is_file():
            shutil.copy2(entry, out_dir / name)
            copied_files += 1
        elif entry.is_dir():
            zip_path = out_dir / f"{name}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in entry.rglob("*"):
                    if path.is_file() and path.suffix != ".pyc":
                        zf.write(path, path.relative_to(site_pkgs))
            zipped_dirs += 1
    print("Export completed successfully.")
    print(f"Site-packages source : {site_pkgs}")
    print(f"Output directory     : {out_dir}")
    print(f"Top-level files copied : {copied_files}")
    print(f"Top-level dirs zipped  : {zipped_dirs}")
    print("Excluded packages     : torch, tensorflow")


if __name__ == "__main__":
    main()
