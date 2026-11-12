#!/data/data/com.termux/files/usr/bin/env python
"""Reduce a ``pip freeze`` file to the bare package names.
Drops the version pins, the comments, the URLs and the VCS requirements, deduplicates what is left and overwrites the file with one name per line.
Usage: clean_version requirements.txt Positional arguments: file Pip freeze output file Examples: $ clean_version freeze.txt Command-line script: run ``python clean_version.py`` or the ``clean_version`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re

PKG_NAME_RE = re.compile(
    r"""
    ^\s*
    (?:
        -e\s+
    )?
    (?P<name>[A-Za-z0-9_.\-]+)
    """,
    re.VERBOSE,
)


def extract_package_name(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith(("git+", "http://", "https://")):
        return None
    if "@" in line:
        name = line.split("@", 1)[0].strip()
        return name or None
    match = PKG_NAME_RE.match(line)
    if match:
        return match.group("name")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean pip freeze output and keep only package names (overwrite file)."
    )
    parser.add_argument("file", help="pip freeze output file")
    args = parser.parse_args()
    path = Path(args.file)
    if not path.is_file():
        msg = f"Not a file: {path}"
        raise SystemExit(msg)
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    packages = []
    for line in lines:
        name = extract_package_name(line)
        if name:
            packages.append(name)
    seen = set()
    cleaned = [p for p in packages if not (p in seen or seen.add(p))]
    path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
