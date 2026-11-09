#!/data/data/com.termux/files/usr/bin/env python
"""Sort .whl files in a directory into `installed/` and `not_installed/`.
A wheel is considered "installed" if a package with the same (canonical) name is present in the current Python environment, regardless of version."""

from importlib import metadata
from pathlib import Path
import re
import shutil
import sys


try:
    from packaging.utils import canonicalize_name, parse_wheel_filename
except ImportError:  # packaging is optional, fall back to manual parsing
    canonicalize_name = parse_wheel_filename = None


def canon(name: str) -> str:
    """PEP 503 canonical form of a distribution name."""
    if canonicalize_name is not None:
        return canonicalize_name(name)
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_name(path: Path):
    """Return the canonical package name parsed from a wheel filename."""
    if parse_wheel_filename is not None:
        try:
            name, _version, _build, _tags = parse_wheel_filename(path.name)
            return canon(name)
        except Exception:
            pass

    # Fallback: <name>-<version>(-<build>)?-<py>-<abi>-<plat>.whl
    parts = path.stem.split("-")
    if len(parts) < 5:
        return None
    return canon(parts[0])


def is_installed(name: str) -> bool:
    try:
        metadata.version(name)  # any version counts
        return True
    except metadata.PackageNotFoundError:
        return False


def main(directory: str = ".") -> None:
    root = Path(directory).resolve()
    installed_dir = root / "installed"
    not_installed_dir = root / "not_installed"

    wheels = sorted(root.glob("*.whl"))
    if not wheels:
        print("No .whl files found.")
        return

    installed_dir.mkdir(exist_ok=True)
    not_installed_dir.mkdir(exist_ok=True)

    for whl in wheels:
        name = wheel_name(whl)

        if name is None:
            print(f"[?] {whl.name}: could not parse filename -> not_installed")
            dest = not_installed_dir
        elif is_installed(name):
            print(f"[+] {whl.name}: {name} is installed")
            dest = installed_dir
        else:
            print(f"[-] {whl.name}: {name} is not installed")
            dest = not_installed_dir

        shutil.move(str(whl), str(dest / whl.name))


if __name__ == "__main__":
    main(*sys.argv[1:])
