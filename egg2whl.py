#!/data/data/com.termux/files/usr/bin/env python
"""Convert an unpacked ``.egg`` directory into a wheel.
Reads ``PKG-INFO`` for the distribution name and version, writes a matching ``setup.py`` into a temporary copy of the egg and calls ``pip wheel`` on it, leaving the resulting ``.whl`` in the current directory.
Usage: egg2whl EGG_DIRECTORY Examples: $ egg2whl ./SomePackage-1.0-py3.9.egg Runs as a script (``python egg2whl.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def parse_pkg_info(egg_info_dir):
    pkg_info_path = egg_info_dir / "PKG-INFO"
    metadata = {}
    if pkg_info_path.exists():
        with Path(pkg_info_path).open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip()] = value.strip()
    return metadata


def create_setup_py(egg_root: Path, temp_dir: Path):
    egg_info_dir = egg_root / "EGG-INFO"
    egg_root / "setupmeta"
    metadata = parse_pkg_info(egg_info_dir)
    setup_py_content = """from setuptools import setup, find_packages
setup(
    name="{name}",
    version="{version}",
    packages=find_packages(),
    package_data={{}},
    include_package_data=True,
    zip_safe=False,
"""
    entry_points_path = egg_info_dir / "entry_points.txt"
    entry_points = ""
    if entry_points_path.exists():
        entry_points = Path(entry_points_path).read_text(encoding="utf-8")
        setup_py_content += f"    entry_points={{\n{entry_points}}},\n"
    setup_py_content += ")"
    name = metadata.get("Name", "unknown")
    version = metadata.get("Version", "0.0.0")
    setup_py_content = setup_py_content.format(name=name, version=version)
    setup_py_path = temp_dir / "setup.py"
    Path(setup_py_path).write_text(setup_py_content, encoding="utf-8")
    return name, version


def convert_egg_to_wheel(egg_root_path: str) -> str | None:
    egg_root = Path(egg_root_path)
    if not egg_root.exists():
        msg = f"Egg directory not found: {egg_root}"
        raise FileNotFoundError(msg)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        standard_egg_info = temp_path / f"{egg_root.name}.egg-info"
        shutil.copytree(egg_root / "EGG-INFO", standard_egg_info)
        setupmeta_src = temp_path / "setupmeta"
        shutil.copytree(egg_root / "setupmeta", setupmeta_src)
        _name, _version = create_setup_py(egg_root, temp_path)
        cmd = [
            sys.executable,
            "setup.py",
            "bdist_wheel",
            "--dist-dir",
            str(temp_path / "dist"),
        ]
        result = subprocess.run(cmd, cwd=temp_path, capture_output=True, text=True)
        if result.returncode != 0:
            print("Build errors:", result.stderr)
            return None
        wheel_files = list((temp_path / "dist").glob("*.whl"))
        if wheel_files:
            wheel_path = wheel_files[0]
            print(f"Wheel created: {wheel_path}")
            return str(wheel_path)
    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python convert_egg.py <egg_directory>")
        sys.exit(1)
    egg_dir = sys.argv[1]
    wheel_path = convert_egg_to_wheel(egg_dir)
    if wheel_path:
        print(f"✓ Successfully converted to wheel: {wheel_path}")
    else:
        print("✗ Conversion failed")
        sys.exit(1)
