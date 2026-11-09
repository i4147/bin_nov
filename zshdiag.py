#!/data/data/com.termux/files/usr/bin/env python
"""Diagnose a zsh setup: duplicate aliases, functions and shadowed binaries.
Collects the aliases and functions defined in ``~/.zshrc``, ``~/.config/zsh.d`` and ``~/.oh-my-zsh``, lists the definitions that appear more than once with their source files and shows which commands are shadowed by other binaries on ``PATH``.
Usage: zshdiag Examples: $ zshdiag Runs as a script (``python zshdiag.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
import contextlib
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable


HOME = Path.home()
ZSHRC = HOME / ".zshrc"
CUSTOM_DIR = HOME / ".config" / "zsh.d"
OH_MY_ZSH = HOME / ".oh-my-zsh"
ALIAS_RE = re.compile(r"""^\s*alias\s+(['"]?)([A-Za-z0-9_.+-]+)\1\s*=""")
FUNCTION_RE = re.compile(
    r"""^\s*(?:function\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?|""" r"""([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\))\s*\{"""
)


def config_files():
    files = []
    if ZSHRC.is_file():
        files.append(ZSHRC)
    if CUSTOM_DIR.is_dir():
        files.extend(sorted(CUSTOM_DIR.glob("*.zsh")))
    if OH_MY_ZSH.is_dir():
        files.extend(sorted(OH_MY_ZSH.rglob("*.zsh")))
    return list(dict.fromkeys(files))


def collect_definitions(files: list[Path]):
    aliases = defaultdict(list)
    functions = defaultdict(list)
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            alias_match = ALIAS_RE.match(line)
            if alias_match:
                aliases[alias_match.group(2)].append((path, number))
                continue
            function_match = FUNCTION_RE.match(line)
            if function_match:
                name = function_match.group(1) or function_match.group(2)
                functions[name].append((path, number))
    return aliases, functions


def path_binaries() -> tuple[list[Any],]:
    occurrences = defaultdict(list)
    path_dirs = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry or ".").expanduser()
        with contextlib.suppress(OSError):
            directory = directory.resolve()
        path_dirs.append(directory)
        if not directory.is_dir():
            continue
        try:
            for item in directory.iterdir():
                if item.is_file() and os.access(item, os.X_OK):
                    occurrences[item.name].append(directory)
        except OSError:
            pass
    return occurrences, path_dirs


def show_locations(locations: Iterable[Any]) -> str:
    return ", ".join(f"{path}:{line}" for path, line in locations)


def main() -> None:
    files = config_files()
    aliases, functions = collect_definitions(files)
    binaries, _path_dirs = path_binaries()
    print("Zsh configuration diagnostic")
    print("=" * 30)
    print(f"Scanned config files: {len(files)}")
    for path in files:
        print(f"  {path}")
    print("\nDuplicate executable names in PATH")
    print("-" * 34)
    duplicates = {name: dirs for name, dirs in binaries.items() if len(dirs) > 1}
    if duplicates:
        for name, dirs in sorted(duplicates.items()):
            print(f"{name}: " + ", ".join(map(str, dirs)))
    else:
        print("None found.")
    print("\nAlias conflicts with PATH executables or custom scripts")
    print("-" * 55)
    conflicts = []
    custom_names = {path.stem for path in CUSTOM_DIR.glob("*.zsh")} if CUSTOM_DIR.is_dir() else set()
    for name, locations in sorted(aliases.items()):
        reasons = []
        if name in binaries:
            reasons.append("executable in PATH: " + ", ".join(map(str, binaries[name])))
        if name in custom_names:
            reasons.append(f"custom script file: {CUSTOM_DIR / (name + '.zsh')}")
        if shutil.which(name) and not any(reason.startswith("executable in PATH") for reason in reasons):
            reasons.append(f"command resolves to: {shutil.which(name)}")
        if reasons:
            conflicts.append((name, locations, reasons))
    if conflicts:
        for name, locations, reasons in conflicts:
            print(f"{name} (alias at {show_locations(locations)})")
            for reason in reasons:
                print(f"  - {reason}")
    else:
        print("None found.")
    print("\nDuplicate alias definitions")
    print("-" * 27)
    duplicate_aliases = {n: locs for n, locs in aliases.items() if len(locs) > 1}
    if duplicate_aliases:
        for name, locations in sorted(duplicate_aliases.items()):
            print(f"{name}: {show_locations(locations)}")
    else:
        print("None found.")
    print("\nDuplicate function definitions")
    print("-" * 30)
    duplicate_functions = {n: locs for n, locs in functions.items() if len(locs) > 1}
    if duplicate_functions:
        for name, locations in sorted(duplicate_functions.items()):
            print(f"{name}: {show_locations(locations)}")
    else:
        print("None found.")
    print("\nNote: This is a static scan. It does not evaluate zsh conditionals,")
    print("sourced files, generated aliases, or runtime plugin behavior.")


if __name__ == "__main__":
    main()
