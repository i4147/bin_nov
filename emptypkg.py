#!/data/data/com.termux/files/usr/bin/env python
"""Find Python packages and wheels that contain no importable code.
Scans the given directories (or the current one) for ``.whl`` files whose ``RECORD`` lists nothing outside ``.dist-info`` and for installed ``*.dist-info`` directories without a module, then prints them, optionally writing the list to a CSV.
Usage: emptypkg [paths ...] Examples: $ emptypkg ~/wheels Runs as a script (``python emptypkg.py``) in the directory it should act on; the module can also be imported.
"""

import csv
from pathlib import Path
import sysconfig
import zipfile


def is_empty_package(dist_info_path: Path) -> bool:
    dist_info_path = Path(dist_info_path)
    record_file = dist_info_path / "RECORD"
    if not record_file.is_file():
        return False
    with record_file.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            rel_path = row[0]
            abs_path = (dist_info_path.parent / rel_path).resolve()
            if not str(abs_path).startswith(str(dist_info_path.resolve()) + "/"):
                return False
    return True


def is_empty_whl(whl_path: Path) -> bool:
    try:
        with zipfile.ZipFile(whl_path, "r") as zf:
            dist_info_dirs = [name for name in zf.namelist() if ".dist-info/" in name]
            if not dist_info_dirs:
                return False
            dist_info_dir = dist_info_dirs[0].split("/")[0] + "/"
            for file_name in zf.namelist():
                if file_name.endswith("/"):
                    continue
                if not file_name.startswith(dist_info_dir):
                    return False
            return True
    except zipfile.BadZipFile:
        print(f"Warning: {whl_path} is not a valid zip file")
        return False


def find_empty_packages(site_packages: str):
    site_packages_path = Path(site_packages)
    empty = []
    if not site_packages_path.is_dir():
        return empty
    for entry in site_packages_path.iterdir():
        if entry.name.endswith(".dist-info") and entry.is_dir() and is_empty_package(entry):
            empty.append(str(entry))
    return empty


def find_empty_wheels(cwd: Path) -> list:
    empty_wheels = []
    for file in cwd.glob("*.whl"):
        if is_empty_whl(file):
            empty_wheels.append(str(file))
    return empty_wheels


def main() -> None:
    site_packages = sysconfig.get_paths()["purelib"]
    empty_installed = find_empty_packages(site_packages)
    cwd = Path.cwd()
    empty_wheels = find_empty_wheels(cwd)
    if empty_installed:
        print("\n=== Empty installed packages (site-packages) ===")
        for pkg in empty_installed:
            print(f"  {pkg}")
    else:
        print("\nNo empty installed packages found.")
    if empty_wheels:
        print("\n=== Empty wheel files in current directory ===")
        for whl in empty_wheels:
            print(f"  {whl}")
    else:
        print("\nNo empty wheel files found in current directory.")
    if not empty_installed and not empty_wheels:
        print("\nNo empty packages or wheels found.")


if __name__ == "__main__":
    raise SystemExit(main())
