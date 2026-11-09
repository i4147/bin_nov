#!/data/data/com.termux/files/usr/bin/env python
"""Print the ``Requires-Dist`` entries of an installed package, or dump them all.
With a package name the declared dependencies are printed one per line; without arguments every installed distribution is read and the mapping is written to ``requirements.json``.
Usage: get_req [PACKAGE] Examples: $ get_req requests $ get_req Runs as a script (``python get_req.py``) in the directory it should act on; the module can also be imported."""

from importlib.metadata import PackageNotFoundError, distributions, metadata
import json
import sys


def get_requires_dist(pkgname):
    try:
        return metadata(pkgname).get_all("Requires-Dist") or []
    except PackageNotFoundError:
        return []


def main() -> None:
    if len(sys.argv) > 1:
        pkgname = sys.argv[1]
        deps = get_requires_dist(pkgname)

        if not deps:
            print(f"No Requires-Dist entries found for {pkgname}")
        else:
            print("\n".join(deps))
    else:
        requirements = {}

        for dist in distributions():
            name = dist.metadata.get("Name")
            if name:
                requirements[name] = dist.metadata.get_all("Requires-Dist") or []

        with open("requirements.json", "w", encoding="utf-8") as f:
            json.dump(requirements, f, indent=2, sort_keys=True)

        print(f"Saved dependencies for {len(requirements)} packages to requirements.json")


if __name__ == "__main__":
    main()
