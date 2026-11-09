#!/data/data/com.termux/files/usr/bin/env python
"""Inject the personal details of ``~/.myinfo`` into a project's metadata files.
Reads the name, e-mail and GitHub username (plus the extra fields), applies the substitutions to ``setup.py``, ``setup.cfg``, ``pyproject.toml``, ``MANIFEST.in``, ``tox.ini`` and ``Pipfile`` and offers ``--dry-run`` and ``--diff``.
Usage: inject-info PROJECT_DIR [--dry-run] [--diff] Positional arguments: project_dir Project directory to update Options: --dry-run Do not write any files, just report --diff Print a unified diff of every change --info Examples: $ inject-info .
--diff Command-line script: run ``python inject-info.py`` or the ``inject-info`` command.
The module is importable as well."""

import argparse
import difflib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from dh import SKIP_DIRS


DEFAULT_INFO = Path.home() / ".myinfo"
TARGET_FILENAMES: set[str] = {
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "MANIFEST.in",
    "tox.ini",
    "Pipfile",
}
REQUIRED_KEYS: tuple[str, str, str] = ("name", "email", "github_username")


def load_info(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"error: info file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path}: {e}")
    for key in REQUIRED_KEYS:
        if not data.get(key):
            sys.exit(f"error: '{key}' is empty or missing in {path}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", data["github_username"]):
        sys.exit(f"error: invalid github_username: {data['github_username']!r}")
    return data


def build_transformations(info: dict) -> dict:
    name = info["name"]
    email = info["email"]
    gh_user = info["github_username"]

    def lit(value: Any):
        return lambda m: f"{m.group(1)}{m.group(2)}{value}{m.group(2)}"

    def _gh_user(m: re.Match[str]) -> str:
        return m.group(0) if m.group(2) == gh_user else f"{m.group(1)}{gh_user}/"

    def _gh_io(m: re.Match[str]) -> str:
        return m.group(0) if m.group(1) == gh_user else f"{gh_user}.github.io"

    common = [
        (re.compile(r"(?<!git\+)(https?://(?:www\.)?github\.com/)([A-Za-z0-9_.-]+)/"), _gh_user),
        (re.compile(r"(?<!git\+)(\bgithub\.com/)([A-Za-z0-9_.-]+)/"), _gh_user),
        (re.compile(r"\b([A-Za-z0-9_.-]+)\.github\.io\b"), _gh_io),
    ]
    py = [
        (re.compile(r'(?<![.\w])(author[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(name)),
        (re.compile(r'(?<![.\w])(author_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(email)),
        (re.compile(r'(?<![.\w])(maintainer[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(name)),
        (re.compile(r'(?<![.\w])(maintainer_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(email)),
    ]

    def _cfg(value: Any):
        return lambda m: f"{m.group(1)}{value}"

    cfg = [
        (re.compile(r'(?m)^([ \t]*author[ \t]*=[ \t]*)(?![ "\']).*$'), _cfg(name)),
        (re.compile(r'(?m)^([ \t]*author_email[ \t]*=[ \t]*)(?![ "\']).*$'), _cfg(email)),
        (re.compile(r'(?m)^([ \t]*maintainer[ \t]*=[ \t]*)(?![ "\']).*$'), _cfg(name)),
        (re.compile(r'(?m)^([ \t]*maintainer_email[ \t]*=[ \t]*)(?![ "\']).*$'), _cfg(email)),
    ]

    def _poetry_author(m: re.Match[str]):
        inner = m.group(2)
        if not inner.strip():
            return m.group(0)
        if inner.lstrip().startswith("{"):
            return m.group(0)
        return f'{m.group(1)}"{name} <{email}>"{m.group(3)}'

    toml = [
        (re.compile(r'(?<![.\w])(author[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(name)),
        (re.compile(r'(?<![.\w])(author_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(email)),
        (re.compile(r'(\b(?:authors|maintainers)\s*=\s*\[\s*\{\s*name\s*=\s*)(["\'])(?:[^"\'\\]|\\.)*\2'), lit(name)),
        (
            re.compile(r'(\b(?:authors|maintainers)\s*=\s*\[\s*\{[^}]*?\bemail\s*=\s*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            lit(email),
        ),
        (re.compile(r"(\b(?:authors|maintainers)\s*=\s*\[)([^\]]*)(\])"), _poetry_author),
    ]
    return {
        ".py": py + common,
        ".cfg": cfg + common,
        ".ini": cfg + common,
        ".toml": toml + common,
    }


def find_target_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            if fname in TARGET_FILENAMES:
                yield Path(dirpath) / fname


def process_file(path: Path, transforms: Iterable[Any], *, dry_run: bool, show_diff: bool) -> bool:
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        print(f"  skipped {path}: {e}", file=sys.stderr)
        return False
    new = original
    for pattern, repl in transforms:
        new = pattern.sub(repl, new)
    if new == original:
        return False
    if show_diff:
        sys.stdout.writelines(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path) + " (new)",
            )
        )
        print()
    if not dry_run:
        path.write_text(new, encoding="utf-8")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inject personal info from ~/.myinfo into a project.")
    parser.add_argument("project_dir", type=Path, help="Project directory to update")
    parser.add_argument("--dry-run", action="store_true", help="Do not write any files, just report")
    parser.add_argument("--diff", action="store_true", help="Print a unified diff of every change")
    parser.add_argument("--info", type=Path, default=DEFAULT_INFO, help=f"Path to info file (default: {DEFAULT_INFO})")
    args = parser.parse_args(argv)
    if not args.project_dir.is_dir():
        sys.exit(f"error: {args.project_dir} is not a directory")
    info = load_info(args.info)
    table = build_transformations(info)
    changed = 0
    scanned = 0
    for path in find_target_files(args.project_dir):
        scanned += 1
        transforms = table.get(path.suffix.lower())
        if transforms is None:
            continue
        if process_file(path, transforms, dry_run=args.dry_run, show_diff=args.diff):
            changed += 1
            if not args.diff:
                action = "would update" if args.dry_run else "updated"
                print(f"{action}: {path}")
    print()
    print(f"Scanned {scanned} file(s), {changed} changed" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
