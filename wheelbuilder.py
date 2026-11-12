#!/data/data/com.termux/files/usr/bin/env python
"""Build Python wheels from package sources."""

import argparse
from multiprocessing import Pool, cpu_count
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Optional

from loguru import logger

LOCAL_WHEELS: Path = Path("/sdcard/whl")


def normalize(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def source_pkg_name(source_name: str) -> str:
    stem = source_name
    stem = stem.removesuffix(".tar.gz")
    if "-" in stem:
        return stem.rsplit("-", 1)[0]
    return stem


def is_already_built(wheels_dir: Path, source_name: str) -> bool:
    if not wheels_dir.exists():
        return False
    target = normalize(source_pkg_name(source_name))
    return any(normalize(whl.name.split("-")[0]) == target for whl in wheels_dir.glob("*.whl"))


def has_rust_backend(pkg_dir: Path) -> bool:
    cargo_toml = pkg_dir / "Cargo.toml"
    if cargo_toml.exists():
        logger.info(f"  -> Rust backend detected in {pkg_dir.name}")
        return True
    return False


def cleanup_build_artifacts(pkg_dir: Path) -> None:
    dirs_to_remove = [
        pkg_dir / "build",
        pkg_dir / "dist",
        pkg_dir / "*.dist-info",
        pkg_dir / "__pycache__",
        pkg_dir / ".eggs",
    ]
    for pattern in dirs_to_remove:
        if "*" in pattern.name:
            for item in pkg_dir.glob(pattern.name):
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                        logger.debug(f"  -> Removed {item.name}")
                except Exception as e:
                    logger.warning(f"  ! Failed to remove {item.name}: {e}")
        elif pattern.exists():
            try:
                shutil.rmtree(pattern)
                logger.debug(f"  -> Removed {pattern.name}")
            except Exception as e:
                logger.warning(f"  ! Failed to remove {pattern.name}: {e}")


def pip_wheel_cmd(target: Path, wheels_dir: Path) -> list:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-cache-dir",
        "--no-deps",
        "--prefer-binary",
        "--find-links",
        str(LOCAL_WHEELS),
        "--wheel-dir",
        str(wheels_dir),
        str(target),
    ]
    return cmd


def build_wheel_from_dir(pkg_dir: Path) -> tuple[Path, Optional[str]]:
    logger.info(f"Processing: {pkg_dir.name}")
    wheels_dir = Path.cwd() / "wheels"
    try:
        if is_already_built(wheels_dir, pkg_dir.name):
            logger.info(f"  ~ Skipping {pkg_dir.name} (already built)")
            return pkg_dir, "Skipped (already built)"
        has_setup = any([
            (pkg_dir / "setup.py").exists(),
            (pkg_dir / "setup.cfg").exists(),
            (pkg_dir / "pyproject.toml").exists(),
        ])
        if not has_setup:
            return pkg_dir, "No setup.py/setup.cfg/pyproject.toml found"
        if has_rust_backend(pkg_dir):
            return pkg_dir, "Skipped (Rust backend detected)"
        logger.info(f"  -> Cleaning previous build artifacts...")
        cleanup_build_artifacts(pkg_dir)
        logger.info(f"  -> Building wheel for {pkg_dir.name}...")
        result = subprocess.run(
            pip_wheel_cmd(pkg_dir, wheels_dir),
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(pkg_dir),
        )
        if result.returncode == 0:
            logger.info(f"  OK Successfully built wheel for {pkg_dir.name}")
            cleanup_build_artifacts(pkg_dir)
            return pkg_dir, None
        else:
            error_msg = result.stderr or result.stdout
            return pkg_dir, f"Build failed: {error_msg[:150]}"
    except subprocess.TimeoutExpired:
        return pkg_dir, "Build timeout (>300s)"
    except Exception as e:
        return pkg_dir, f"Error: {e!s}"


def build_wheel_from_tar_gz(tar_gz_file: Path) -> tuple[Path, Optional[str]]:
    logger.info(f"Processing: {tar_gz_file.name}")
    wheels_dir = Path.cwd() / "wheels"
    if is_already_built(wheels_dir, tar_gz_file.name):
        logger.info(f"  ~ Skipping {tar_gz_file.name} (already built)")
        return tar_gz_file, "Skipped (already built)"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        try:
            logger.info(f"  -> Extracting {tar_gz_file.name}...")
            with tarfile.open(tar_gz_file, "r:gz") as tar:
                tar.extractall(path=temp_path)
            extracted_items = list(temp_path.iterdir())
            if not extracted_items:
                return tar_gz_file, f"No files extracted from {tar_gz_file.name}"
            pkg_dir = extracted_items[0]
            if pkg_dir.is_file():
                pkg_dir = temp_path
            if has_rust_backend(pkg_dir):
                return tar_gz_file, "Skipped (Rust backend detected)"
            logger.info(f"  -> Building wheel for {tar_gz_file.name}...")
            result = subprocess.run(
                pip_wheel_cmd(pkg_dir, wheels_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                logger.info(f"  OK Successfully built wheel for {tar_gz_file.name}")
                return tar_gz_file, None
            else:
                error_msg = result.stderr or result.stdout
                return tar_gz_file, f"Build failed: {error_msg[:150]}"
        except tarfile.TarError as e:
            return tar_gz_file, f"Extraction failed: {e!s}"
        except subprocess.TimeoutExpired:
            return tar_gz_file, "Build timeout (>300s)"
        except Exception as e:
            return tar_gz_file, f"Error: {e!s}"


def is_valid_package_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    has_metadata = any([
        (path / "setup.py").exists(),
        (path / "setup.cfg").exists(),
        (path / "pyproject.toml").exists(),
        (path / "PKG-INFO").exists(),
    ])
    return has_metadata


def build_from_directories() -> None:
    current_dir = Path.cwd()
    pkg_dirs = sorted([d for d in current_dir.iterdir() if is_valid_package_dir(d)])
    if not pkg_dirs:
        logger.warning("No valid Python package directories found in current directory")
        logger.info("Looking for directories with setup.py, setup.cfg, or pyproject.toml")
        return
    logger.info(f"Found {len(pkg_dirs)} package directory/directories to process")
    for pkg_dir in pkg_dirs:
        logger.info(f"  - {pkg_dir.name}")
    wheels_dir = current_dir / "wheels"
    wheels_dir.mkdir(exist_ok=True)
    logger.info(f"\nWheels will be saved to: {wheels_dir}\n")
    logger.info(f"Using local wheel cache: {LOCAL_WHEELS}\n")
    num_workers = min(cpu_count() - 1, len(pkg_dirs)) or 1
    logger.info(f"Using {num_workers} worker(s) for parallel builds\n")
    try:
        with Pool(processes=num_workers) as pool:
            results = pool.map(build_wheel_from_dir, pkg_dirs)
        logger.info("\n" + "=" * 70)
        logger.info("BUILD SUMMARY")
        logger.info("=" * 70)
        successful = 0
        skipped = 0
        failed = 0
        for pkg_dir, error in results:
            if error is None:
                successful += 1
                logger.info(f"OK {pkg_dir.name}: SUCCESS")
            elif "Skipped" in error:
                skipped += 1
                logger.info(f"-- {pkg_dir.name}: {error}")
            else:
                failed += 1
                logger.error(f"XX {pkg_dir.name}: {error}")
        logger.info("=" * 70)
        logger.info(f"Results: {successful} successful, {skipped} skipped, {failed} failed")
        logger.info("=" * 70)
    except Exception as e:
        logger.error(f"Multiprocessing failed: {e}")
        return
    wheels = list(wheels_dir.glob("*.whl"))
    if wheels:
        logger.info(f"\nBuilt wheels ({len(wheels)}):")
        for wheel in sorted(wheels):
            size_mb = wheel.stat().st_size / (1024 * 1024)
            logger.info(f"  - {wheel.name} ({size_mb:.2f} MB)")
    else:
        logger.warning("No wheels were built")


def build_from_tar_gz() -> None:
    current_dir = Path.cwd()
    tar_gz_files = sorted(current_dir.glob("*.tar.gz"))
    if not tar_gz_files:
        logger.warning("No .tar.gz files found in current directory")
        return
    logger.info(f"Found {len(tar_gz_files)} package(s) to process")
    for tar_file in tar_gz_files:
        logger.info(f"  - {tar_file.name}")
    wheels_dir = current_dir / "wheels"
    wheels_dir.mkdir(exist_ok=True)
    logger.info(f"\nWheels will be saved to: {wheels_dir}\n")
    logger.info(f"Using local wheel cache: {LOCAL_WHEELS}\n")
    num_workers = min(cpu_count() - 1, len(tar_gz_files)) or 1
    logger.info(f"Using {num_workers} worker(s) for parallel builds\n")
    try:
        with Pool(processes=num_workers) as pool:
            results = pool.map(build_wheel_from_tar_gz, tar_gz_files)
        logger.info("\n" + "=" * 70)
        logger.info("BUILD SUMMARY")
        logger.info("=" * 70)
        successful = 0
        skipped = 0
        failed = 0
        for tar_file, error in results:
            if error is None:
                successful += 1
                logger.info(f"OK {tar_file.name}: SUCCESS")
            elif "Skipped" in error:
                skipped += 1
                logger.info(f"-- {tar_file.name}: {error}")
            else:
                failed += 1
                logger.error(f"XX {tar_file.name}: {error}")
        logger.info("=" * 70)
        logger.info(f"Results: {successful} successful, {skipped} skipped, {failed} failed")
        logger.info("=" * 70)
    except Exception as e:
        logger.error(f"Multiprocessing failed: {e}")
        return
    wheels = list(wheels_dir.glob("*.whl"))
    if wheels:
        logger.info(f"\nBuilt wheels ({len(wheels)}):")
        for wheel in sorted(wheels):
            size_mb = wheel.stat().st_size / (1024 * 1024)
            logger.info(f"  - {wheel.name} ({size_mb:.2f} MB)")
    else:
        logger.warning("No wheels were built")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Python wheels from package sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python build_wheels.py          # Build from extracted subdirectories
  python build_wheels.py -g       # Build from .tar.gz files
        """,
    )
    parser.add_argument(
        "-g",
        "--gzip",
        action="store_true",
        help="Build from .tar.gz files instead of extracted subdirectories",
    )
    args = parser.parse_args()
    logger.info("=" * 70)
    if args.gzip:
        logger.info("Mode: Building from .tar.gz files")
        logger.info("=" * 70 + "\n")
        build_from_tar_gz()
    else:
        logger.info("Mode: Building from extracted subdirectories")
        logger.info("=" * 70 + "\n")
        build_from_directories()


if __name__ == "__main__":
    main()
