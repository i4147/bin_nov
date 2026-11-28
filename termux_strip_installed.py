#!/data/data/com.termux/files/usr/bin/env python
"""
Strip installed (dpkg) packages from a package list file, in place.

Usage:
    python strip_installed.py pkgs.txt

"""

from __future__ import annotations
from pathlib import Path
import subprocess
import sys

from dh import atomic_write


def get_installed_packages() -> set[str]:
    """Return the set of packages whose dpkg status is 'install ok installed'."""
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Status}\n"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print(
            "error: dpkg-query not found (is this a Termux/dpkg system?)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if proc.returncode != 0:
        print(f"error: dpkg-query failed: {proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)

    installed: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, _, status = line.partition("\t")
        if status.strip() == "install ok installed":
            installed.add(name.strip())
    return installed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} PKGS_FILE", file=sys.stderr)
        return 2

    path = Path(argv[1]).expanduser()
    if not path.is_file():
        print(f"error: not a regular file: {path}", file=sys.stderr)
        return 1

    installed = get_installed_packages()

    original = path.read_text(encoding="utf-8").splitlines()

    # Keep only non-empty lines whose package is NOT installed.
    remaining: list[str] = []
    removed: list[str] = []
    for line in original:
        name = line.strip()
        if not name:
            continue
        if name in installed:
            removed.append(name)
        else:
            remaining.append(name)

    atomic_write(
        path,
        "\n".join(remaining) + ("\n" if remaining else ""),
        encoding="utf-8",
    )

    print(f"removed {len(removed)} installed package(s), kept {len(remaining)} / {len(original)} line(s)")
    if removed:
        print("removed:", ", ".join(removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
