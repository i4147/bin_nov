#!/data/data/com.termux/files/usr/bin/env python
"""Rename wheels that were mangled by a bulk rename back to their canonical name.
Opens each ``.whl`` in the current directory, reads ``Name``/``Version`` from the bundled ``METADATA`` and renames the file to ``name-version-pyX-none-any.whl``, refusing to overwrite an existing file.
Usage: fixwhl Examples: $ fixwhl Runs as a script (``python fixwhl.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

from packaging.utils import canonicalize_name


def parse_metadata_from_wheel(wheel_path: Path) -> tuple[str,]:
    with ZipFile(wheel_path) as zf:
        meta_name = next(
            (n for n in zf.namelist() if n.endswith(".dist-info/METADATA")),
            None,
        )
        if meta_name is None:
            msg = "missing .dist-info/METADATA"
            raise ValueError(msg)
        text = zf.read(meta_name).decode("utf-8", errors="replace")
        msg = Parser().parsestr(text)
        name = msg.get("Name")
        version = msg.get("Version")
        if not name or not version:
            msg_0 = "missing Name or Version in METADATA"
            raise ValueError(msg_0)
        dist = canonicalize_name(name).replace("-", "_")
        return dist, version


def safe_target_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = dest.with_name(f"{dest.stem}.{i}{dest.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def restore_wheel_names(folder: str | Path) -> None:
    folder = Path(folder)
    conflict_dir = folder / "_wheel_name_conflicts"
    conflict_dir.mkdir(exist_ok=True)
    versions_by_pkg = defaultdict(set)
    for src in sorted(folder.iterdir()):
        if not src.is_file() or src.suffix != ".whl":
            continue
        try:
            dist, version = parse_metadata_from_wheel(src)
            versions_by_pkg[dist].add(version)
        except Exception as e:
            print(f"SKIP  {src.name}  ({e})")
            continue
        dest = folder / f"{dist}-{version}-py3-none-any.whl"
        if dest.exists() and dest.resolve() != src.resolve():
            moved = safe_target_path(conflict_dir / src.name)
            src.rename(moved)
            print(f"CONFLICT {src.name} -> moved to {moved.name}")
        elif dest.resolve() == src.resolve():
            print(f"OK    {src.name} -> already correct")
        else:
            src.rename(dest)
            print(f"RENAMED {src.name} -> {dest.name}")
    for pkg, versions in sorted(versions_by_pkg.items()):
        if len(versions) > 1:
            print(f"MULTIPLE VERSIONS: {pkg} -> {sorted(versions)}")


if __name__ == "__main__":
    restore_wheel_names(".")
