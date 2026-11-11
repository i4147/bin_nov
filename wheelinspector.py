#!/data/data/com.termux/files/usr/bin/env python
"""Unified tool for detecting and managing empty Python wheels."""

import argparse
import csv
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from typing import Any, Optional
import zipfile


WheelInfo = dict[str, object]
PackageInfo = dict[str, str]
DEFAULT_DEST: str = "empty_wheels"
DEFAULT_METHOD: str = "basic"
VALID_METHODS: set[str] = {"basic", "record", "ext"}
CODE_EXT: set[str] = {".py", ".so", ".pyi", ".pyd", ".pyx"}


def is_empty_wheel_basic(wheel_path: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            file_list = zf.namelist()
            has_python = any(f.endswith(".py") for f in file_list)
            has_non_dist_info = any(
                (not f.startswith(("dist-info/", "__pycache__/")))
                and (not f.endswith("/"))
                and (not f.endswith(".dist-info/"))
                for f in file_list
            )
            return not (has_python or has_non_dist_info)
    except zipfile.BadZipFile:
        print(f"Error: {wheel_path} is not a valid zip file")
        return False
    except Exception as e:
        print(f"Error reading {wheel_path}: {e}")
        return False


def is_empty_wheel_record(wheel_path: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            dist_info_dirs = [f for f in zf.namelist() if ".dist-info/" in f]
            if not dist_info_dirs:
                return False
            dist_info_dir = dist_info_dirs[0].split("/")[0] + "/"
            record_path = f"{dist_info_dir}RECORD"
            if record_path not in zf.namelist():
                return False
            with zf.open(record_path) as f:
                reader = csv.reader((line.decode("utf-8") for line in f))
                for row in reader:
                    if not row:
                        continue
                    file_path = row[0]
                    if not file_path.startswith(dist_info_dir):
                        return False
            return True
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return False
    except Exception as e:
        print(f"Error reading {wheel_path}: {e}")
        return False


def is_empty_wheel_ext(wheel_path: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            for file_name in zf.namelist():
                if Path(file_name).suffix.lower() in CODE_EXT:
                    return False
    except zipfile.BadZipFile:
        print(f"Warning: {wheel_path} is not a valid ZIP file. Skipping.")
        return False
    except Exception as e:
        print(f"Error reading {wheel_path}: {e}")
        return False
    return True


def is_empty_wheel(wheel_path: Path, method: str = DEFAULT_METHOD) -> bool:
    if method == "basic":
        return is_empty_wheel_basic(wheel_path)
    elif method == "record":
        return is_empty_wheel_record(wheel_path)
    elif method == "ext":
        return is_empty_wheel_ext(wheel_path)
    else:
        msg = f"Unknown method: {method}"
        raise ValueError(msg)


def parse_wheel_name(wheel_path: Path) -> tuple[Optional[str], Optional[str]]:
    stem = wheel_path.stem
    parts = stem.split("-")
    if len(parts) >= 2:
        package_name = parts[0].replace("_", "-")
        version = parts[1]
        return package_name, version
    return None, None


def get_installed_packages() -> dict[str, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
        packages = {}
        for line in result.stdout.strip().split("\n"):
            if "==" in line:
                name, version = line.split("==")
                packages[name.lower()] = version
        return packages
    except Exception as e:
        print(f"Warning: Could not get installed packages: {e}")
        return {}


def get_package_info(package_name: str) -> Optional[PackageInfo]:
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


def get_package_location(package_name: str) -> tuple[Optional[str], bool]:
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
                    (n.strip() and ".dist-info" not in n for n in lines[i + 1 : i + 10])
                ):
                    has_files = True
            return location, has_files
    except Exception:
        pass
    return None, False


def check_installed_package(wheel_path: Path, installed_packages: dict[str, str]) -> Optional[WheelInfo]:
    package_name, _version = parse_wheel_name(wheel_path)
    if not package_name:
        return None
    installed_version = installed_packages.get(package_name.lower())
    if installed_version:
        location, has_files = get_package_location(package_name)
        return {
            "wheel": wheel_path,
            "package": package_name,
            "version": installed_version,
            "location": location,
            "has_files": has_files,
        }
    return None


def find_wheels(directory: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*.whl" if recursive else "*.whl"
    return [f for f in directory.glob(pattern) if f.is_file()]


def move_wheel(wheel_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(exist_ok=True)
    dest_path = dest_dir / wheel_path.name
    counter = 1
    while dest_path.exists():
        dest_path = dest_dir / f"{wheel_path.stem}_{counter}{wheel_path.suffix}"
        counter += 1
    shutil.move(str(wheel_path), str(dest_path))
    return dest_path


def scan_site_packages() -> list[Path]:
    site_packages = Path(sysconfig.get_paths()["purelib"])
    empty_packages = []
    if not site_packages.is_dir():
        return empty_packages
    for dist_info in site_packages.iterdir():
        if dist_info.name.endswith(".dist-info") and dist_info.is_dir():
            record_file = dist_info / "RECORD"
            if not record_file.is_file():
                continue
            is_empty = True
            with record_file.open(newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    file_path = row[0]
                    resolved_path = (dist_info.parent / file_path).resolve()
                    if not str(resolved_path).startswith(str(dist_info.resolve()) + "/"):
                        is_empty = False
                        break
            if is_empty:
                empty_packages.append(dist_info)
    return empty_packages


def cmd_check(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    wheels = find_wheels(directory, args.recursive)
    if not wheels:
        print(f"No .whl files found in {directory}")
        return 0
    print(f"Found {len(wheels)} wheel files to check")
    empty_wheels = []
    valid_wheels = []
    for wheel in wheels:
        print(f"Checking {wheel.name}...", end=" ")
        if is_empty_wheel(wheel, args.method):
            print("EMPTY")
            empty_wheels.append(wheel)
        else:
            print("OK")
            valid_wheels.append(wheel)
    print(f"\nFound {len(empty_wheels)} empty wheel(s)")
    for wheel in empty_wheels:
        print(f"  {wheel.relative_to(directory)}")
    if args.verbose:
        print(f"\nValid wheels: {len(valid_wheels)}")
        for wheel in valid_wheels:
            print(f"  {wheel.relative_to(directory)}")
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    dest_dir = directory / args.dest
    wheels = find_wheels(directory, args.recursive)
    if not wheels:
        print(f"No .whl files found in {directory}")
        return 0
    print(f"Found {len(wheels)} wheel files to check")
    empty_wheels = []
    valid_wheels = []
    for wheel in wheels:
        print(f"Checking {wheel.name}...", end=" ")
        if is_empty_wheel(wheel, args.method):
            print("EMPTY")
            empty_wheels.append(wheel)
        else:
            print("OK")
            valid_wheels.append(wheel)
    if not empty_wheels:
        print("\nNo empty wheels found!")
        return 0
    print(f"\nFound {len(empty_wheels)} empty wheel(s)")
    dest_dir.mkdir(exist_ok=True)
    moved_count = 0
    for wheel in empty_wheels:
        new_path = move_wheel(wheel, dest_dir)
        print(f"Moved: {wheel.name} -> {args.dest}/{new_path.name}")
        moved_count += 1
    print(f"\nMoved {moved_count} empty wheels to {args.dest}/")
    print(f"Valid wheels remaining: {len(valid_wheels)}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    empty_packages = scan_site_packages()
    cwd_wheels = find_wheels(Path.cwd(), args.recursive)
    if empty_packages:
        print("\n=== Empty installed packages (site-packages) ===")
        for pkg in empty_packages:
            print(f"  {pkg}")
    else:
        print("\nNo empty installed packages found.")
    if cwd_wheels:
        print("\n=== Empty wheel files in current directory ===")
        for wheel in cwd_wheels:
            if is_empty_wheel(wheel, args.method):
                print(f"  {wheel}")
    else:
        print("\nNo wheel files found in current directory.")
    if not empty_packages and not cwd_wheels:
        print("\nNo empty packages or wheels found.")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    dest_dir = directory / args.dest
    wheels = find_wheels(directory, args.recursive)
    if not wheels:
        print(f"No .whl files found in {directory}")
        return 0
    print(f"Found {len(wheels)} wheel files to check")
    installed_packages = {}
    if args.check_installed:
        installed_packages = get_installed_packages()
        print(f"Found {len(installed_packages)} installed packages in current environment\n")
    empty_wheels = []
    installed_empty_wheels = []
    valid_wheels = []
    for wheel in wheels:
        print(f"Checking {wheel.name}...")
        if is_empty_wheel(wheel, args.method):
            print("  ✓ EMPTY wheel")
            if args.check_installed:
                pkg_info = check_installed_package(wheel, installed_packages)
                if pkg_info:
                    print(f"  ⚠ WARNING: Package '{pkg_info['package']}' is INSTALLED (version {pkg_info['version']})")
                    if pkg_info["location"]:
                        print(f"  📍 Installed at: {pkg_info['location']}")
                        if not pkg_info["has_files"]:
                            print("  ⚠ Installation appears incomplete!")
                    installed_empty_wheels.append(pkg_info)
                else:
                    print("  ℹ Package not found in installed packages")
            empty_wheels.append(wheel)
        else:
            print("  ✓ VALID wheel (contains code)")
            valid_wheels.append(wheel)
        print()
    print("-" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Total wheels: {len(wheels)}")
    print(f"Valid wheels: {len(valid_wheels)}")
    print(f"Empty wheels: {len(empty_wheels)}")
    if installed_empty_wheels:
        print(f"\n⚠ CRITICAL: {len(installed_empty_wheels)} empty wheels correspond to INSTALLED packages!")
        for info in installed_empty_wheels:
            print(f"  - {info['wheel'].name} -> {info['package']}=={info['version']}")
        print("\nRECOMMENDATIONS:")
        print("  1. DO NOT move/delete these wheels if you need the packages")
        print("  2. The packages are likely broken installs")
        print("  3. Consider reinstalling these packages:")
        for info in installed_empty_wheels:
            print(f"     pip uninstall {info['package']} -y")
            print(f"     pip install {info['package']}")
    if empty_wheels:
        print(f"\nFound {len(empty_wheels)} empty wheel(s) total")
        wheels_to_move = []
        if installed_empty_wheels and not args.auto_move_all:
            response = input("\nSome empty wheels are INSTALLED. Move ONLY the uninstalled empty wheels? (y/n): ")
            installed_wheels = {info["wheel"] for info in installed_empty_wheels}
            wheels_to_move = [w for w in empty_wheels if w not in installed_wheels] if response.lower() == "y" else []
        elif args.auto_move_all:
            wheels_to_move = empty_wheels
        else:
            response = input(f"\nMove all {len(empty_wheels)} empty wheels to '{args.dest}/'? (y/n): ")
            wheels_to_move = empty_wheels if response.lower() == "y" else []
        if wheels_to_move:
            dest_dir.mkdir(exist_ok=True)
            moved_count = 0
            for wheel in wheels_to_move:
                new_path = move_wheel(wheel, dest_dir)
                print(f"Moved: {wheel.name} -> {args.dest}/{new_path.name}")
                moved_count += 1
            print(f"\nMoved {moved_count} empty wheels to {args.dest}/")
        else:
            print("No wheels were moved.")
    if installed_empty_wheels:
        print("\n" + "=" * 40)
        print("IMPORTANT ACTIONS TO TAKE")
        print("-" * 40)
        print("These packages were installed from empty wheels and are likely broken:")
        for info in installed_empty_wheels:
            print(f"  - {info['package']} (version {info['version']})")
        print("\nTo fix them:")
        print("1. Check if the packages work correctly")
        print("2. If broken, reinstall with valid wheels:")
        for info in installed_empty_wheels:
            print(f"   pip uninstall {info['package']}")
            print(f"   pip install {info['package']}  # or use a valid wheel")
        print(
            "\n3. Or completely remove them: pip uninstall "
            + " ".join([info["package"] for info in installed_empty_wheels])
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wheel Inspector - Unified tool for detecting and managing empty Python wheels.",
        epilog="""\
Detection Methods:
  basic  - Check for .py files OR non-dist-info files (ewhl.py/ewhl2.py)
  record - Check RECORD file, all files must be in dist-info (emptypkg.py/emptywhl.py)
  ext    - Check for .py, .so, .pyi files (find_empty_wheels.py)
Examples:
  python wheel_inspector.py check
  python wheel_inspector.py move --method record --dest quarantine
  python wheel_inspector.py scan
  python wheel_inspector.py all --auto-move-all
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    check_parser = subparsers.add_parser("check", help="Check wheels and list empty ones (no moving)")
    check_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    check_parser.add_argument(
        "-m",
        "--method",
        choices=sorted(VALID_METHODS),
        default=DEFAULT_METHOD,
        help=f"Detection method (default: {DEFAULT_METHOD})",
    )
    check_parser.add_argument("-r", "--recursive", action="store_true", help="Search recursively for wheels")
    check_parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed output")
    check_parser.set_defaults(func=cmd_check)
    move_parser = subparsers.add_parser("move", help="Move empty wheels to destination directory")
    move_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    move_parser.add_argument(
        "-d",
        "--dest",
        default=DEFAULT_DEST,
        help=f"Destination subdirectory name (default: '{DEFAULT_DEST}')",
    )
    move_parser.add_argument(
        "-m",
        "--method",
        choices=sorted(VALID_METHODS),
        default=DEFAULT_METHOD,
        help=f"Detection method (default: {DEFAULT_METHOD})",
    )
    move_parser.add_argument("-r", "--recursive", action="store_true", help="Search recursively for wheels")
    move_parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed output")
    move_parser.set_defaults(func=cmd_move)
    scan_parser = subparsers.add_parser("scan", help="Scan site-packages for empty installed packages")
    scan_parser.add_argument(
        "-m",
        "--method",
        choices=sorted(VALID_METHODS),
        default=DEFAULT_METHOD,
        help=f"Detection method for wheel files (default: {DEFAULT_METHOD})",
    )
    scan_parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Search recursively for wheels in current directory",
    )
    scan_parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed output")
    scan_parser.set_defaults(func=cmd_scan)
    all_parser = subparsers.add_parser("all", help="Full workflow: check wheels, warn about installed, move empty ones")
    all_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing .whl files (default: current directory)",
    )
    all_parser.add_argument(
        "-d",
        "--dest",
        default=DEFAULT_DEST,
        help=f"Destination subdirectory name (default: '{DEFAULT_DEST}')",
    )
    all_parser.add_argument(
        "-m",
        "--method",
        choices=sorted(VALID_METHODS),
        default=DEFAULT_METHOD,
        help=f"Detection method (default: {DEFAULT_METHOD})",
    )
    all_parser.add_argument("-r", "--recursive", action="store_true", help="Search recursively for wheels")
    all_parser.add_argument(
        "--check-installed",
        dest="check_installed",
        action="store_true",
        default=True,
        help="Check installed packages (default: True)",
    )
    all_parser.add_argument(
        "--no-check-installed",
        dest="check_installed",
        action="store_false",
        help="Skip checking installed packages",
    )
    all_parser.add_argument(
        "--auto-move-all",
        action="store_true",
        help="Automatically move all empty wheels without prompting",
    )
    all_parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed output")
    all_parser.set_defaults(func=cmd_all)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    if hasattr(args, "directory"):
        directory = Path(args.directory)
        if not directory.exists():
            print(f"Error: Directory '{args.directory}' does not exist")
            return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
