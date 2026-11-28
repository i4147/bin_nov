#!/data/data/com.termux/files/usr/bin/env python
"""List the .whl files in the current directory and flag entry points.

Prints each wheel highlighted when it declares console entry points, and can folderize copies into with/without (and pure/nonpure) directories via CLI flags.
"""

from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import zipfile

from dh import cprint


HIGHLIGHT: tuple[int, int, int] = (206, 236, 132)
DIM: tuple[int, int, int] = (
    90,
    90,
    90,
)  # adjust to whatever "dim" should look like in your terminal


def has_entry_point(whl_path: Path) -> bool:
    with zipfile.ZipFile(whl_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".dist-info/entry_points.txt"):
                continue
            text = zf.read(name).decode("utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip()
                # skip blanks, comments, and group headers like [console_scripts]
                if not line or line.startswith(("#", "[")) or "=" not in line:
                    continue
                return True
    return False


def is_pure(whl_path: Path) -> bool:
    # Wheel filename: name-version[-build]-pytag-abitag-plattag.whl
    # Pure Python wheels have abi tag "none" and platform tag "any".
    abi_tag, plat_tag = whl_path.stem.split("-")[-2:]
    return abi_tag == "none" and plat_tag == "any"


def parse_args():
    parser = argparse.ArgumentParser(description="List and sort wheels.")
    parser.add_argument(
        "-f",
        "--folderize",
        action="store_true",
        help="move wheels into 'with/' and 'without/' folders based on entry points",
    )
    parser.add_argument(
        "-p",
        "--pure",
        action="store_true",
        help="move wheels into 'pure/' and 'nonpure/' folders",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    wheels = sorted(Path().glob("*.whl"))
    if not wheels:
        cprint("No .whl files found in the current directory.", color=DIM)
        return

    for whl in wheels:
        try:
            matched = has_entry_point(whl)
            pure = is_pure(whl)
        except zipfile.BadZipFile:
            cprint(f"{whl.name} (invalid wheel)", color=DIM)
            continue

        color = HIGHLIGHT if matched else DIM
        cprint(whl.name, color=color)

        if not (args.folderize or args.pure):
            continue

        target = Path()
        if args.folderize:
            target = target / ("with" if matched else "without")
        if args.pure:
            target = target / ("pure" if pure else "nonpure")

        target.mkdir(parents=True, exist_ok=True)
        shutil.move(str(whl), str(target / whl.name))


if __name__ == "__main__":
    main()
