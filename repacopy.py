#!/data/data/com.termux/files/usr/bin/env python
"""Copy the installed packages into a wheel-like directory structure.
Finds the packages of the active environment (or of the local scan, ``--skip-scan``), copies their files plus the dist-info metadata into ``~/tmp/repack`` and reports the packages it could not find.
Usage: repacopy [-o OUTDIR] [-v] [--skip-scan] Options: --output, -o Output directory (default: ~/tmp/repack) --verbose, -v Enable verbose output --skip-scan Skip local scan and use current active environment only Examples: $ repacopy -o ~/tmp/repack Command-line script: run ``python repacopy.py`` or the ``repacopy`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import shutil
import sys

from loguru import logger


class PackageRepacker:
    def __init__(self, output_base: str = "~/tmp/repack") -> None:
        self.output_base = Path(output_base).expanduser()
        self.output_base.mkdir(parents=True, exist_ok=True)
        self.found_site_packages: list[Path] = []

    def find_site_packages_dirs(self) -> list[Path]:
        site_packages_dirs = []
        search_paths = [Path.cwd()]
        venv_patterns = [".venv", "venv", "env", "virtualenv"]
        for search_path in search_paths:
            if not search_path.exists():
                continue
            try:
                for pattern in venv_patterns:
                    for venv_dir in search_path.rglob(pattern):
                        if venv_dir.is_dir():
                            possible_paths = [
                                venv_dir / "lib" / "python*" / "site-packages",
                                venv_dir / "lib" / "site-packages",
                                venv_dir / "Lib" / "site-packages",
                            ]
                            for possible_path in possible_paths:
                                for site_pkg in possible_path.parent.glob(possible_path.name):
                                    if site_pkg.exists() and site_pkg.is_dir():
                                        if site_pkg not in site_packages_dirs:
                                            site_packages_dirs.append(site_pkg)
                                            print(
                                                "Found virtualenv site-packages: %s",
                                                site_pkg,
                                            )
                for site_pkg in search_path.rglob("site-packages"):
                    if site_pkg.is_dir() and site_pkg not in site_packages_dirs:
                        site_packages_dirs.append(site_pkg)
                        print("Found site-packages: %s", site_pkg)
                for dist_pkg in search_path.rglob("dist-packages"):
                    if dist_pkg.is_dir() and dist_pkg not in site_packages_dirs:
                        site_packages_dirs.append(dist_pkg)
                        print("Found dist-packages: %s", dist_pkg)
            except (PermissionError, OSError) as e:
                logger.debug("Permission denied scanning {}: {}", search_path, e)
        unique_dirs = list(set(site_packages_dirs))
        unique_dirs.sort()
        self.found_site_packages = unique_dirs
        return unique_dirs

    def get_package_info_from_dist_info(self, dist_info_dir: Path) -> dict:
        metadata = {
            "name": dist_info_dir.name.split("-")[0],
            "version": None,
            "requires_python": None,
            "dependencies": [],
        }
        parts = dist_info_dir.stem.split("-")
        if len(parts) >= 2:
            metadata["version"] = parts[-1]
        metadata_file = dist_info_dir / "METADATA"
        if metadata_file.exists():
            try:
                with Path(metadata_file).open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("Version:"):
                            metadata["version"] = line.split(":", 1)[1].strip()
                        elif line.startswith("Requires-Python:"):
                            metadata["requires_python"] = line.split(":", 1)[1].strip()
                        elif line.startswith("Requires-Dist:"):
                            dep = line.split(":", 1)[1].strip()
                            metadata["dependencies"].append(dep)
            except Exception as e:
                logger.warning("Could not read metadata from {}: {}", metadata_file, e)
        return metadata

    def create_wheel_structure(
        self,
        package_name: str,
        metadata: dict,
        files: list[Path],
        site_packages_path: Path,
        base_output_dir: Path,
        original_dist_info_dir: Path,
        is_pure_python: bool,
    ) -> Path | None:
        version = metadata["version"] or "0.0.0"
        if is_pure_python:
            python_tag = "py3"
            abi_tag = "none"
            platform_tag = "any"
            root_is_purelib = "true"
        else:
            print(
                "Detected C extensions for %s; generating platform-specific tags.",
                package_name,
            )
            root_is_purelib = "false"
            try:
                from packaging.tags import sys_tags

                best_tag = next(sys_tags())
                python_tag = best_tag.interpreter
                abi_tag = best_tag.abi
                platform_tag = best_tag.platform
                logger.debug(
                    "Using 'packaging' lib. Tags: {}-{}-{}",
                    python_tag,
                    abi_tag,
                    platform_tag,
                )
            except ImportError:
                logger.warning("`packaging` library not found. (Install with: pip install packaging)")
                logger.warning("Falling back to best-guess tags based on current system.")
                import platform

                python_ver = sys.version_info
                python_tag = f"cp{python_ver.major}{python_ver.minor}"
                abi_tag = python_tag
                platform_tag = f"{platform.system().lower()} _{platform.machine()} "
                logger.debug("Fallback tags: {}-{}-{}", python_tag, abi_tag, platform_tag)
        wheel_name = f"{package_name.replace('-', '_')} -{version} -{python_tag} -{abi_tag} -{platform_tag} "
        wheel_dir = base_output_dir / wheel_name
        dist_info_dir = wheel_dir / f"{package_name}-{version}.dist-info"
        wheel_dir / f"{package_name}-{version}.data"
        dist_info_dir.mkdir(parents=True, exist_ok=True)
        for path in files:
            relative_path = path
            target_path: Path
            if ".dist-info" in str(relative_path):
                target_path = dist_info_dir / relative_path.name
            else:
                target_path = wheel_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = site_packages_path / path
            if source_path.exists():
                shutil.copy2(source_path, target_path)
            else:
                logger.warning("File from RECORD not found at {}", source_path)
        wheel_file = dist_info_dir / "WHEEL"
        with Path(wheel_file).open("w", encoding="utf-8") as f:
            f.write("Wheel-Version: 1.0\n")
            f.write("Generator: auto-repacker 1.0\n")
            f.write(f"Root-Is-Purelib: {root_is_purelib}\n")
            f.write(f"Tag: {python_tag}-{abi_tag}-{platform_tag}\n")
        original_metadata = original_dist_info_dir / "METADATA"
        new_metadata_path = dist_info_dir / "METADATA"
        if not new_metadata_path.exists() and original_metadata.exists():
            shutil.copy2(original_metadata, new_metadata_path)
        elif not new_metadata_path.exists():
            with Path(new_metadata_path).open("w", encoding="utf-8") as f:
                f.write("Metadata-Version: 2.1\n")
                f.write(f"Name: {package_name}\n")
                f.write(f"Version: {version}\n")
        original_record = original_dist_info_dir / "RECORD"
        if original_record.exists():
            shutil.copy2(original_record, dist_info_dir / "RECORD")
        else:
            logger.warning("Could not find original RECORD file at {}", original_record)
        return wheel_dir

    def copy_package_files(self, dist_info_dir: Path, site_packages_path: Path, output_dir: Path) -> bool:
        try:
            record_file = dist_info_dir / "RECORD"
            if not record_file.exists():
                logger.warning(f"Skipping {dist_info_dir.name}: RECORD file not found")
                return False
            metadata = self.get_package_info_from_dist_info(dist_info_dir)
            package_name = metadata["name"]
            if not package_name:
                logger.warning("Could not determine package name for {}", dist_info_dir)
                return False
            files_to_include = []
            is_pure_python = True
            with Path(record_file).open(encoding="utf-8") as f:
                for line in f:
                    path_str = line.split(",")[0].strip()
                    if path_str and not path_str.endswith(".dist-info/RECORD"):
                        if path_str.endswith(".so"):
                            is_pure_python = False
                        full_path = site_packages_path / path_str
                        if full_path.exists():
                            files_to_include.append(Path(path_str))
            if not files_to_include:
                logger.warning("No files found for package {}", package_name)
                return False
            package_structure_path = self.create_wheel_structure(
                package_name,
                metadata,
                files_to_include,
                site_packages_path,
                output_dir,
                dist_info_dir,
                is_pure_python,
            )
            if package_structure_path:
                print("Copied package files to: %s", package_structure_path)
                return True
            return False
        except Exception as e:
            logger.exception(f"Error copying files for {dist_info_dir.name}: {e}")
            return False

    def copy_all_packages(self) -> None:
        total_copied = 0
        for site_packages_dir in self.found_site_packages:
            print("Processing site-packages: %s", site_packages_dir)
            env_name = "local_env"
            try:
                if (
                    ".venv" in str(site_packages_dir)
                    or "venv" in str(site_packages_dir)
                    or "env" in str(site_packages_dir)
                ):
                    env_name = site_packages_dir.parent.parent.name
            except Exception:
                pass
            output_dir = self.output_base / env_name
            output_dir.mkdir(parents=True, exist_ok=True)
            dist_info_dirs = list(site_packages_dir.glob("*.dist-info"))
            package_count = 0
            for dist_info_dir in dist_info_dirs:
                if dist_info_dir.is_dir() and self.copy_package_files(dist_info_dir, site_packages_dir, output_dir):
                    package_count += 1
                    total_copied += 1
            print("Copied %s packages from %s", package_count, site_packages_dir)
        print("Total packages copied: %s", total_copied)
        print(f"Package files saved to: {self.output_base}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatically find and copy Python packages to a wheel structure")
    parser.add_argument(
        "--output",
        "-o",
        default="~/tmp/repack",
        help="Output directory (default: ~/tmp/repack)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--skip-scan",
        action="store_true",
        help="Skip local scan and use current active environment only",
    )
    args = parser.parse_args()
    if args.verbose:
        pass
    try:
        repacker = PackageRepacker(output_base=args.output)
        if args.skip_scan:
            import site

            current_site_packages = site.getsitepackages()
            user_site = site.getusersitepackages()
            if user_site:
                current_site_packages.append(user_site)
            repacker.found_site_packages = [Path(p) for p in current_site_packages if Path(p).exists()]
            print(f"Using current active environment site-packages: {repacker.found_site_packages}")
        else:
            print("Scanning current directory for site-packages directories...")
            site_packages_dirs = repacker.find_site_packages_dirs()
            print(f"Found {len(site_packages_dirs)} site-packages directories")
        if not repacker.found_site_packages:
            logger.error("No site-packages directories found!")
            return 1
        repacker.copy_all_packages()
    except Exception as e:
        logger.exception("Fatal error: {}", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
