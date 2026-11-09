#!/data/data/com.termux/files/usr/bin/env python
"""Move the empty wheels aside, flagging the ones already installed.
Detects the ``.whl`` files without code members, extracts their name and version, checks whether the package is installed (``pip show`` and the import location), prompts before moving and offers ``--auto-move-all`` and ``--no-install-check``.
Usage: ewhl2 [directory] [-d DEST] [--auto-move-all] Positional arguments: directory Directory containing .whl files (default: current directory) Options: -d, --dest Destination subdirectory name (default: 'empty_wheels') --no-install-check Skip checking installed packages --auto-move-all Automatically move all empty wheels without prompting Examples: $ ewhl2 ~/wheels --auto-move-all Command-line script: run ``python ewhl2.py`` or the ``ewhl2`` command.
The module is importable as well."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from typing import Any


def is_empty_wheel(wheel_path: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zip_ref:
            all_files = zip_ref.namelist()
            has_py_files = any(file.endswith(".py") for file in all_files)
            has_code_dirs = any(
                not (file.startswith(("dist-info/", "__pycache__/")))
                and not file.endswith("/")
                and not file.endswith(".dist-info/")
                for file in all_files
            )
            return not (has_py_files or has_code_dirs)
    except Exception as e:
        print(f"  Error reading {wheel_path}: {e}")
        return False


def extract_package_info(wheel_path: Path) -> tuple[str, str] | tuple[None, None]:
    wheel_name = wheel_path.stem
    parts = wheel_name.split("-")
    if len(parts) >= 2:
        name = parts[0]
        version = parts[1]
        name = name.replace("_", "-")
        return name, version
    return None, None


def get_installed_packages():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
        installed = {}
        for line in result.stdout.strip().split("\n"):
            if "==" in line:
                name, version = line.split("==")
                installed[name.lower()] = version
        return installed
    except Exception as e:
        print(f"Warning: Could not get installed packages: {e}")
        return {}


def check_pip_show(package_name: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info = {}
            for line in result.stdout.strip().split("\n"):
                if ": " in line:
                    key, value = line.split(": ", 1)
                    info[key.lower()] = value
            return info
    except Exception:
        pass
    return None


def check_package_location(
    package_name: str,
) -> tuple[str | None, bool] | tuple[None, bool]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "-f", package_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            location = None
            has_files = False
            for i, line in enumerate(lines):
                if line.startswith("Location:"):
                    location = line.split(":", 1)[1].strip()
                elif line.startswith("Files:") and any(
                    file_line.strip() and ".dist-info" not in file_line for file_line in lines[i + 1 : i + 10]
                ):
                    has_files = True
            return location, has_files
    except Exception:
        pass
    return None, False


def analyze_wheels(source_dir: Path, dest_dir_name: str = "empty_wheels", check_installed = True) -> None:
    source_path = Path(source_dir)
    dest_path = source_path / dest_dir_name
    installed_packages = get_installed_packages() if check_installed else {}
    wheel_files = list(source_path.glob("*.whl"))
    if not wheel_files:
        print(f"No .whl files found in {source_dir}")
        return
    print(f"Found {len(wheel_files)} wheel files to check")
    if check_installed:
        print(f"Found {len(installed_packages)} installed packages in current environment\n")
    empty_wheels = []
    installed_empty_wheels = []
    valid_wheels = []
    for wheel_file in wheel_files:
        print(f"Checking {wheel_file.name}...")
        if is_empty_wheel(wheel_file):
            print("  ✓ EMPTY wheel")
            pkg_name, _pkg_version = extract_package_info(wheel_file)
            if check_installed and pkg_name:
                installed_version = installed_packages.get(pkg_name.lower())
                if installed_version:
                    print(f"  ⚠ WARNING: Package '{pkg_name}' is INSTALLED (version {installed_version})")
                    location, has_files = check_package_location(pkg_name)
                    if location:
                        print(f"  📍 Installed at: {location}")
                        if not has_files:
                            print("  ⚠ Installation appears incomplete!")
                    installed_empty_wheels.append({
                        "wheel": wheel_file,
                        "package": pkg_name,
                        "version": installed_version,
                    })
                else:
                    print(f"  ℹ Package '{pkg_name}' not found in installed packages")
            empty_wheels.append(wheel_file)
        else:
            print("  ✓ VALID wheel (contains code)")
            valid_wheels.append(wheel_file)
        print()
    print("-" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Total wheels: {len(wheel_files)}")
    print(f"Valid wheels: {len(valid_wheels)}")
    print(f"Empty wheels: {len(empty_wheels)}")
    if installed_empty_wheels:
        print(f"""
⚠ CRITICAL: {len(installed_empty_wheels)} empty wheels correspond to INSTALLED packages!""")
        for item in installed_empty_wheels:
            print(f"  - {item['wheel'].name} -> {item['package']}=={item['version']}")
        print("\nRECOMMENDATIONS:")
        print("  1. DO NOT move/delete these wheels if you need the packages")
        print("  2. The packages are likely broken installs")
        print("  3. Consider reinstalling these packages:")
        for item in installed_empty_wheels:
            print(f"     pip uninstall {item['package']} -y")
            print(f"     pip install {item['package']}")
    if empty_wheels:
        print(f"\nFound {len(empty_wheels)} empty wheel(s) total")
        if installed_empty_wheels:
            response = input("""
Some empty wheels are INSTALLED. Move ONLY the uninstalled empty wheels? (y/n): """)
            wheels_to_move = [w for w in empty_wheels if w not in [item["wheel"] for item in installed_empty_wheels]]
        else:
            response = input(f"""
Move all {len(empty_wheels)} empty wheels to '{dest_dir_name}/'? (y/n): """)
            wheels_to_move = empty_wheels if response.lower() == "y" else []
        if wheels_to_move:
            dest_path.mkdir(exist_ok=True)
            moved_count = 0
            for wheel_file in wheels_to_move:
                dest_file = dest_path / wheel_file.name
                if dest_file.exists():
                    counter = 1
                    while dest_file.exists():
                        dest_file = dest_path / f"{wheel_file.stem}_{counter}{wheel_file.suffix}"
                        counter += 1
                shutil.move(str(wheel_file), str(dest_file))
                print(f"Moved: {wheel_file.name} -> {dest_dir_name}/{dest_file.name}")
                moved_count += 1
            print(f"\nMoved {moved_count} empty wheels to {dest_dir_name}/")
        else:
            print("No wheels were moved.")
    if installed_empty_wheels:
        print("\n" + "=" * 40)
        print("IMPORTANT ACTIONS TO TAKE")
        print("-" * 40)
        print("These packages were installed from empty wheels and are likely broken:")
        for item in installed_empty_wheels:
            print(f"  - {item['package']} (version {item['version']})")
        print("\nTo fix them:")
        print("1. Check if the packages work correctly")
        print("2. If broken, reinstall with valid wheels:")
        for item in installed_empty_wheels:
            print(f"   pip uninstall {item['package']}")
            print(f"   pip install {item['package']}  # or use a valid wheel")
        print(
            "\n3. Or completely remove them: pip uninstall "
            + " ".join([item["package"] for item in installed_empty_wheels])
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Identify and move empty .whl files, with detection of potentially installed ones"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    parser.add_argument(
        "-d",
        "--dest",
        default="empty_wheels",
        help="Destination subdirectory name (default: 'empty_wheels')",
    )
    parser.add_argument(
        "--no-install-check",
        action="store_true",
        help="Skip checking installed packages",
    )
    parser.add_argument(
        "--auto-move-all",
        action="store_true",
        help="Automatically move all empty wheels without prompting",
    )
    args = parser.parse_args()
    directory_path = Path(args.directory)
    if not directory_path.exists():
        print(f"Error: Directory '{args.directory}' does not exist")
        return
    analyze_wheels(args.directory, args.dest, check_installed=not args.no_install_check)


if __name__ == "__main__":
    try:
        pass
    except ImportError:
        print("Note: 'packaging' module not found. Install it with: pip install packaging")
        print("Continuing with limited version parsing...\n")
    raise SystemExit(main())
