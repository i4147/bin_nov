#!/data/data/com.termux/files/usr/bin/env python
"""Delete the outdated and the unwanted wheel files of the current directory.
Parses the ``name-version-python-tag.whl`` names, keeps only the newest dated build of each package and also removes the ``py3-none-any`` builds of the packages on the blocklist (pycryptodome, matplotlib), printing every deletion.
Usage: version_filter Examples: $ cd ~/wheels && version_filter Runs as a script (``python version_filter.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import re
from typing import Any, Iterable


WHL_DIRECTORY: Path = Path()
WHL_PATTERN = re.compile(
    r"(?P<name>[\w\-]+)"
    r"-(?P<version>[\d\.]+(?:-\d{8})?)"
    r"-(?P<python>"
    r"py3-none-any"
    r"|cp37-abi3-linux_armv8l"
    r"|cp312-cp312-linux_armv8l"
    r"|cp312-cp312-linux_arm"
    r"|py3-none-linux_armv8l"
    r")\.whl"
)
PY3_NONE_ANY_BLOCKLIST: frozenset[str] = frozenset({"pycryptodome", "matplotlib"})


def _parse_wheel(filename: str) -> tuple[str, str, str] | None:
    match = WHL_PATTERN.match(filename)
    if match is None:
        return None
    return match.group("name"), match.group("version"), match.group("python")


def _latest_dates(parsed: list[tuple[str, str, str, str]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for _, name, version, _ in parsed:
        if "-" not in version:
            continue
        date_part = version.rsplit("-", 1)[-1]
        if date_part > latest.get(name, ""):
            latest[name] = date_part
    return latest


def cleanup_wheels(whl_files: list[str], directory: Path = WHL_DIRECTORY) -> int:
    parsed: list[tuple[str, str, str, str]] = []
    for filename in whl_files:
        info = _parse_wheel(filename)
        if info is not None:
            parsed.append((filename, *info))
    latest_versions = _latest_dates(parsed)
    deleted = 0
    for filename, name, version, python_variant in parsed:
        blocked_py3 = name in PY3_NONE_ANY_BLOCKLIST and python_variant == "py3-none-any"
        outdated = "-" in version and version.rsplit("-", 1)[-1] != latest_versions.get(name)
        if not (blocked_py3 or outdated):
            continue
        (directory / filename).unlink()
        print(f"Deleted: {filename}")
        deleted += 1
    return deleted


def main() -> None:
    whl_files = [f for f in os.listdir(WHL_DIRECTORY) if f.endswith(".whl")]
    deleted = cleanup_wheels(whl_files)
    print(f"Number of files deleted: {deleted}")


if __name__ == "__main__":
    main()
