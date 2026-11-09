#!/data/data/com.termux/files/usr/bin/env python
"""Check that every installed distribution can still be imported.
Imports each package reported by ``importlib.metadata.distributions()`` and prints the failures (with the exception) plus a summary of how many packages import cleanly.
Usage: pip_sanity_check Examples: $ pip_sanity_check Runs as a script (``python pip_sanity_check.py``) in the directory it should act on; the module can also be imported."""

import importlib
import importlib.metadata
import sys

from dh import PKG_MAPPING as pkg_mapping


GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def sanity_check_packages():
    print(f"Python Version: {sys.version.split()[0]}")
    print("Starting system site-packages sanity check in Termux...\n")
    installed_dists = sorted(importlib.metadata.distributions(), key=lambda d: d.name.lower())
    healthy_packages = []
    problematic_packages = []
    for dist in installed_dists:
        pkg_name = dist.name
        version = dist.version
        top_levels = []
        try:
            top_level_text = dist.read_text("top_level.txt")
            if top_level_text:
                top_levels = [line.strip() for line in top_level_text.splitlines() if line.strip()]
                print(f"{pkg_name}: {top_levels}")
        except Exception:
            pass
        if not top_levels:
            top_levels = [pkg_name.replace("-", "_").lower()]
        pkg_ok = True
        error_details = ""
        if pkg_name in pkg_mapping:
            mod_name = pkg_mapping.get(pkg_name)
            try:
                importlib.import_module(mod_name)
                continue
            except:
                pass
        for mod_name in top_levels:
            try:
                importlib.import_module(mod_name)
                importlib.import_module(mod_name)
            except Exception as e:
                pkg_ok = False
                error_details = f"{type(e).__name__}: {e}"
                break
        if pkg_ok:
            healthy_packages.append(f"{pkg_name}=={version}")
            print(f"{GREEN}[\u2713] {pkg_name}   {version}{RESET}")
        else:
            problematic_packages.append({"name": pkg_name, "version": version, "error": error_details})
            print(f"{YELLOW}[\u2717]  {pkg_name}{RESET}")
    print("\n" + "=" * 50)
    print("Sanity Check Complete:")
    print(f"  - Healthy packages: {len(healthy_packages)}")
    print(f"  - Problematic packages: {len(problematic_packages)}")
    print("=" * 50)
    with open("requirements.txt", "w", encoding="utf-8") as f:
        f.write("# Verified healthy packages\n")
        f.writelines(f"{pkg}\n" for pkg in healthy_packages)
    print("Saved healthy package list to 'requirements.txt'")
    if problematic_packages:
        with open("problematic_packages.log", "w", encoding="utf-8") as f:
            f.write("Package,Version,Error\n")
            f.writelines(f"{item['name']},{item['version']},{item['error']}\n" for item in problematic_packages)
        print("Saved problematic packages to 'problematic_packages.log'")


if __name__ == "__main__":
    sanity_check_packages()
