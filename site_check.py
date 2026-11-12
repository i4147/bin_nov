#!/data/data/com.termux/files/usr/bin/env python
"""Compile and import every module in ``site-packages`` and write a JSON report.
Byte-compiles each ``.py`` file of the system and user ``site-packages``, imports every top-level module with pkgutil and saves the compilation and import failures to ``site_packages_sanity_report.json``, printing the two counts.
Usage: python site_check.py Examples: $ python site_check.py Runs the full check at import time; it can take a while on a large environment.
"""

import importlib
import json
import os
from pathlib import Path
import pkgutil
import py_compile
import site
from typing import Any


def run_sanity_check() -> dict[str, list[Any]]:
    issues = {"compilation_errors": [], "import_errors": []}
    site_dirs = site.getsitepackages()
    if hasattr(site, "getusersitepackages"):
        user_site = site.getusersitepackages()
        if Path(user_site).exists():
            site_dirs.append(user_site)
    print(f"Scanning site-packages directories: {site_dirs}")
    seen_modules = set()
    for site_dir in site_dirs:
        if not Path(site_dir).exists():
            continue
        for root, dirs, files in os.walk(site_dir):
            for file in files:
                if file.endswith(".py"):
                    full_path = Path(root).joinpath(file)
                    try:
                        py_compile.compile(full_path, doraise=True)
                    except Exception as e:
                        issues["compilation_errors"].append({"file": full_path, "error": str(e)})
        for finder, name, ispkg in pkgutil.iter_modules([site_dir]):
            if name in seen_modules:
                continue
            seen_modules.add(name)
            try:
                importlib.import_module(name)
            except Exception as e:
                issues["import_errors"].append({"module": name, "error": str(e)})
    return issues


if __name__ == "__main__":
    print("Starting package sanity check...")
    report = run_sanity_check()
    report_filename = "site_packages_sanity_report.json"
    with open(report_filename, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4)
    print(f"\nSanity check complete. Report saved to '{report_filename}'.")
    print(f"- Compilation Errors Found: {len(report['compilation_errors'])}")
    print(f"- Import/Dependency Errors Found: {len(report['import_errors'])}")
