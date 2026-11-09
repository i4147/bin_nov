#!/data/data/com.termux/files/usr/bin/env python
"""Put a local wheel into pip's HTTP cache so pip can install it offline.
Copies the wheel into the ``pip cache dir`` layout that matches a ``file://`` URL, computes the sha224 cache key pip expects and writes the JSON metadata next to it, extracted from the wheel's ``METADATA`` file.
Usage: inject_wheel_to_pip_cache WHEEL.whl Examples: $ inject_wheel_to_pip_cache dist/pkg-1.0-py3-none-any.whl Runs as a script (``python inject_wheel_to_pip_cache.py``) in the directory it should act on; the module can also be imported."""

from email import policy
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from pip._internal.utils.compatibility_tags import interpreter_name, interpreter_version
from pip._internal.utils.urls import path_to_url


def wheel_cache_hash(link_url: str) -> str:
    key_parts = {
        "url": link_url,
        "interpreter_name": interpreter_name(),
        "interpreter_version": interpreter_version(),
    }
    return hashlib.sha224(json.dumps(key_parts, sort_keys=True).encode("utf-8")).hexdigest()


def wheel_cache_dir(cache_dir: Path, link_url: str) -> Path:
    h = wheel_cache_hash(link_url)
    return Path(cache_dir) / "wheels" / h[0:2] / h[2:4] / h[4:6] / h


def extract_wheel_metadata(wheel_path: Path) -> dict:
    with zipfile.ZipFile(wheel_path, "r") as wheel_zip:
        metadata_files = [f for f in wheel_zip.namelist() if f.endswith(".dist-info/METADATA")]
        if not metadata_files:
            msg = f"No METADATA found in wheel {wheel_path}"
            raise ValueError(msg)
        metadata_content = wheel_zip.read(metadata_files[0]).decode("utf-8")
        metadata = Parser(policy=policy.compat32).parsestr(metadata_content)
    link_url = path_to_url(str(wheel_path.absolute()))
    wheel_metadata = {
        "url": link_url,
        "filename": wheel_path.name,
        "size": wheel_path.stat().st_size,
        "sha224": hashlib.sha224(wheel_path.read_bytes()).hexdigest(),
        "origin": "manual_cache",
    }
    for field in (
        "Name",
        "Version",
        "Summary",
        "Home-page",
        "Author",
        "License",
        "Requires-Python",
        "Requires-Dist",
    ):
        value = metadata.get(field)
        if value:
            wheel_metadata[field.lower().replace("-", "_")] = value
    return wheel_metadata


def add_wheel_to_pip_cache(wheel_path: Path) -> None:
    cache_dir = Path(subprocess.check_output(["pip", "cache", "dir"]).decode().strip())
    link_url = path_to_url(str(wheel_path.absolute()))
    target_dir = wheel_cache_dir(cache_dir, link_url)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / wheel_path.name
    shutil.copy2(wheel_path, target_file)
    metadata = extract_wheel_metadata(wheel_path)
    (target_dir / f"{wheel_path.name}.json").write_text(json.dumps(metadata, indent=2))
    print(f"Cache key (sha224): {wheel_cache_hash(link_url)}")
    print(f"Wheel cached at:    {target_file}")
    print(f"Metadata cached at: {target_dir / (wheel_path.name + '.json')}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python add_wheel_to_cache.py <path/to/wheel.whl>")
        sys.exit(1)
    wheel_file = Path(sys.argv[1])
    if not wheel_file.exists():
        print(f"Error: Wheel file {wheel_file} does not exist")
        sys.exit(1)
    add_wheel_to_pip_cache(wheel_file)
