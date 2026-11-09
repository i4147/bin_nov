#!/data/data/com.termux/files/usr/bin/env python
"""Diagnose the shell setup: ``PATH`` entries, executables, aliases and functions.
Lists the entries of ``PATH`` with duplicate detection, hashes the executables it finds, and extracts the aliases and functions defined by the shell rc file (``~/.bashrc`` by default).
``--fix-dedup`` rewrites the rc file without the repeated entries.
Usage: pathdiag [--fix-dedup] [--rc-file FILE] [--alias-file FILE] [--func-file FILE] Options: --fix-dedup Deduplicate PATH and update shell rc --rc-file Path to shell rc file --alias-file Path to aliases file --func-file Path to functions file Examples: $ pathdiag --fix-dedup Command-line script: run ``python pathdiag.py`` or the ``pathdiag`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import sys


def get_path_entries() -> list[str]:
    path_env: str = os.environ.get("PATH", "")
    return [p for p in path_env.split(":") if p]


def get_file_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return ""
    h = sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(1048576):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def scan_executables(
    dirs: list[Path],
) -> tuple[dict[str, list[tuple[Path, str]]], dict[str, list[str]]]:
    executables: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    duplicates: dict[str, list[str]] = defaultdict(list)
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
            for item in directory.iterdir():
                if item.is_file() and os.access(item, os.X_OK):
                    file_hash = get_file_hash(item)
                    executables[item.name].append((item, file_hash))
                    duplicates[item.name].append(str(directory))
        except (PermissionError, OSError):
            continue
    conflicting = {cmd: locs for cmd, locs in duplicates.items() if len(locs) > 1}
    return dict(executables), conflicting


def extract_aliases(alias_file: Path) -> set[str]:
    aliases: set[str] = set()
    pattern = re.compile(r"^\s*alias\s+([a-zA-Z_][a-zA-Z0-9_-]*)\s*=", re.MULTILINE)
    if not alias_file.exists():
        return aliases
    try:
        content = alias_file.read_text(encoding="utf-8")
        matches = pattern.findall(content)
        for match in matches:
            aliases.add(match)
    except Exception:
        pass
    return aliases


def extract_functions(func_file: Path) -> set[str]:
    functions: set[str] = set()
    patterns = [
        re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*\(\s*\)\s*\{"),
        re.compile(r"^\s*function\s+([a-zA-Z_][a-zA-Z0-9_-]*)\s*\{"),
        re.compile(r"^\s*function\s+([a-zA-Z_][a-zA-Z0-9_-]*)\s*\(\s*\)\s*\{"),
    ]
    if not func_file.exists():
        return functions
    try:
        for line in func_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for pattern in patterns:
                match = pattern.search(stripped)
                if match:
                    functions.add(match.group(1))
                    break
    except Exception:
        pass
    return functions


def deduplicate_paths(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


def update_rc_file(rc_path: Path, new_path_str: str) -> bool:
    if not rc_path.exists():
        return False
    try:
        content = rc_path.read_text(encoding="utf-8")
        backup_path = rc_path.with_suffix(rc_path.suffix + ".backup")
        shutil.copy2(rc_path, backup_path)
        lines = content.splitlines()
        path_export = f'export PATH="{new_path_str}"'
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("export PATH="):
                new_lines.append(path_export)
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(path_export)
        rc_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fix-dedup",
        action="store_true",
        help="Deduplicate PATH and update shell rc",
    )
    parser.add_argument("--rc-file", type=str, default="", help="Path to shell rc file")
    parser.add_argument("--alias-file", type=str, default="", help="Path to aliases file")
    parser.add_argument("--func-file", type=str, default="", help="Path to functions file")
    args = parser.parse_args()
    raw_entries = get_path_entries()
    valid_dirs = [Path(d).expanduser() for d in raw_entries if Path(d).exists()]
    print("--- PATH DIAGNOSTIC TOOL ---")
    print(f"Total PATH entries: {len(raw_entries)}")
    print(f"Valid PATH directories: {len(valid_dirs)}")
    unique_entries = deduplicate_paths(raw_entries)
    if len(unique_entries) < len(raw_entries):
        print(f"Duplicates detected in PATH: {len(raw_entries) - len(unique_entries)}")
        seen_dups: set[str] = set()
        for entry in raw_entries:
            if raw_entries.count(entry) > 1 and entry not in seen_dups:
                seen_dups.add(entry)
                print(f"  - Duplicate entry: {entry} (x{raw_entries.count(entry)})")
    else:
        print("No duplicate PATH entries found.")
    executables, conflicts = scan_executables(valid_dirs)
    print(f"Unique commands found: {len(executables)}")
    if conflicts:
        print(f"Executables found in multiple directories: {len(conflicts)}")
        for cmd, locs in sorted(conflicts.items()):
            print(f"  • {cmd}:")
            for idx, loc in enumerate(locs):
                status = "ACTIVE" if idx == 0 else "SHADOWED"
                print(f"    - {loc} ({status})")
    else:
        print("No shadowed or duplicate binaries found.")
    home = Path.home()
    rc_path = Path(args.rc_file) if args.rc_file else home / ".zshrc"
    alias_path = Path(args.alias_file) if args.alias_file else home / ".config/zsh.d/aliases.zsh"
    func_path = Path(args.func_file) if args.func_file else home / ".config/zsh.d/functions.zsh"
    aliases = extract_aliases(alias_path)
    functions = extract_functions(func_path)
    alias_conflicts = {cmd: executables[cmd] for cmd in aliases if cmd in executables}
    func_conflicts = {cmd: executables[cmd] for cmd in functions if cmd in executables}
    if alias_conflicts:
        print(f"Alias conflicts found: {len(alias_conflicts)}")
        for alias in sorted(alias_conflicts.keys()):
            print(f"  - Alias '{alias}' conflicts with binary in PATH")
    if func_conflicts:
        print(f"Function conflicts found: {len(func_conflicts)}")
        for func in sorted(func_conflicts.keys()):
            print(f"  - Function '{func}' conflicts with binary in PATH")
    if args.fix_dedup:
        new_path_str = ":".join(unique_entries)
        success = update_rc_file(rc_path, new_path_str)
        if success:
            print(f"Successfully updated {rc_path} with deduplicated PATH.")
        else:
            print(f"Failed to update {rc_path}.")
    if conflicts or alias_conflicts or func_conflicts:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
