#!/data/data/com.termux/files/usr/bin/env python
"""List the packages that are installed with more than one version.
Reads the ``.dist-info``/``.egg-info`` directory names of every ``site-packages`` location and prints the packages whose name appears with several versions, so leftovers from earlier installs become visible.
Usage: distinfo Examples: $ distinfo Runs as a script (``python distinfo.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
import contextlib
from pathlib import Path
import re
import site


def get_site_packages_dirs():
    dirs = []
    with contextlib.suppress(Exception):
        dirs.extend(site.getsitepackages())
    dirs.append(site.getusersitepackages())
    return list(dict.fromkeys(dirs))


def parse_pkg_info(dirname):
    m = re.match(r"(.+)-(\d+.*?)(\.dist-info|\.egg-info)$", dirname)
    if m:
        return m.group(1).lower(), m.group(2)
    return None, None


def find_multiple_versions() -> None:
    pkg_versions = defaultdict(set)
    for sp_dir in get_site_packages_dirs():
        sp_path = Path(sp_dir)
        if not sp_path.is_dir():
            continue
        for entry in sp_path.iterdir():
            if entry.name.endswith((".dist-info", ".egg-info")):
                name, version = parse_pkg_info(entry.name)
                if name:
                    pkg_versions[name].add(version)
    for pkg, versions in sorted(pkg_versions.items()):
        if len(versions) > 1:
            print(f"\nPackage: {pkg}")
            for v in sorted(versions):
                print(f"  - Version: {v}")
    print("\nDone.")


if __name__ == "__main__":
    find_multiple_versions()
