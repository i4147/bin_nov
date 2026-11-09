#!/data/data/com.termux/files/usr/bin/env python
"""Report Python packages installed both system-wide and in the user site directory.
Scans every ``site-packages`` directory known to :mod:`site`, matches packages by name across the system and user locations and prints the version of each copy, flagging version mismatches.
Scanning of the two sets runs in parallel threads.
Usage: check_duplicate_packages Examples: $ check_duplicate_packages Runs as a script (``python check_duplicate_packages.py``) in the directory it should act on; the module can also be imported."""

import concurrent.futures
import pathlib
import site
import sys
from typing import Any, Iterable


def get_site_directories() -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    site_dirs = [pathlib.Path(p) for p in site.getsitepackages()]
    user_site = pathlib.Path(site.getusersitepackages())
    system_site_dirs = [d for d in site_dirs if d != user_site]
    user_site_dirs = [user_site] if user_site.exists() else []
    return system_site_dirs, user_site_dirs


def scan_directory_for_packages(directory: pathlib.Path) -> dict[str, pathlib.Path]:
    packages = {}
    if not directory.exists():
        return packages
    try:
        for item in directory.iterdir():
            if item.is_dir():
                if (item / "__init__.py").exists():
                    packages[item.name] = item
                elif item.suffix in {".dist-info", ".egg-info"}:
                    pkg_name = item.name.split("-")[0]
                    packages[pkg_name] = item
            elif item.is_file():
                if item.suffix == ".py":
                    packages[item.stem] = item
                elif (item.suffix in {".dist-info", ".egg-info"}) and item.is_dir():
                    pkg_name = item.name.split("-")[0]
                    packages[pkg_name] = item
    except PermissionError:
        print(f"Warning: Permission denied accessing {directory}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Error scanning {directory}: {e}", file=sys.stderr)
    return packages


def find_duplicate_packages(
    system_packages: dict[str, pathlib.Path], user_packages: dict[str, pathlib.Path]
) -> dict[str, tuple[pathlib.Path, pathlib.Path]]:
    duplicates = {}
    common_packages = set(system_packages.keys()) & set(user_packages.keys())
    for pkg_name in sorted(common_packages):
        duplicates[pkg_name] = (system_packages[pkg_name], user_packages[pkg_name])
    return duplicates


def process_system_directories(
    system_dirs: list[pathlib.Path],
) -> dict[str, pathlib.Path]:
    system_packages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(system_dirs) or 1) as executor:
        future_to_dir = {
            executor.submit(scan_directory_for_packages, directory): directory for directory in system_dirs
        }
        for future in concurrent.futures.as_completed(future_to_dir):
            directory = future_to_dir[future]
            try:
                packages = future.result()
                system_packages.update(packages)
            except Exception as e:
                print(f"Error processing {directory}: {e}", file=sys.stderr)
    return system_packages


def process_user_directories(user_dirs: list[pathlib.Path]) -> dict[str, pathlib.Path]:
    user_packages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(user_dirs) or 1) as executor:
        future_to_dir = {executor.submit(scan_directory_for_packages, directory): directory for directory in user_dirs}
        for future in concurrent.futures.as_completed(future_to_dir):
            directory = future_to_dir[future]
            try:
                packages = future.result()
                user_packages.update(packages)
            except Exception as e:
                print(f"Error processing {directory}: {e}", file=sys.stderr)
    return user_packages


def analyze_package_versions(
    package_name: str, system_location: pathlib.Path, user_location: pathlib.Path
) -> dict[str, str]:
    versions = {"system_version": "unknown", "user_version": "unknown"}
    for location_type, location in [
        ("system", system_location),
        ("user", user_location),
    ]:
        try:
            if location.suffix in [".dist-info", ".egg-info"]:
                metadata_files = ["METADATA", "PKG-INFO"]
                for metadata_file in metadata_files:
                    metadata_path = location / metadata_file
                    if metadata_path.exists():
                        with open(metadata_path) as f:
                            for line in f:
                                if line.startswith("Version:"):
                                    version_key = f"{location_type}_version"
                                    versions[version_key] = line.split(":", 1)[1].strip()
                                    break
            else:
                parent_dir = location.parent if location.is_file() else location
                dist_info_dirs = list(parent_dir.glob(f"{package_name}-*.dist-info"))
                if not dist_info_dirs:
                    dist_info_dirs = list(parent_dir.glob(f"{package_name}-*.egg-info"))
                for dist_dir in dist_info_dirs[:1]:
                    metadata_path = dist_dir / "METADATA"
                    if not metadata_path.exists():
                        metadata_path = dist_dir / "PKG-INFO"
                    if metadata_path.exists():
                        with open(metadata_path) as f:
                            for line in f:
                                if line.startswith("Version:"):
                                    version_key = f"{location_type}_version"
                                    versions[version_key] = line.split(":", 1)[1].strip()
                                    break
        except Exception:
            pass
    return versions


def main() -> None:
    print("Python Package Duplicate Checker")
    print("-" * 40)
    try:
        system_dirs, user_dirs = get_site_directories()
    except Exception as e:
        print(f"Error getting site directories: {e}", file=sys.stderr)
        sys.exit(1)
    print("\nSystem site directories:")
    for d in system_dirs:
        print(f"  - {d}")
    print("\nUser site directory:")
    for d in user_dirs:
        print(f"  - {d}")
    if not user_dirs:
        print("\nNo user site-packages directory found.")
        return
    print("\nScanning for packages...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        system_future = executor.submit(process_system_directories, system_dirs)
        user_future = executor.submit(process_user_directories, user_dirs)
        system_packages = system_future.result()
        user_packages = user_future.result()
    duplicates = find_duplicate_packages(system_packages, user_packages)
    print("\nResults:")
    print(f"  System packages found: {len(system_packages)}")
    print(f"  User packages found: {len(user_packages)}")
    print(f"  Duplicate packages: {len(duplicates)}")
    if duplicates:
        print("\n" + "=" * 40)
        print("Packages installed in BOTH system and user directories:")
        print("-" * 40)
        for pkg_name, (system_loc, user_loc) in duplicates.items():
            versions = analyze_package_versions(pkg_name, system_loc, user_loc)
            print(f"\n📦 {pkg_name}")
            print(f"   System:  {system_loc}")
            if versions["system_version"] != "unknown":
                print(f"            Version: {versions['system_version']}")
            print(f"   User:    {user_loc}")
            if versions["user_version"] != "unknown":
                print(f"            Version: {versions['user_version']}")
            if (versions["system_version"] != "unknown" and versions["user_version"] != "unknown") and versions[
                "system_version"
            ] != versions["user_version"]:
                print("   ⚠️  Version mismatch!")
    else:
        print("\n✅ No duplicate packages found.")
    print("\n" + "=" * 40)
    print("Note: Having packages in both locations can lead to confusion about")
    print("which version is being used. Consider removing user installations of")
    print("packages that are already available system-wide.")


if __name__ == "__main__":
    raise SystemExit(main())
