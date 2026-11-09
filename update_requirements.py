#!/data/data/com.termux/files/usr/bin/env python
"""Show packages with newer versions available on PyPI."""


import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import sys
from typing import Final

from dh import _MAX_WORKERS, MAX_WORKERS
from packaging.version import InvalidVersion, Version
import requests


_TIMEOUT: Final[float] = 10.0
_DEFAULT_INPUT: Final[str] = "requirements.txt"
_DEFAULT_OUTPUT: Final[str] = "upgradable.txt"
_DIM: Final[str] = "\033[2m"
_GREEN: Final[str] = "\033[32m"
_RESET: Final[str] = "\033[0m"
_EDITABLE_RE = re.compile(r"^\s*(?:-e|--editable)\b")


def parse_freeze(path: Path) -> dict[str, str]:
    installed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _EDITABLE_RE.match(line):
            continue
        if "@" in line:
            continue
        name, sep, version = line.partition("==")
        if not sep:
            continue
        name = name.strip()
        version = version.strip()
        if not name or not version:
            continue
        installed[name] = version
    return installed


def fetch_latest(session: requests.Session, name: str) -> tuple[str, str | None]:
    try:
        r = session.get(f"https://pypi.org/pypi/{name}/json", timeout=_TIMEOUT)
        if r.status_code == 404:
            return name, None
        r.raise_for_status()
        return name, str(r.json()["info"]["version"])
    except (requests.RequestException, KeyError, ValueError):
        return name, None


def find_upgradable(
    installed: dict[str, str],
    *,
    max_workers: int = _MAX_WORKERS,
) -> dict[str, tuple[str, str]]:
    if not installed:
        return {}
    latest: dict[str, str | None] = {}
    with requests.Session() as session:
        session.headers["User-Agent"] = "requirements-updater/1.0"
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_latest, session, name): name for name in installed}
            for fut in as_completed(futures):
                name, ver = fut.result()
                latest[name] = ver
    upgradable: dict[str, tuple[str, str]] = {}
    for name, current in installed.items():
        new = latest.get(name)
        if new is None:
            continue
        try:
            is_newer = Version(new) > Version(current)
        except InvalidVersion:
            is_newer = new != current
        if is_newer:
            upgradable[name] = (current, new)
    return upgradable


def render(
    installed: dict[str, str],
    upgradable: dict[str, tuple[str, str]],
) -> str:
    lines: list[str] = []
    for name, current in installed.items():
        if name in upgradable:
            _, new = upgradable[name]
            lines.append(f"{name} {current} {_GREEN}{new}{_RESET}")
        else:
            lines.append(f"{name} {_DIM}{current} {current}{_RESET}")
    return "\n".join(lines)


def write_upgradable(
    path: Path,
    upgradable: dict[str, tuple[str, str]],
) -> None:
    path.write_text(
        "\n".join(f"{name}=={new}" for name, (_, new) in upgradable.items()),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upgradable",
        description="Show packages with newer versions available on PyPI.",
    )
    parser.add_argument(
        "src",
        nargs="?",
        default=_DEFAULT_INPUT,
        type=Path,
        help=f"pip freeze output (default: {_DEFAULT_INPUT})",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=Path(_DEFAULT_OUTPUT),
        type=Path,
        help=f"file to write upgradable pins to (default: {_DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)
    if not args.src.is_file():
        print(f"error: file not found: {args.src}", file=sys.stderr)
        return 1
    installed = parse_freeze(args.src)
    upgradable = find_upgradable(installed, max_workers=MAX_WORKERS)
    write_upgradable(args.output, upgradable)
    report = render(installed, upgradable)
    if report:
        print(report)
    print(f"\n{len(upgradable)} upgradable package(s) written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
