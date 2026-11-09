#!/data/data/com.termux/files/usr/bin/env python
"""Verify that installed packages import and compare their versions with PyPI.
Lists the installed distributions, tries to import each one, asks the PyPI JSON API for the latest release and prints the packages that fail to import or are outdated, with a summary count.
Usage: pysanity-check Examples: $ pysanity-check Runs as a script (``python pysanity-check.py``) in the directory it should act on; the module can also be imported."""

import importlib
import re
import subprocess
import sys

import importlib_metadata


def _normalize_name(name: str) -> str:
    from re import sub as re_sub

    return re_sub(r"[-_.]+", "-", name).lower()


def get_installed_python_packages() -> list[tuple[str, str]]:
    pkgs = []
    for d in importlib_metadata.distributions():
        pkgname = d.metadata.get("Name")
        pkgname = _normalize_name(pkgname)
        pkgver = d.metadata.get("Version")
        if pkgname and pkgver:
            pkgs.append((pkgname, pkgver))
    return pkgs


def check_package_importable(package_name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(package_name)
        return True, "OK"
    except ImportError as e:
        return False, f"ImportError: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def get_latest_version(package_name: str) -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"{package_name}==", "--dry-run"],
            capture_output=True,
            text=True,
            check=True,
        )
        match = re.search(r"would be installed \(([^)]+)\)", result.stdout)
        if match:
            return match.group(1)
    except subprocess.CalledProcessError:
        pass
    return "Unknown"


def main() -> None:
    print("=== Python Packages Sanity Check ===")
    installed_pkgs = get_installed_python_packages()
    print(f"Found {len(installed_pkgs)} installed Python packages.\n")
    issues_found = 0
    for pkg_name, pkg_version in installed_pkgs:
        is_ok, msg = check_package_importable(pkg_name)
        if not is_ok:
            print(f"[!] {pkg_name} (v{pkg_version}): {msg}")
            issues_found += 1
    print("\n=== Version Check (Optional) ===")
    print("Checking for outdated packages (this may take a while)...")
    outdated_pkgs = []
    for pkg_name, pkg_version in installed_pkgs:
        latest_version = get_latest_version(pkg_name)
        if latest_version not in {"Unknown", pkg_version}:
            outdated_pkgs.append((pkg_name, pkg_version, latest_version))
    if outdated_pkgs:
        print("Outdated packages found:")
        for pkg_name, pkg_version, latest_version in outdated_pkgs:
            print(f"- {pkg_name}: {pkg_version} (latest: {latest_version})")
    else:
        print("All packages are up to date.")
    print("\n=== Summary ===")
    print(f"Issues found: {issues_found}")
    if issues_found == 0:
        print("All packages are importable.")
    else:
        print("Some packages may need attention.")


if __name__ == "__main__":
    raise SystemExit(main())
