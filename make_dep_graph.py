#!/data/data/com.termux/files/usr/bin/env python
"""Dump the dependency graph of the installed distributions as JSON.
Reads the ``METADATA`` of every ``.dist-info`` directory in the first ``site-packages`` on ``sys.path``, collects the ``Requires-Dist`` entries (versions and markers stripped) and writes ``package_dependencies.json``.
Usage: make_dep_graph Examples: $ make_dep_graph Runs as a script (``python make_dep_graph.py``) in the directory it should act on; the module can also be imported.
"""

import json
import os
from pathlib import Path
import re
import sys


def get_installed_packages_dependencies():
    dependencies = {}
    site_packages_path = None
    for path in sys.path:
        if "site-packages" in path:
            site_packages_path = path
            break
    if not site_packages_path:
        return "Could not find site-packages directory."
    for package_dir in os.listdir(site_packages_path):
        if package_dir.endswith(".dist-info"):
            dist_info_path = Path(site_packages_path).joinpath(package_dir)
            metadata_path = Path(dist_info_path).joinpath("METADATA")
            if Path(metadata_path).exists():
                package_name = package_dir.split(".dist-info")[0]
                package_dependencies = []
                with Path(metadata_path).open(encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("Requires-Dist:"):
                            dep = re.sub(r";.*", "", line.split("Requires-Dist:")[1].strip())
                            dep = re.sub(r"[<>=~]", "", dep).split("(")[0].strip()
                            package_dependencies.append(dep)
                dependencies[package_name] = package_dependencies
    return dependencies


if __name__ == "__main__":
    all_dependencies = get_installed_packages_dependencies()
    if isinstance(all_dependencies, str):
        print(all_dependencies)
    else:
        output_file = "package_dependencies.json"
        with Path(output_file).open("w", encoding="utf-8") as f:
            json.dump(all_dependencies, f, indent=4, ensure_ascii=False)
        print(f"Package dependencies saved to {output_file}")
