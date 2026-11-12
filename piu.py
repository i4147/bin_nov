#!/data/data/com.termux/files/usr/bin/env python
"""Uninstall pip packages by name or by substring.
Matches the arguments against the installed distribution names and calls ``pip uninstall -y`` for the matches.
``-w`` treats the arguments as substrings instead of exact names.
Usage: piu [-w] PATTERN [PATTERN ...] Examples: $ piu requests $ piu -w numpy Runs as a script (``python piu.py``) in the directory it should act on; the module can also be imported.
"""

from importlib.metadata import distributions
import sys

from pip._internal.cli.main import main as pip_main


def get_installed_packages() -> list[str]:
    return [dist.metadata["Name"] for dist in distributions() if dist.metadata["Name"] is not None]


def uninstall(packages: list[str]) -> int:
    args = ["uninstall", "-y", *packages]
    return pip_main(args)


def main() -> None:
    argv: list[str] = sys.argv[1:]
    if not argv:
        print("Usage: python piu.py [-w] <package> [<package> ...]")
        sys.exit(1)
    wildcard: bool = argv[0] == "-w"
    patterns: list[str] = argv[1:] if wildcard else argv
    if not patterns:
        print("Usage: python piu.py [-w] <package> [<package> ...]")
        sys.exit(1)
    installed: list[str] = get_installed_packages()
    installed_map: dict[str, str] = {name.lower(): name for name in installed}
    matched: set[str] = set()
    if wildcard:
        for pattern in patterns:
            p: str = pattern.lower()
            for name in installed:
                if p in name.lower():
                    matched.add(name)
    else:
        for pattern in patterns:
            p = pattern.lower()
            if p in installed_map:
                matched.add(installed_map[p])
    if not matched:
        print("No matching packages found")
        return
    uninstall(sorted(matched))


if __name__ == "__main__":
    main()
