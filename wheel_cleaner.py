#!/data/data/com.termux/files/usr/bin/env python
"""Detect and handle empty wheels and metadata-only installed packages."""

import argparse
import csv
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import textwrap
from typing import Any, Callable, Iterable, Optional, Sequence
import zipfile

__all__: list[str] = ["main"]
DEFAULT_DEST: str = "empty_wheels"
DEFAULT_DETECTOR: str = "dist-info-only"
_CODE_SUFFIXES: tuple[str, ...] = (".py", ".so", ".pyi")


def _dist_info_prefix(names: Sequence[str]) -> Optional[str]:
    for name in names:
        if name.endswith(".dist-info/"):
            return name
    for name in names:
        if ".dist-info/" in name:
            return name.split("/")[0] + "/"
    return None


def _detect_dist_info_only(zf: zipfile.ZipFile, names: Sequence[str]) -> bool:
    prefix = _dist_info_prefix(names)
    if prefix is None:
        return False
    return all(name.endswith("/") or name.startswith(prefix) for name in names)


def _detect_record(zf: zipfile.ZipFile, names: Sequence[str]) -> bool:
    prefix = _dist_info_prefix(names)
    if prefix is None:
        return False
    record = f"{prefix}RECORD"
    if record not in names:
        return False
    try:
        with zf.open(record) as handle:
            reader = csv.reader(line.decode("utf-8") for line in handle)
            for row in reader:
                if not row:
                    continue
                if not row[0].startswith(prefix):
                    return False
    except (KeyError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
        return False
    return True


def _detect_no_code(zf: zipfile.ZipFile, names: Sequence[str]) -> bool:
    return not any(name.lower().endswith(_CODE_SUFFIXES) for name in names)


def _detect_no_payload(zf: zipfile.ZipFile, names: Sequence[str]) -> bool:
    has_py = any(name.endswith(".py") for name in names)
    has_other = any(
        not name.startswith(("dist-info/", "__pycache__/"))
        and not name.endswith("/")
        and not name.endswith(".dist-info/")
        for name in names
    )
    return not (has_py or has_other)


DetectorFn = Callable[[zipfile.ZipFile, Sequence[str]], bool]
DETECTORS: dict[str, DetectorFn] = {
    "dist-info-only": _detect_dist_info_only,
    "record": _detect_record,
    "no-code": _detect_no_code,
    "no-payload": _detect_no_payload,
}
DETECTOR_HELP: str = """\
detectors:
  dist-info-only  every entry lives under a single *.dist-info/ directory (default)
  record          every RECORD row points inside the *.dist-info/ directory
  no-code         the archive contains no .py / .so / .pyi file
  no-payload      literal ewhl.py logic (no .py file and no extra file)
  all             union of every detector above
"""


def resolve_detectors(raw: Optional[Iterable[str]]) -> list[str]:
    if not raw:
        return [DEFAULT_DETECTOR]
    chosen: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part in ("all", "any"):
                for name in DETECTORS:
                    if name not in chosen:
                        chosen.append(name)
            elif part in DETECTORS:
                if part not in chosen:
                    chosen.append(part)
            else:
                msg = f"Error: unknown detector {part!r}. Choose from: {', '.join(DETECTORS)} (or 'all')"
                raise SystemExit(msg)
    return chosen or [DEFAULT_DETECTOR]


def wheel_is_empty(path: Path, detectors: Sequence[str], quiet: bool = False) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            return any(DETECTORS[det](zf, names) for det in detectors)
    except zipfile.BadZipFile:
        if not quiet:
            print(f"  Warning: {path} is not a valid zip file", file=sys.stderr)
        return False
    except OSError as exc:
        if not quiet:
            print(f"  Warning: cannot read {path}: {exc}", file=sys.stderr)
        return False


def find_wheels(directory: Path, recursive: bool, exclude: Optional[Path] = None) -> list[Path]:
    candidates = directory.rglob("*.whl") if recursive else directory.glob("*.whl")
    result: list[Path] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if exclude is not None:
            try:
                candidate.relative_to(exclude)
                continue
            except ValueError:
                pass
        result.append(candidate)
    return sorted(result)


def unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def parse_wheel_name(path: Path) -> tuple[Optional[str], Optional[str]]:
    parts = path.stem.split("-")
    if len(parts) >= 2:
        return parts[0].replace("_", "-"), parts[1]
    return None, None


def default_site_packages() -> Path:
    return Path(sysconfig.get_paths()["purelib"])


def installed_packages() -> dict[str, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as exc:
        print(f"Warning: could not list installed packages: {exc}", file=sys.stderr)
        return {}
    packages: dict[str, str] = {}
    for line in proc.stdout.strip().splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            packages[name.lower()] = version
    return packages


def pip_show(name: str) -> Optional[dict[str, str]]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show", name],
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    info: dict[str, str] = {}
    for line in proc.stdout.strip().splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            info[key.lower()] = value
    return info


def pip_show_location_and_files(name: str) -> tuple[Optional[str], bool]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show", "-f", name],
            capture_output=True,
            text=True,
        )
    except Exception:
        return None, False
    if proc.returncode != 0:
        return None, False
    lines = proc.stdout.strip().split("\n")
    location: Optional[str] = None
    has_files = False
    for index, line in enumerate(lines):
        if line.startswith("Location:"):
            location = line.split(":", 1)[1].strip()
        elif line.startswith("Files:"):
            window = lines[index + 1 : index + 10]
            if any(entry.strip() and ".dist-info" not in entry for entry in window):
                has_files = True
    return location, has_files


def is_empty_dist_info(dist_info: Path) -> bool:
    record = dist_info / "RECORD"
    if not record.is_file():
        return False
    root = dist_info.resolve()
    try:
        with record.open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                try:
                    target = (dist_info.parent / row[0]).resolve()
                except OSError:
                    return False
                try:
                    target.relative_to(root)
                except ValueError:
                    return False
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return True


def find_empty_installed_packages(site_packages: Path) -> list[str]:
    found: list[str] = []
    if not site_packages.is_dir():
        return found
    for entry in sorted(site_packages.iterdir()):
        if entry.name.endswith(".dist-info") and entry.is_dir() and is_empty_dist_info(entry):
            found.append(str(entry))
    return found


def cmd_wheels(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: directory '{args.directory}' does not exist", file=sys.stderr)
        return 2
    detectors = resolve_detectors(args.detect)
    dest_dir = directory / args.dest
    wheels = find_wheels(directory, args.recursive, exclude=dest_dir)
    if not wheels:
        print(f"No .whl files found in {directory}")
        return 0
    print(f"Found {len(wheels)} wheel file(s) to check (detectors: {', '.join(detectors)})")
    installed: dict[str, str] = {}
    if args.check_installed:
        installed = installed_packages()
        print(f"Found {len(installed)} installed package(s) in the current environment")
    empty: list[Path] = []
    valid: list[Path] = []
    conflicts: list[tuple[Path, str, str]] = []
    for wheel in wheels:
        if wheel_is_empty(wheel, detectors, quiet=args.quiet):
            print(f"Checking {wheel.name} ... EMPTY")
            empty.append(wheel)
        else:
            print(f"Checking {wheel.name} ... OK")
            valid.append(wheel)
            continue
        if not args.check_installed:
            continue
        package, _ = parse_wheel_name(wheel)
        if not package:
            continue
        version = installed.get(package.lower())
        if version is None:
            if args.verbose:
                print(f"  info: package '{package}' is not installed")
            continue
        print(f"  WARNING: package '{package}' is INSTALLED (version {version})")
        location, has_files = pip_show_location_and_files(package)
        if location:
            print(f"  installed at: {location}")
            if not has_files:
                print("  installation appears incomplete!")
        conflicts.append((wheel, package, version))
    print("-" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Total wheels : {len(wheels)}")
    print(f"Valid wheels : {len(valid)}")
    print(f"Empty wheels : {len(empty)}")
    if conflicts:
        print(f"\nCRITICAL: {len(conflicts)} empty wheel(s) correspond to INSTALLED packages!")
        for wheel, package, version in conflicts:
            print(f"  - {wheel.name} -> {package}=={version}")
        print("\nRECOMMENDATIONS:")
        print("  1. DO NOT move/delete these wheels if you still need the packages")
        print("  2. The packages are likely broken installs")
        print("  3. Consider reinstalling them:")
        for _, package, _ in conflicts:
            print(f"     {sys.executable} -m pip uninstall {package} -y")
            print(f"     {sys.executable} -m pip install {package}")
    if not empty:
        print("\nNo empty wheels found!")
        return 0
    if args.dry_run or not args.move:
        print(f"\nFound {len(empty)} empty wheel(s) (report only -- pass --move to relocate them)")
        for wheel in empty:
            print(f"  {wheel.name}")
        return 0
    installed_paths = {wheel for wheel, _, _ in conflicts}
    moveable = [w for w in empty if w not in installed_paths]
    if not args.assume_yes:
        if conflicts:
            question = (
                f"\n{len(conflicts)} empty wheel(s) belong to INSTALLED packages. "
                f"Move only the remaining {len(moveable)} uninstalled empty wheel(s)? (y/n): "
            )
        else:
            question = f"\nMove {len(empty)} empty wheel(s) to '{args.dest}/'? (y/n): "
        answer = input(question).strip().lower()
        if answer not in ("y", "yes"):
            print("No wheels were moved.")
            return 0
    if not moveable:
        print("Nothing to move (every empty wheel belongs to an installed package).")
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for wheel in moveable:
        target = unique_path(dest_dir, wheel.name)
        shutil.move(str(wheel), str(target))
        print(f"Moved: {wheel.name} -> {args.dest}/{target.name}")
        moved += 1
    print(f"\nMoved {moved} empty wheel(s) to {dest_dir}")
    print(f"Valid wheels remaining: {len(valid)}")
    if conflicts:
        print("\n" + "=" * 40)
        print("IMPORTANT ACTIONS TO TAKE")
        print("-" * 40)
        print("These packages were installed from empty wheels and are likely broken:")
        for _, package, version in conflicts:
            print(f"  - {package} (version {version})")
        print("\nTo fix them:")
        for _, package, _ in conflicts:
            print(f"   {sys.executable} -m pip uninstall {package}")
            print(f"   {sys.executable} -m pip install {package}  # or use a valid wheel")
    return 0


def cmd_installed(args: argparse.Namespace) -> int:
    site_packages = Path(args.path) if args.path else default_site_packages()
    if not site_packages.is_dir():
        print(
            f"Error: site-packages directory '{site_packages}' does not exist",
            file=sys.stderr,
        )
        return 2
    found = find_empty_installed_packages(site_packages)
    if not found:
        print(f"No empty installed packages found in {site_packages}.")
        return 0
    print(f"=== Empty installed packages ({site_packages}) ===")
    for path in found:
        print(f"  {path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: directory '{args.directory}' does not exist", file=sys.stderr)
        return 2
    site_packages = Path(args.path) if args.path else default_site_packages()
    detectors = resolve_detectors(args.detect)
    empty_packages = find_empty_installed_packages(site_packages) if site_packages.is_dir() else []
    empty_wheels = [
        wheel for wheel in find_wheels(directory, args.recursive) if wheel_is_empty(wheel, detectors, quiet=args.quiet)
    ]
    if empty_packages:
        print("\n=== Empty installed packages (site-packages) ===")
        for path in empty_packages:
            print(f"  {path}")
    else:
        print("\nNo empty installed packages found.")
    if empty_wheels:
        print("\n=== Empty wheel files ===")
        for wheel in empty_wheels:
            print(f"  {wheel}")
    else:
        print("\nNo empty wheel files found.")
    if not empty_packages and not empty_wheels:
        print("\nNo empty packages or wheels found.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wheel_cleaner.py",
        description=(
            "Detect (and optionally move) 'empty' Python wheels -- wheels that "
            "contain only *.dist-info metadata and no importable code -- and "
            "find broken installations in site-packages."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            {DETECTOR_HELP}
            original script mapping:
              ewhl.py               ->  wheels . --move -y
              ewhl2.py              ->  wheels . --move --check-installed
              emptypkg.py           ->  scan .
              emptywhl.py           ->  wheels --detect record --move -y
              find_empty_wheels.py  ->  wheels . -r --detect no-code --move -y
            """),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{wheels,installed,scan}",
    )
    wheels = subparsers.add_parser(
        "wheels",
        help="scan a directory for empty .whl files",
        description="Scan a directory for empty .whl files and optionally move them.",
    )
    wheels.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory containing .whl files (default: current directory)",
    )
    wheels.add_argument(
        "-d",
        "--dest",
        default=DEFAULT_DEST,
        help=f"destination subdirectory name (default: {DEFAULT_DEST!r})",
    )
    wheels.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="search subdirectories recursively (find_empty_wheels.py behaviour)",
    )
    wheels.add_argument(
        "--detect",
        action="append",
        metavar="NAME",
        help="empty-wheel detector(s); repeatable or comma separated "
        f"({', '.join(DETECTORS)}, or 'all'). Default: {DEFAULT_DETECTOR}",
    )
    wheels.add_argument(
        "--move",
        action="store_true",
        help="move the empty wheels into --dest (default: report only)",
    )
    wheels.add_argument(
        "-y",
        "--yes",
        "--auto-move-all",
        dest="assume_yes",
        action="store_true",
        help="do not prompt before moving (ewhl2.py --auto-move-all / ewhl.py behaviour)",
    )
    wheels.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly report only, never move anything",
    )
    wheels.add_argument(
        "--check-installed",
        action="store_true",
        help="cross-check empty wheels against the packages installed in the current environment (ewhl2.py behaviour)",
    )
    wheels.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show detailed per-wheel output",
    )
    wheels.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress warnings about unreadable/invalid wheel files",
    )
    wheels.set_defaults(func=cmd_wheels)
    installed = subparsers.add_parser(
        "installed",
        help="find empty *.dist-info directories in site-packages",
        description="Find installed packages that consist of metadata only.",
    )
    installed.add_argument(
        "--path",
        default=None,
        help="site-packages directory to inspect (default: sysconfig purelib)",
    )
    installed.set_defaults(func=cmd_installed)
    scan = subparsers.add_parser(
        "scan",
        help="report empty installed packages AND empty wheels (emptypkg.py)",
        description="Combined report: empty site-packages entries and empty wheels.",
    )
    scan.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory containing .whl files (default: current directory)",
    )
    scan.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="search subdirectories recursively",
    )
    scan.add_argument(
        "--path",
        default=None,
        help="site-packages directory to inspect (default: sysconfig purelib)",
    )
    scan.add_argument(
        "--detect",
        action="append",
        metavar="NAME",
        help="empty-wheel detector(s); repeatable or comma separated "
        f"({', '.join(DETECTORS)}, or 'all'). Default: {DEFAULT_DETECTOR}",
    )
    scan.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress warnings about unreadable/invalid wheel files",
    )
    scan.set_defaults(func=cmd_scan)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
