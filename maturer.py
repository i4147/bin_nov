#!/data/data/com.termux/files/usr/bin/env python
"""Build a Rust/maturin extension and repackage it as a wheel.
Runs ``cargo build --release``, computes the platform wheel tag (``cp3XX``/free-threaded, normalised for 32-bit ARM) and packs the shared library with the expected ``.dist-info`` metadata into ``<name>-<version>-<tag>.whl``.
Usage: maturer Examples: $ maturer Run from the crate root; it needs ``Cargo.toml`` and a working cargo toolchain."""

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile


def get_cargo_metadata():
    with open("Cargo.toml", "rb") as f:
        cargo_data = tomllib.load(f)
    package = cargo_data.get("package", {})
    name = package.get("name", "").replace("-", "_")
    version = package.get("version", "0.1.0")
    if not name:
        print("[-] Error: Could not parse package name from Cargo.toml", file=sys.stderr)
        sys.exit(1)
    return name, version


def get_wheel_tags():
    import sysconfig

    py_version = f"{sys.version_info.major}{sys.version_info.minor}"
    is_freethreaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    py_tag = f"cp{py_version}{'t' if is_freethreaded else ''}"
    abi_tag = py_tag
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    if "linux" in platform and ("armv8l" in platform or "armv7l" in platform):
        platform = "linux_armv7l"
    return py_tag, abi_tag, platform


def build_cargo():
    print("[+] Compiling Rust crate via Cargo...")
    result = subprocess.run(["cargo", "build", "--release"], check=True)
    if result.returncode != 0:
        print("[-] Cargo compilation failed.", file=sys.stderr)
        sys.exit(1)


def create_wheel():
    pkg_name, version = get_cargo_metadata()
    py_tag, abi_tag, plat_tag = get_wheel_tags()
    wheel_stem = f"{pkg_name}-{version}-{py_tag}-{abi_tag}-{plat_tag}"
    dist_info_dir = f"{pkg_name}-{version}.dist-info"
    so_source = Path("target") / "release" / f"lib{pkg_name}.so"
    if not so_source.exists():
        so_source = Path("target") / "release" / f"{pkg_name}.so"
    if not so_source.exists():
        print(f"[-] Shared library not found at: {so_source}. Ensure crate-type is ['cdylib'].", file=sys.stderr)
        sys.exit(1)
    so_target_name = f"{pkg_name}.{py_tag}-{plat_tag}.so"
    build_dir = Path("target") / "wheel_scratch"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(so_source, build_dir / so_target_name)
    dist_info_path = build_dir / dist_info_dir
    dist_info_path.mkdir(exist_ok=True)
    wheel_metadata = (
        f"Wheel-Version: 1.0\n"
        f"Generator: custom_cargo_wrapper (1.0.0)\n"
        f"Root-Is-Purelib: false\n"
        f"Tag: {py_tag}-{abi_tag}-{plat_tag}\n"
    )
    (dist_info_path / "WHEEL").write_text(wheel_metadata)
    metadata_content = (
        f"Metadata-Version: 2.1\n"
        f"Name: {pkg_name.replace('_', '-')}\n"
        f"Version: {version}\n"
        f"Summary: Manually compiled rust extension module.\n"
    )
    (dist_info_path / "METADATA").write_text(metadata_content)
    record_entries = []

    def add_to_record(file_path: Path):
        rel_path = file_path.relative_to(build_dir)
        content = file_path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("utf-8").rstrip("=")
        record_entries.append(f"{rel_path},sha256={digest},{len(content)}")

    add_to_record(build_dir / so_target_name)
    add_to_record(dist_info_path / "WHEEL")
    add_to_record(dist_info_path / "METADATA")
    record_entries.append(f"{dist_info_dir}/RECORD,,")
    (dist_info_path / "RECORD").write_text("\n".join(record_entries) + "\n")
    dist_out = Path("target") / "wheels"
    dist_out.mkdir(exist_ok=True)
    wheel_file_path = dist_out / f"{wheel_stem}.whl"
    print(f"[+] Packing files into wheel archive: {wheel_file_path}")
    with zipfile.ZipFile(wheel_file_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(build_dir):
            for file in files:
                full_path = Path(root) / file
                archive_name = full_path.relative_to(build_dir)
                zinfo = zipfile.ZipInfo(str(archive_name))
                if full_path.suffix == ".so":
                    zinfo.external_attr = 0o755 << 16
                else:
                    zinfo.external_attr = 0o644 << 16
                zipf.writestr(zinfo, full_path.read_bytes())
    print(f"[+] Wheel successfully generated!")


if __name__ == "__main__":
    build_cargo()
    create_wheel()
