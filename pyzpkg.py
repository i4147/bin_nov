#!/data/data/com.termux/files/usr/bin/env python
"""Zip an installed package and install a loader stub for it.
Locates the package in the user ``site-packages``, copies its files into a ``.zip`` (compiling them first when possible), removes the original directory and writes a loader stub so the import still resolves through the archive.
Usage: pyzpkg PACKAGE [PACKAGE ...] Examples: $ pyzpkg requests Modifies the installed package in place; keep a backup of ``site-packages``."""

import compileall
import csv
import os
from pathlib import Path
import shutil
import sys


def get_user_site_packages():
    import site

    return Path(site.getusersitepackages())


def check_dist_info_safely(dist_info_path):
    top_level_path = dist_info_path / "top_level.txt"
    if top_level_path.exists():
        try:
            with open(top_level_path, encoding="utf-8") as tlf:
                top_levels = [line.strip() for line in tlf if line.strip()]
                if len(top_levels) > 1:
                    return (
                        True,
                        f"Multi-folder package detected ({', '.join(top_levels)})",
                    )
        except Exception as e:
            print(f"  Warning: Couldn't read top_level.txt for {dist_info_path.name}: {e}")
    record_path = dist_info_path / "RECORD"
    if record_path.exists():
        try:
            with open(record_path, encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                for row in reader:
                    if row:
                        path = row[0].lower()
                        if path.endswith(".so"):
                            return (True, "Contains compiled C-extensions (.so binary)")
                        if path.endswith(".pth"):
                            return (True, "Contains path configuration file (.pth)")
        except Exception as e:
            print(f"  Warning: Could not read RECORD for {dist_info_path.name}: {e}")
    return (False, "")


def create_loader_stub(pkg_name, site_packages):
    stub_path = site_packages / f"{pkg_name}.py"
    pkg_dir = site_packages / pkg_name
    has_main = (pkg_dir / "__main__.py").exists()
    stub_content = f"""import sys\nfrom pathlib import Path\n\nZIP_DIR = Path(r"{
        site_packages.absolute()
    }")\nZIP_PATH = str(ZIP_DIR / "{
        pkg_name
    }.zip")\n\nif ZIP_PATH not in sys.path:\n    sys.path.insert(0, ZIP_PATH)\n\nmodule = __import__("{
        pkg_name
    }")\nsys.modules["{pkg_name}"] = module\n\n# Edge Case Fix: Support running via 'python -m {
        pkg_name
    }'\nif __name__ == "__main__" and {
        has_main
    }:\n    import importlib.util\n    import runpy\n    \n    # Locate and execute the __main__.py file safely inside the zip archive\n    spec = importlib.util.find_spec("{
        pkg_name
    }.__main__")\n    if spec and spec.origin:\n        runpy.run_path(spec.origin, run_name="__main__")\n"""
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(stub_content)
    print(f"  Created loader stub: {pkg_name}.py (with __main__ execution hook: {has_main})")


def process_package(pkg_name, site_packages):
    pkg_dir = site_packages / pkg_name
    if not pkg_dir.is_dir() or pkg_name.endswith((".dist-info", ".egg-info", "__pycache__")):
        return False
    if "-" in pkg_name or "_" in pkg_name:
        return False
    if not (pkg_dir / "__init__.py").exists():
        print(f"Skipped: {pkg_name} (Missing __init__.py - likely a shared namespace package)")
        return False
    print(f"Checking: {pkg_name}...")
    dist_info_prefix = pkg_name.lower().replace("-", "_")
    corresponding_dist = None
    for dist in site_packages.iterdir():
        if dist.is_dir() and dist.name.lower().startswith(dist_info_prefix) and dist.name.endswith(".dist-info"):
            corresponding_dist = dist
            break
    if corresponding_dist:
        should_skip, reason = check_dist_info_safely(corresponding_dist)
        if should_skip:
            print(f"  Skipped: {pkg_name} -> {reason}")
            return False
    else:
        print(f"  Warning: No .dist-info found for {pkg_name}. Proceeding cautiously...")
    print(f"  Processing: {pkg_name}...")
    compileall.compile_dir(pkg_dir, quiet=1, legacy=True)
    import zipfile

    zip_filename = f"{pkg_name}.zip"
    zip_path = site_packages / zip_filename
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(pkg_dir):
            for file in files:
                path = Path(root) / file
                arcname = str(path.relative_to(pkg_dir))
                zipf.write(path, arcname)
    create_loader_stub(pkg_name, site_packages)
    try:
        shutil.rmtree(pkg_dir)
        print(f"  Successfully zipped into {zip_filename}")
        return True
    except Exception as e:
        print(f"  Error deleting original directory for {pkg_name}: {e}")
        return False


def main():
    site_packages = get_user_site_packages()
    if not site_packages.exists():
        print(f"User site-packages directory not found at: {site_packages}")
        return
    os.chdir(site_packages)
    converted = 0
    if len(sys.argv) > 1:
        target_pkg = sys.argv[1]
        print(f"Targeting single package via CLI: {target_pkg}\n")
        if process_package(target_pkg, site_packages):
            converted += 1
    else:
        print(f"Scanning all packages in: {site_packages}\n")
        for path in site_packages.iterdir():
            if process_package(path.name, site_packages):
                converted += 1
    print(f"\nFinished! Successfully converted {converted} package(s) to zip format.")


if __name__ == "__main__":
    raise SystemExit(main())
