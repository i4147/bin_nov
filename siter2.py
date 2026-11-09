#!/data/data/com.termux/files/usr/bin/env python
"""Build wheel files from the site-packages of a virtual environment."""

import argparse
import base64
import csv
import hashlib
from multiprocessing import cpu_count
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from typing import Any, Optional
import zipfile

from dh import MAX_WORKERS


try:
    from packaging.tags import Tag, sys_tags
except ImportError:
    Tag = Any
    sys_tags = None


class WheelBuilder:
    def __init__(
        self,
        site_packages: str | Path,
        output_dir: str | Path,
        force_all: bool = False,
        parallel: bool = True,
        max_workers: Optional[int] = None,
    ) -> None:
        self.site_packages: Path = Path(site_packages).resolve()
        self.output_dir: Path = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.force_all: bool = force_all
        self.parallel: bool = parallel
        self.max_workers: int = max_workers or min(cpu_count(), 8)
        self.venv_root: Optional[Path] = self._find_venv_root()
        self.bin_dir: Optional[Path] = self._find_bin_dir()
        self.share_dir: Optional[Path] = self.venv_root / "share" if self.venv_root else None
        self.processed_packages: set[str] = set()

    def _find_venv_root(self) -> Optional[Path]:
        current = self.site_packages
        for _ in range(5):
            if (current / "pyvenv.cfg").exists():
                return current
            if (current / "bin" / "activate").exists():
                return current
            if (current / "Scripts" / "activate.bat").exists():
                return current
            current = current.parent
        return None

    def _find_bin_dir(self) -> Optional[Path]:
        if not self.venv_root:
            return None
        for name in ("bin", "Scripts"):
            d = self.venv_root / name
            if d.exists() and d.is_dir():
                return d
        return None

    def _compute_hash(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        digest = h.digest()
        return f"sha256={base64.urlsafe_b64encode(digest).decode().rstrip('=')}"

    def _read_record(self, dist_info: Path) -> dict[str, dict[str, str]]:
        record_file = dist_info / "RECORD"
        if not record_file.exists():
            return {}
        records: dict[str, dict[str, str]] = {}
        with record_file.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or not row[0]:
                    continue
                path = row[0]
                records[path] = {
                    "hash": row[1] if len(row) > 1 else "",
                    "size": row[2] if len(row) > 2 else "",
                }
        return records

    def _find_scripts_for_package(self, records: dict[str, dict[str, str]]) -> list[Path]:
        if not self.bin_dir or not self.bin_dir.exists():
            return []
        scripts: list[Path] = []
        entry_points_file: Optional[Path] = None
        for path_str in records:
            if path_str.endswith("entry_points.txt"):
                entry_points_file = self.site_packages / path_str
                break
        if not entry_points_file or not entry_points_file.exists():
            return scripts
        script_names: set[str] = set()
        in_console_scripts = False
        for line in entry_points_file.read_text().splitlines():
            line = line.strip()
            if line == "[console_scripts]":
                in_console_scripts = True
                continue
            if line.startswith("["):
                in_console_scripts = False
            elif in_console_scripts and "=" in line:
                script_name = line.split("=")[0].strip()
                script_names.add(script_name)
        for script_name in script_names:
            for ext in ("", ".exe", ".bat", ".cmd"):
                script_path = self.bin_dir / f"{script_name}{ext}"
                if script_path.exists() and script_path.is_file():
                    scripts.append(script_path)
                    break
        return scripts

    def _find_data_for_package(self, package_name: str) -> list[tuple[Path, str]]:
        if not self.share_dir or not self.share_dir.exists():
            return []
        data_files: list[tuple[Path, str]] = []
        pkg_normalized = package_name.lower().replace("-", "_")
        for item in self.share_dir.rglob("*"):
            if not item.is_file():
                continue
            if any(pkg_normalized in p.name.lower() for p in item.parents):
                try:
                    rel_path = item.relative_to(self.share_dir)
                    data_files.append((item, str(rel_path)))
                except ValueError:
                    pass
        return data_files

    def _get_wheel_tags(self) -> tuple[str, str, str]:
        if sys_tags is not None:
            try:
                tag: Tag = next(sys_tags())
                return (tag.interpreter, tag.abi, tag.platform)
            except StopIteration:
                pass
        py_ver = sys.version_info
        python_tag = f"cp{py_ver.major}{py_ver.minor}"
        abi_tag = python_tag
        plat = platform.system().lower()
        machine = platform.machine().lower()
        platform_tag = f"{plat}_{machine}"
        return (python_tag, abi_tag, platform_tag)

    def _detect_purity(self, records: dict[str, dict[str, str]]) -> bool:
        return all(not path.endswith((".so", ".pyd", ".dll")) for path in records)

    def build_wheel(self, dist_info_dir: Path) -> Optional[Path]:
        if not dist_info_dir.is_dir():
            return None
        parts = dist_info_dir.stem.split("-")
        if len(parts) < 2:
            print(f"⚠️  Invalid dist-info name: {dist_info_dir.name}")
            return None
        pkg_name = parts[0]
        version = parts[1] if len(parts) > 1 else "0.0.0"
        pkg_key = f"{pkg_name}-{version}"
        if pkg_key in self.processed_packages:
            return None
        self.processed_packages.add(pkg_key)
        print(f"📦 Building {pkg_name} {version}")
        records = self._read_record(dist_info_dir)
        if not records:
            print(f"  ⚠️  No RECORD file for {pkg_name}")
            return None
        is_pure = self._detect_purity(records)
        if is_pure:
            python_tag, abi_tag, platform_tag = ("py3", "none", "any")
        else:
            python_tag, abi_tag, platform_tag = self._get_wheel_tags()
        scripts = self._find_scripts_for_package(records)
        data_files = self._find_data_for_package(pkg_name)
        if scripts:
            print(f"  📎 Found {len(scripts)} script(s)")
        if data_files:
            print(f"  📁 Found {len(data_files)} data file(s)")
        wheel_name = f"{pkg_name.replace('-', '_')}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
        wheel_path = self.output_dir / wheel_name
        if wheel_path.exists() and not self.force_all:
            print(f"  ⏭️  Skipping {wheel_path.name} (already exists)")
            return wheel_path
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                dist_info_name = f"{pkg_name}-{version}.dist-info"
                dist_info_dest = tmp_path / dist_info_name
                dist_info_dest.mkdir(parents=True)
                data_dir_name = f"{pkg_name}-{version}.data"
                data_dir: Optional[Path] = None
                new_record: list[tuple[str, str, str]] = []
                for path_str in records:
                    src = self.site_packages / path_str
                    if not src.exists():
                        continue
                    if ".dist-info" in path_str:
                        dest = dist_info_dest / Path(path_str).name
                    else:
                        dest = tmp_path / path_str
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    rel_path = dest.relative_to(tmp_path)
                    file_hash = self._compute_hash(dest)
                    file_size = str(dest.stat().st_size)
                    new_record.append((str(rel_path), file_hash, file_size))
                if scripts:
                    data_dir = tmp_path / data_dir_name
                    scripts_dir = data_dir / "scripts"
                    scripts_dir.mkdir(parents=True, exist_ok=True)
                    for script in scripts:
                        if not script.exists():
                            continue
                        dest = scripts_dir / script.name
                        shutil.copy2(script, dest)
                        rel_path = dest.relative_to(tmp_path)
                        file_hash = self._compute_hash(dest)
                        file_size = str(dest.stat().st_size)
                        new_record.append((str(rel_path), file_hash, file_size))
                if data_files:
                    if not data_dir:
                        data_dir = tmp_path / data_dir_name
                    data_data_dir = data_dir / "data"
                    data_data_dir.mkdir(parents=True, exist_ok=True)
                    for src, rel_data_path in data_files:
                        dest = data_data_dir / rel_data_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        rel_path = dest.relative_to(tmp_path)
                        file_hash = self._compute_hash(dest)
                        file_size = str(dest.stat().st_size)
                        new_record.append((str(rel_path), file_hash, file_size))
                wheel_file = dist_info_dest / "WHEEL"
                with wheel_file.open("w", encoding="utf-8") as f:
                    f.write("Wheel-Version: 1.0\n")
                    f.write("Generator: wheel-builder 1.0\n")
                    f.write(f"Root-Is-Purelib: {'true' if is_pure else 'false'}\n")
                    f.write(f"Tag: {python_tag}-{abi_tag}-{platform_tag}\n")
                rel_path = wheel_file.relative_to(tmp_path)
                file_hash = self._compute_hash(wheel_file)
                file_size = str(wheel_file.stat().st_size)
                new_record.append((str(rel_path), file_hash, file_size))
                record_file = dist_info_dest / "RECORD"
                with record_file.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerows(new_record)
                    writer.writerow([str(record_file.relative_to(tmp_path)), "", ""])
                with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for file_path in tmp_path.rglob("*"):
                        if file_path.is_file():
                            arcname = file_path.relative_to(tmp_path)
                            zf.write(file_path, arcname)
            print(f"  ✅ Created {wheel_path.name}")
            return wheel_path
        except Exception as e:
            print(f"  ❌ Failed to build {pkg_name}: {e}")
            if wheel_path.exists():
                wheel_path.unlink(missing_ok=True)
            return None

    def build_all(self) -> list[Path]:
        wheels: list[Path] = []
        for dist_info in self.site_packages.glob("*.dist-info"):
            wheel = self.build_wheel(dist_info)
            if wheel:
                wheels.append(wheel)
        return wheels


def main() -> int:
    parser = argparse.ArgumentParser(description="Build wheel packages from a virtual environment's site-packages.")
    parser.add_argument(
        "site_packages",
        type=Path,
        help="Path to site-packages directory",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Directory to write wheel files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing wheels",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel builds",
    )
    args = parser.parse_args()
    builder = WheelBuilder(
        site_packages=args.site_packages,
        output_dir=args.output_dir,
        force_all=args.force,
        parallel=not args.no_parallel,
        max_workers=MAX_WORKERS,
    )
    wheels = builder.build_all()
    print(f"\n🎉 Built {len(wheels)} wheel(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
