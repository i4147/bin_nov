#!/data/data/com.termux/files/usr/bin/env python
"""Repackage installed apt packages as ``.deb`` files.
Uses python-apt to list the installed packages and their file lists, rebuilds a ``control`` file and a ``data.tar.xz`` from the files on disk and assembles a real ``.deb`` with ``unix_ar`` under ``~/debs``.
Usage: mkdebs [packages ...] Examples: $ mkdebs bash curl Runs as a script (``python mkdebs.py``) in the directory it should act on; the module can also be imported.
"""

import contextlib
import os
from pathlib import Path
import shutil
import tarfile

import apt
import apt_pkg
import unix_ar

BASE_DIR = Path.home() / "debs"
BASE_DIR.mkdir(parents=True, exist_ok=True)
apt_pkg.init_system()


def get_installed_packages() -> list[str]:
    cache = apt.Cache()
    return [pkg.name for pkg in cache if pkg.is_installed]


def get_package_files(pkg_name: str) -> list[str]:
    try:
        dpkg_info_dir = Path("/data/data/com.termux/files/usr/var/lib/dpkg/info")
        list_file = dpkg_info_dir / f"{pkg_name}.list"
        if not list_file.exists():
            return []
        files = list_file.read_text().splitlines()
        return [f for f in files if Path(f).exists()]
    except Exception:
        return []


def get_package_metadata(pkg_name: str) -> dict[str, str]:
    cache = apt.Cache()
    if pkg_name not in cache:
        msg = f"Package {pkg_name} not found"
        raise ValueError(msg)
    pkg = cache[pkg_name]
    if pkg.is_installed:
        version = pkg.installed.version
    else:
        version = pkg.candidate.version
    architecture = pkg.architecture or "all"
    try:
        if hasattr(pkg, "description"):
            description = pkg.description
        elif hasattr(pkg, "candidate") and hasattr(pkg.candidate, "description"):
            description = pkg.candidate.description
        else:
            status_file = Path("/data/data/com.termux/files/usr/var/lib/dpkg/status")
            if status_file.exists():
                content = status_file.read_text()
                sections = content.split("\n\n")
                for section in sections:
                    if f"Package: {pkg_name}" in section:
                        lines = section.split("\n")
                        for line in lines:
                            if line.startswith("Description:"):
                                desc = line.replace("Description:", "").strip()
                                desc_lines = [desc]
                                idx = lines.index(line) + 1
                                while idx < len(lines) and lines[idx].startswith(" "):
                                    desc_lines.append(lines[idx].strip())
                                    idx += 1
                                description = " ".join(desc_lines)
                                break
                        break
                else:
                    description = "No description available"
            else:
                description = "No description available"
    except:
        description = "No description available"
    try:
        if hasattr(pkg, "maintainer"):
            maintainer = pkg.maintainer
        elif hasattr(pkg, "candidate") and hasattr(pkg.candidate, "maintainer"):
            maintainer = pkg.candidate.maintainer
        else:
            status_file = Path("/data/data/com.termux/files/usr/var/lib/dpkg/status")
            if status_file.exists():
                content = status_file.read_text()
                sections = content.split("\n\n")
                for section in sections:
                    if f"Package: {pkg_name}" in section:
                        lines = section.split("\n")
                        for line in lines:
                            if line.startswith("Maintainer:"):
                                maintainer = line.replace("Maintainer:", "").strip()
                                break
                        break
                else:
                    maintainer = "Unknown Maintainer"
            else:
                maintainer = "Unknown Maintainer"
    except:
        maintainer = "Unknown Maintainer"
    return {
        "Package": pkg_name,
        "Version": version,
        "Architecture": architecture,
        "Maintainer": maintainer,
        "Description": description.replace("\n", " ").strip(),
    }


def create_control_file(path: Path, meta: dict[str, str]) -> None:
    control_content = f"""Package: {meta["Package"]}
Version: {meta["Version"]}
Architecture: {meta["Architecture"]}
Maintainer: {meta["Maintainer"]}
Description: {meta["Description"]}
"""
    (path / "control").write_text(control_content)


def copy_pkg_files(files: list[str], dest: Path) -> None:
    for f in files:
        path = Path(f)
        if not path.is_file():
            continue
        target = dest / f.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, target)
            stat_info = os.stat(f)
            os.chmod(target, stat_info.st_mode)
        except (PermissionError, OSError, shutil.Error):
            try:
                content = path.read_bytes()
                target.write_bytes(content)
                with contextlib.suppress(BaseException):
                    os.chmod(target, path.stat().st_mode)
            except:
                pass


def build_tar_xz(source_dir: Path, output_path: Path) -> None:
    with tarfile.open(output_path, "w:xz") as tar:
        tar.add(source_dir, arcname=".")


def build_deb(pkg_dir: Path, output_deb: Path) -> None:
    debian_binary_content = b"2.0\n"
    control_tar_path = pkg_dir / "control.tar.xz"
    data_tar_path = pkg_dir / "data.tar.xz"
    build_tar_xz(pkg_dir / "DEBIAN", control_tar_path)
    build_tar_xz(pkg_dir / "files", data_tar_path)
    control_data = control_tar_path.read_bytes()
    data_data = data_tar_path.read_bytes()
    ar = unix_ar.open(str(output_deb), "w")
    try:
        ar.add_file("debian-binary", debian_binary_content)
        ar.add_file("control.tar.xz", control_data)
        ar.add_file("data.tar.xz", data_data)
    finally:
        ar.close()


def process_pkg(pkg_name: str) -> str | None:
    try:
        pkg_dir = BASE_DIR / pkg_name
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
        pkg_dir.mkdir()
        files_dir = pkg_dir / "files"
        debian_dir = pkg_dir / "DEBIAN"
        files_dir.mkdir()
        debian_dir.mkdir()
        meta = get_package_metadata(pkg_name)
        files = get_package_files(pkg_name)
        if not files:
            print(f"[!] No files found for {pkg_name}")
            return None
        copy_pkg_files(files, files_dir)
        create_control_file(debian_dir, meta)
        output_deb = BASE_DIR / f"{pkg_name}.deb"
        build_deb(pkg_dir, output_deb)
        print(f"[✔] {pkg_name} → {output_deb}")
        shutil.rmtree(pkg_dir)
        return str(output_deb)
    except Exception as e:
        print(f"[✖] {pkg_name} FAILED: {e}")
        return None


def main() -> None:
    import sys

    args = sys.argv[1:]
    pkgs = [p.strip() for p in args] if args else ["python", "mc", "python2"]
    print(f"[+] Building {len(pkgs)} packages\n")
    for pkg in pkgs:
        process_pkg(pkg)


if __name__ == "__main__":
    raise SystemExit(main())
