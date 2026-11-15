#!/data/data/com.termux/files/usr/bin/env python
"""Distribute current-directory items into N size-balanced folders named by size range."""

from pathlib import Path
import re
import shutil
import sys


SKIP: set[str] = {".git", ".gitignore"}
RANGE_RE = re.compile(r"^(empty(_\d+)?|\d+(?:\.\d+)?[a-z]+-\d+(?:\.\d+)?[a-z]+(_\d+)?)$")


def get_size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in path.walk():
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            fp = root / f
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                pass
    return total


def human_compact(n: float) -> str:
    if n == 0:
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            val = f"{n:.0f}" if (n >= 10 or n == int(n)) else f"{n:.1f}"
            return f"{val}{unit.lower()}"
        n /= 1024
    return f"{n:.1f}pb"


def chunk_label(sizes: list[int], first: bool) -> str:
    if not sizes:
        return "empty"
    lo = 0 if first else min(sizes)
    return f"{human_compact(lo)}-{human_compact(max(sizes))}"


def make_unique(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for label in labels:
        count = seen.get(label, 0) + 1
        seen[label] = count
        result.append(label if count == 1 else f"{label}_{count}")
    return result


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} N", file=sys.stderr)
        sys.exit(1)
    try:
        n = int(sys.argv[1])
    except ValueError:
        print("N must be an integer", file=sys.stderr)
        sys.exit(1)
    if n < 1:
        print("N must be >= 1", file=sys.stderr)
        sys.exit(1)

    cwd = Path.cwd()
    items: list[tuple[str, int]] = [
        (p.name, get_size(p)) for p in cwd.iterdir() if p.name not in SKIP and not RANGE_RE.match(p.name)
    ]

    if not items:
        print("Nothing to move.")
        return

    items.sort(key=lambda x: x[1])
    m = len(items)

    chunks: list[list[tuple[str, int]]] = [[] for _ in range(n)]
    for idx, item in enumerate(items):
        chunk_idx = idx if m <= n else idx * n // m
        chunks[chunk_idx].append(item)

    labels = make_unique([chunk_label([s for _, s in chunk], first=(i == 0)) for i, chunk in enumerate(chunks)])

    for label in labels:
        (cwd / label).mkdir(exist_ok=True)

    for label, chunk in zip(labels, chunks):
        for name, _ in chunk:
            shutil.move(str(cwd / name), str(cwd / label / name))
            print(f"{name}  ->  {label}")

    print("\nSummary:")
    for label, chunk in zip(labels, chunks):
        total = sum(s for _, s in chunk)
        print(f"  {label}: {human_compact(total):>10}  ({len(chunk)} item(s))")


if __name__ == "__main__":
    main()
