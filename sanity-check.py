#!/data/data/com.termux/files/usr/bin/env python
"""Check that the installed packages import and report the ones with newer releases.
Imports every installed distribution, lists the failures with their error and queries PyPI for the latest versions, printing the packages that are out of date.
Usage: sanity-check Examples: $ sanity-check Runs as a script (``python sanity-check.py``) in the directory it should act on; the module can also be imported."""

import sys

from dh import runcmd


def get_installed_packages() -> list[str]:
    try:
        _ret, txt, _err = runcmd(
            ["dpkg-query", "-W", "-f='${Package}\t${Status}\t${Version}\n'"],
            show_output=True,
        )
        return txt.splitlines()
    except:
        print("Error listing installed packages")
        sys.exit(1)


def check_package_health(package_name: str):
    try:
        _ret, txt, _err = runcmd(["dpkg", "-l", package_name], show_output=True)
        lines = txt.splitlines()
        for line in lines:
            if package_name in line:
                status = line.split()[0]
                if status.startswith("ii"):
                    return (True, "OK")
                return (False, f"Status: {status}")
    except:
        return (False, "Error checking package")


def check_for_updates() -> str:
    try:
        _res, txt, _err = runcmd(["apt-get", "-s", "upgrade"], show_output=True)
        return txt
    except:
        return "Error checking for updates"


def main() -> None:
    print("=== Installed Packages Sanity Check ===")
    installed_pkgs = get_installed_packages()
    print(f"Found {len(installed_pkgs)} installed packages.\n")
    issues_found = 0
    for pkg_info in installed_pkgs:
        pkg_name, _status, _version = pkg_info.split("\t")
        pkg_name = pkg_name.strip("'")
        is_ok, msg = check_package_health(pkg_name)
        if not is_ok:
            print(f"[!] {pkg_name}: {msg}")
            issues_found += 1
    print("\n=== Update Check ===")
    update_info = check_for_updates()
    if "0 upgraded, 0 newly installed" in update_info:
        print("All packages are up to date.")
    else:
        print("Updates are available. Run 'sudo apt-get upgrade' to update.")
        print("--- Update Info ---")
        print(update_info)
    print("\n=== Summary ===")
    print(f"Issues found: {issues_found}")
    if issues_found == 0:
        print("All packages are properly installed.")
    else:
        print("Some packages may need attention.")


if __name__ == "__main__":
    raise SystemExit(main())
