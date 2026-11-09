#!/data/data/com.termux/files/usr/bin/env python
"""Trim ``.dist-info`` directories down to the standard metadata files.
Keeps ``METADATA``, ``RECORD``, ``WHEEL`` and ``top_level.txt`` and removes the extra files that packagers leave behind (``INSTALLER``, ``REQUESTED``, ``direct_url.json``, license and author files, ``LICENSE`` copies ...).
Usage: distinfo2 [paths ...] Examples: $ distinfo2 site-packages/ Runs as a script (``python distinfo2.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil
import sys

from dh import cprint


major, minor, _, _, _ = sys.version_info
py_version: str = f"{major}.{minor}"
ALLOWED: list[str] = ["METADATA", "RECORD", "WHEEL", "top_level.txt"]
NOT_ALLOWED: list[str] = [
    "REQUESTED",
    "INSTALLER",
    "direct_url.json",
    "AUTHORS",
    "AUTHORS.md",
    "AUTHORS.rst",
    "AUTHORS.txt",
    "BSD-0-Clause.rst",
    "BSD-2-Clause.rst",
    "CONTRIBUTORS.txt",
    "COPYING",
    "COPYING.GPL",
    "COPYING.LESSER",
    "COPYING.LGPL",
    "COPYING.MPL",
    "COPYING.rst",
    "COPYING.txt",
    "DESCRIPTION.rst",
    "LICENCE",
    "LICENCE.rst",
    "LICENSE",
    "LICENSE-APACHE",
    "LICENSE.APACHE2",
    "LICENSE.markdown-it",
    "LICENSE.md",
    "LICENSE.rst",
    "LICENSE.txt",
    "LICENSE_numpy.txt",
    "LICENSE_scipy.txt",
    "NOTICE",
    "NOTICE.txt",
    "gpl-3-0.txt",
    "pbr.json",
    "toplevel.txt",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
]


def process_lic(path: Path) -> None:
    lic_dir = path / "licenses"
    if lic_dir.exists() and "dist-info" in lic_dir.parent.name:
        shutil.rmtree(lic_dir)
        print(f"{lic_dir} removed.")
    for k in NOT_ALLOWED:
        nap = path / k
        if nap.exists():
            print(nap)
            nap.unlink()


def main() -> None:
    cwd = Path.cwd()
    for path in cwd.rglob("*"):
        if path.is_dir() and "dist-info" in path.name:
            process_lic(path)
            if len(list(path.iterdir())) < 2:
                cprint(f"{path.name} empty pkg", "cyan")


if __name__ == "__main__":
    raise SystemExit(main())
