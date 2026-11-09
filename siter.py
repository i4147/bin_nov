#!/data/data/com.termux/files/usr/bin/env python
"""Rebuild wheel files from packages installed in site-packages."""


import argparse
import base64
import csv
import hashlib
from multiprocessing import cpu_count
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
from typing import Any, Iterable, Sequence

from dh import MAX_WORKERS


class WheelBuilder:
    def __init__(
        self,
        site_packages: Path,
        output_dir: Path,
        force_all = False,
        parallel = True,
        max_workers = None,
    ) -> None:
        self.site_packages = site_packages.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.force_all = force_all
        self.parallel = parallel
        self.max_workers = max_workers or min(cpu_count(), 8)
        self.venv_root = self._find_venv_root()
        self.bin_dir = self._find_bin_dir()
        self.share_dir = self.venv_root / "share" if self.venv_root else None
        self.processed_packages = set()

    def _find_venv_root(self):
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

    def _find_bin_dir(self):
        if not self.venv_root:
            return None
        for name in ["bin", "Scripts"]:
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
        return f"sha256={base64.urlsafe_b64encode(digest).decode().rstrip('=')} "

    def _read_record(self, dist_info: str):
        record_file = dist_info / "RECORD"
        if not record_file.exists():
            return {}
        records = {}
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

    def _find_scripts_for_package(self, records: dict[str, Any]):
        if not self.bin_dir or not self.bin_dir.exists():
            return []
        scripts = []
        entry_points_file = None
        for path_str in records:
            if path_str.endswith("entry_points.txt"):
                entry_points_file = self.site_packages / path_str
                break
        if entry_points_file and entry_points_file.exists():
            script_names = set()
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
                for ext in ["", ".exe", ".bat", ".cmd"]:
                    script_path = self.bin_dir / f"{script_name}{ext}"
                    if script_path.exists() and script_path.is_file():
                        scripts.append(script_path)
                        break
        return scripts

    def _find_data_for_package(self, package_name: str):
        if not self.share_dir or not self.share_dir.exists():
            return []
        data_files = []
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

    def _get_wheel_tags(self):
        try:
            from packaging.tags import sys_tags

            tag = next(sys_tags())
            return (tag.interpreter, tag.abi, tag.platform)
        except ImportError:
            import platform

            py_ver = sys.version_info
            python_tag = f"cp{py_ver.major}{py_ver.minor}"
            abi_tag = python_tag
            plat = platform.system().lower()
            machine = platform.machine().lower()
            platform_tag = f"{plat}_{machine}"
            return (python_tag, abi_tag, platform_tag)

    def _detect_purity(self, records: dict[str, Any]) -> bool:
        return all(not path.endswith((".so", ".pyd", ".dll")) for path in records)

    def build_wheel(self, dist_info_dir: Path):
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
        if wheel_path.exists() and (not self.force_all):
            print(f"  ⏭️  Skipping {wheel_path.name} (already exists)")
            return wheel_path
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                dist_info_name = f"{pkg_name}-{version}.dist-info"
                dist_info_dest = tmp_path / dist_info_name
                dist_info_dest.mkdir(parents=True)
                data_dir_name = f"{pkg_name}-{version}.data"
                data_dir = None
                new_record = []
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
                    file_size = dest.stat().st_size
                    new_record.append((str(rel_path), file_hash, str(file_size)))
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
                        file_size = dest.stat().st_size
                        new_record.append((str(rel_path), file_hash, str(file_size)))
                if data_files:
                    if not data_dir:
                        data_dir = tmp_path / data_dir_name
                    data_data_dir = data_dir / "data"
                    data_data_dir.mkdir(parents=True, exist_ok=True)
                    for src, rel_data_path in data_files:
                        dest = data_data_dir / rel_data_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(src, dest)
                        rel_path = dest.relative_to(tmp_path)
                        file_hash = self._compute_hash(dest)
                        file_size = dest.stat().st_size
                        new_record.append((str(rel_path), file_hash, str(file_size)))
                wheel_file = dist_info_dest / "WHEEL"
                with wheel_file.open("w", encoding="utf-8") as f:
                    f.write("Wheel-Version: 1.0\n")
                    f.write("Generator: wheel-builder 1.0\n")
                    f.write(f"Root-Is-Purelib: {('true' if is_pure else 'false')}\n")
                    f.write(f"Tag: {python_tag}-{abi_tag}-{platform_tag}\n")
                rel_path = wheel_file.relative_to(tmp_path)
                file_hash = self._compute_hash(wheel_file)
                file_size = wheel_file.stat().st_size
                new_record.append((str(rel_path), file_hash, str(file_size)))
                record_file = dist_info_dest / "RECORD"
                with record_file.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    for row in new_record:
                        writer.writerow(row)
                    writer.writerow([f"{dist_info_name}/RECORD", "", ""])
                with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
                    for file in tmp_path.rglob("*"):
                        if file.is_file():
                            arcname = file.relative_to(tmp_path)
                            whl.write(file, arcname)
            print(f"  ✅ Created: {wheel_path.name}")
            return wheel_path
        except Exception as e:
            print(f"  ❌ Failed to build {pkg_name}: {e}")
            return None

    def _build_wheel_worker(self, dist_info: Any):
        try:
            result = self.build_wheel(dist_info)
            return (dist_info.name, result)
        except Exception as e:
            print(f"❌ Worker failed for {dist_info.name}: {e}")
            return (dist_info.name, None)

    def build_all(self) -> int:
        dist_infos = sorted(self.site_packages.glob("*.dist-info"))
        if not dist_infos:
            print(f"❌ No packages found in {self.site_packages}")
            return 0
        print(f"📂 Found {len(dist_infos)} package(s) in {self.site_packages}")
        if self.venv_root:
            print(f"🐍 Virtual environment: {self.venv_root}")
        if self.parallel and len(dist_infos) > 1:
            print(f"🚀 Using parallel processing with {self.max_workers} workers")
            return self._build_parallel(dist_infos)
        else:
            if len(dist_infos) > 1:
                print("📝 Using serial processing")
            return self._build_serial(dist_infos)

    def _build_serial(self, dist_infos: Iterable[Any]) -> int:
        built = 0
        for dist_info in dist_infos:
            try:
                if self.build_wheel(dist_info):
                    built += 1
            except Exception as e:
                print(f"  ❌ Failed to build {dist_info.name}: {e}")
        print(f"\n✅ Built {built}/{len(dist_infos)} wheels in {self.output_dir}")
        return built

    def _build_parallel(self, dist_infos: Sequence[Any]):
        self.processed_packages.clear()
        from multiprocessing import get_context

        ctx = get_context("spawn")
        build_func = self._build_wheel_worker
        with ctx.Pool(processes=self.max_workers) as pool:
            results = pool.map(build_func, dist_infos)
        built = sum((1 for _, result in results if result is not None))
        print(f"\n✅ Built {built}/{len(dist_infos)} wheels in {self.output_dir}")
        return built


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build proper wheel files from installed packages (run from site-packages)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\nExamples:\n  # Run from inside site-packages directory:\n  cd /path/to/venv/lib/python3.x/site-packages\n  python /path/to/script.py\n\n  # Force rebuild all packages with parallel processing (default)\n  python /path/to/script.py -a\n\n  # Build with serial processing (no parallel)\n  python /path/to/script.py --no-parallel\n\n  # Build specific package\n  python /path/to/script.py -p requests\n\n  # Force rebuild specific package\n  python /path/to/script.py -a -p requests\n\n  # Specify number of workers\n  python /path/to/script.py -a -w 4\n\n  # Specify output directory\n  python /path/to/script.py -o /path/to/wheels\n        ",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("./wheels"),
        help="Output directory for wheels (default: ./wheels)",
    )
    parser.add_argument("--package", "-p", help="Build only this package (by name)")
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Repack all packages (overwrite existing wheels)",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel processing (use serial mode)",
    )
    parser.add_argument(
        "--site-packages",
        "-s",
        type=Path,
        help="Path to site-packages directory (default: current directory)",
    )
    args = parser.parse_args()
    if args.site_packages:
        site_packages = args.site_packages.resolve()
    else:
        site_packages = Path.cwd()
    if not site_packages.exists():
        print(f"❌ Site-packages not found: {site_packages}")
        return 1
    dist_infos = list(site_packages.glob("*.dist-info"))
    if not dist_infos:
        print(f"⚠️  No .dist-info directories found in {site_packages}")
        print("   Make sure you're running this script from a site-packages directory")
        response = input("   Continue anyway? [y/N]: ")
        if response.lower() != "y":
            return 1
    print(f"📂 Using site-packages: {site_packages}")
    parallel = not args.no_parallel
    builder = WheelBuilder(
        site_packages,
        args.output,
        args.all,
        parallel=parallel,
        max_workers=MAX_WORKERS,
    )
    if args.package:
        matches = list(site_packages.glob(f"{args.package}*.dist-info"))
        if not matches:
            print(f"❌ Package not found: {args.package}")
            return 1
        if len(matches) > 1:
            print(f"📦 Multiple matches for '{args.package}':")
            for m in matches:
                print(f"  - {m.name}")
            print("Building all matches...")
        builder.parallel = False
        built = 0
        for dist_info in matches:
            if builder.build_wheel(dist_info):
                built += 1
        return 0 if built > 0 else 1
    built = builder.build_all()
    return 0 if built > 0 else 1


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    raise SystemExit(main())
