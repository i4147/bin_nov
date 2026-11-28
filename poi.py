#!/data/data/com.termux/files/usr/bin/env python
"""Install all wheels in one pip invocation and remove them afterwards.

Expands the given paths (directories or .whl files) into a sorted list, runs a single pip install without dependencies, then deletes the installed archives.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def collect_wheels(targets: list[str]) -> list[Path]:
    """Expand the given paths into a sorted, de-duplicated list of wheel files."""
    wheels: set[Path] = set()
    for target in targets:
        path = Path(target)
        if path.is_dir():
            wheels.update(p.resolve() for p in path.glob("*.whl"))
        elif path.is_file() and path.suffix == ".whl":
            wheels.add(path.resolve())
        else:
            print(f"[!] skipping (not a wheel or directory): {target}", file=sys.stderr)
    return sorted(wheels)


def install_wheels(wheels: list[Path]) -> bool:
    """Install all wheels in one pip invocation. Returns True on success."""
    print(f"[+] installing {len(wheels)} package(s):")
    for wheel in wheels:
        print(f"    - {wheel.name}")

    cmd: list[str] = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--force-reinstall",
        *(str(wheel) for wheel in wheels),
    ]
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install wheel files without dependencies and remove them afterwards."
    )
    parser.add_argument(
        "targets",
        nargs="+",
        help="wheel files or directories containing wheel files",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep wheel files after a successful install",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wheels = collect_wheels(args.targets)

    if not wheels:
        print("[!] no wheel files found", file=sys.stderr)
        return 1

    if not install_wheels(wheels):
        print("[!] pip install failed; no wheel files were removed", file=sys.stderr)
        return 1

    if not args.keep:
        for wheel in wheels:
            wheel.unlink()
        print(f"[+] removed {len(wheels)} wheel file(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
