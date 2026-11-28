#!/data/data/com.termux/files/usr/bin/env python
"""
Save full paths of files installed by selected Termux packages to ~/x11.txt. Packages that are not installed are skipped silently.

"""

from __future__ import annotations
import fnmatch
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable


OUTPUT_FILE = os.path.join(os.path.expanduser("~"), "x11.txt")

PACKAGES: list[str] = [
    # fonts / text
    "fontconfig",
    "ttf-dejavu",
    "harfbuzz",
    "fribidi",
    "raqm",
    # X11 core
    "libice",
    "libsm",
    "libx11",
    "libxau",
    "libxcb",
    "libxdmcp",
    "libxext",
    "libxmu",
    "libxrender",
    "libxt",
    "libxi",
    "libxkbcommon",
    "libxfixes",
    "libxshmfence",
    "libxxf86vm",
    "xcb-util*",
    "xorg-util-macros",
    "xorg-xauth",
    "xorgproto",
    "xorg-xprop",
    "xkeyboard-config",
    # image libs
    "littlecms",
    "openjpeg",
    "openjpeg-tools",
    "svt-av1",
    "giflib",
    "libwebp",
    "libavif",
    "libtiff",
    "libaom",
    "libdav1d",
    "librav1e",
    "libjxl",
    # Qt 6
    "qt6-qt5compat",
    "qt6-qtbase",
    "qt6-qtdeclarative",
    "qt6-qtlanguageserver",
    "qt6-qtshadertools",
    # graphics / GL / Vulkan
    "vulkan-*",
    "mesa",
    "mesa-vulkan-icd-swrast",
    "opengl",
    "libglvnd",
    "libglvnd-dev",
    "libwayland",
    "ocl-icd",
    # desktop integration
    "xdg-utils",
    "desktop-file-utils",
    "shared-mime-info",
    # multimedia
    "ffmpeg",
    "libvpx",
    "libx264",
    "libx265",
    # misc
    "libsodium",
    "libzmq",
]


def installed_packages():
    """Return the set of package names whose dpkg status is fully installed."""
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package} ${Status}\n"],
        capture_output=True,
        text=True,
    )
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and "install ok installed" in parts[1]:
            names.add(parts[0])
    return names


def resolve_packages(installed: Iterable[Any]):
    """Expand specs against installed packages. Specs with no installed match are dropped."""
    resolved = []
    for spec in PACKAGES:
        if any(ch in spec for ch in "*?["):
            resolved.extend(sorted(p for p in installed if fnmatch.fnmatch(p, spec)))
        elif spec in installed:
            resolved.append(spec)
    return list(dict.fromkeys(resolved))


def package_files(pkg: Any):
    """Return absolute paths of non-directory files owned by an installed package."""
    result = subprocess.run(
        ["dpkg-query", "-L", pkg],
        capture_output=True,
        text=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if Path(path).is_dir() and not Path(path).is_symlink():
            continue
        paths.append(path)
    return paths


def main() -> None:
    installed = installed_packages()
    if not installed:
        sys.exit("Could not read the dpkg database. Is this a Termux environment?")

    wanted = resolve_packages(installed)

    all_files = set()
    for pkg in wanted:
        all_files.update(package_files(pkg))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(path + "\n" for path in sorted(all_files))

    print(f"Packages processed: {len(wanted)}")
    print(f"Files written:      {len(all_files)}")
    print(f"Output:             {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
