#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations
import email
import email.policy
from importlib.metadata import distributions
from pathlib import Path
import sys


def remove_dep_from_metadata(dist, dep_name: str) -> None:
    metadata_path: Path = Path(dist._path) / "METADATA"
    if not metadata_path.exists():
        return
    with metadata_path.open("r", encoding="utf-8") as f:
        msg = email.message_from_file(f, policy=email.policy.compat32)
    changed: bool = False
    for key in list(msg.keys()):
        if key == "Requires-Dist" and msg[key].split()[0].lower() == dep_name.lower():
            del msg[key]
            changed = True
    if changed:
        with metadata_path.open("w", encoding="utf-8") as f:
            f.write(msg.as_string())


def main() -> None:
    if len(sys.argv) != 2:
        return
    dep_name: str = sys.argv[1]
    for dist in distributions():
        try:
            remove_dep_from_metadata(dist, dep_name)
        except Exception:
            continue


if __name__ == "__main__":
    main()
