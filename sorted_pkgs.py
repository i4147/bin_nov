#!/data/data/com.termux/files/usr/bin/env python
"""List the installed packages by size in a table.
Queries dpkg for the installed packages, caches the result in ``installed.json`` (``-r`` refreshes it) and prints a width-adaptive table with human-readable sizes, optionally filtered to the biggest ones.
Usage: sorted_pkgs [-r] [N] Examples: $ sorted_pkgs 20 Runs as a script (``python sorted_pkgs.py``) in the directory it should act on; the module can also be imported."""

import json
from pathlib import Path
import shutil
import subprocess
import sys


DEFAULT_JSON: Path = Path("installed.json")


def human_size(kib: int) -> str:
    if kib >= 1024 * 1024:
        value, unit = kib / (1024 * 1024), "gb"
    elif kib >= 1024:
        value, unit = kib / 1024, "mb"
    else:
        value, unit = float(kib), "kb"
    return f"{int(value)}{unit}" if value == int(value) else f"{value:.1f}{unit}"


def get_installed() -> list[tuple[str, int]]:
    proc = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Status}\t${Package}\t${Installed-Size}\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    packages: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, name, size_raw = parts
        if status != "installed":
            continue
        try:
            kib = int(size_raw)
        except ValueError:
            continue
        if kib > 0:
            packages.append((name, kib))
    packages.sort(key=lambda item: item[1], reverse=True)
    return packages


def term_width() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def render(packages: list[tuple[str, int]], width: int) -> str:
    if not packages:
        return ""
    entries: list[tuple[str, str]] = [(n, human_size(kb)) for n, kb in packages]
    name_max = max(len(n) for n, _ in entries)
    size_max = max(len(s) for _, s in entries)
    name_width = name_max + 2
    if name_width + size_max > width:
        name_width = max(6, width - size_max)
    lines: list[str] = []
    for name, size in entries:
        if len(name) > name_width:
            name = name[: name_width - 2] + ".." if name_width >= 3 else name[:name_width]
        lines.append(f"{name:<{name_width}}{size}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    output_path: Path = Path(argv[1]) if len(argv) > 1 else DEFAULT_JSON
    packages = get_installed()
    payload = [
        {
            "package": name,
            "installed_kib": kib,
            "installed_human": human_size(kib),
        }
        for name, kib in packages
    ]
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(render(packages, term_width()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
