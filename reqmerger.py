#!/data/data/com.termux/files/usr/bin/env python
"""Merge the ``requirements.txt`` files of the top-level subdirectories.
Reads every ``<subdir>/requirements.txt``, merges the lines without duplicates into the root ``requirements.txt`` and with ``-r`` deletes the originals.
Usage: reqmerger [-r] Options: -r, --remove Remove original requirements.txt files after merging.
Examples: $ reqmerger -r Command-line script: run ``python reqmerger.py`` or the ``reqmerger`` command.
The module is importable as well."""

import argparse
from pathlib import Path


def merge_requirements(root: Path, remove_originals: bool = False) -> Path:
    output = root / "requirements.txt"
    sources = sorted(p for p in root.iterdir() if p.is_dir() and (p / "requirements.txt").is_file())
    if not sources:
        print("No requirements.txt found in top-level subdirectories.")
        return output
    lines: list[str] = []
    seen: set[str] = set()
    for subdir in sources:
        req_file = subdir / "requirements.txt"
        lines.append(f"# --- {subdir.name}/requirements.txt ---")
        for raw in req_file.read_text().splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(raw)
                continue
            if stripped in seen:
                lines.append(f"# (duplicate) {raw}")
            else:
                seen.add(stripped)
                lines.append(raw)
        lines.append("")
    output.write_text("\n".join(lines).rstrip() + "\n")
    print(f"Wrote {output}")
    if remove_originals:
        for subdir in sources:
            (subdir / "requirements.txt").unlink()
            print(f"Removed {subdir / 'requirements.txt'}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge requirements.txt from top-level subdirs.")
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove original requirements.txt files after merging.",
    )
    args = parser.parse_args()
    merge_requirements(Path.cwd(), remove_originals=args.remove)


if __name__ == "__main__":
    main()
