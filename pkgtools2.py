#!/data/data/com.termux/files/usr/bin/env python
"""Debian/Termux package and system-binary toolkit.
Subcommands: ``check-system-bin`` compares the files with ``/system/bin``, ``missing-files`` audits the installed dpkg packages for missing files, ``copy-pkg-files`` copies the files of one package, ``orphan-libs-debian``/``orphan-pkgs-termux`` find the unused packages, ``list-installed-sizes`` ranks the packages by installed size and ``make-deb`` builds a ``.deb``.
Usage: pkgtools2 {check-system-bin,missing-files,copy-pkg-files,orphan-libs-debian,orphan-pkgs-termux,list-installed-sizes,make-deb} [options] Positional arguments: package packages ...
csvfile Subcommands: check-system-bin Scan /system/bin and move same-name matching files from CWD.
missing-files Audit installed dpkg packages for missing files.
copy-pkg-files Copy files belonging to one dpkg/rpm package.
orphan-libs-debian Find Debian orphan-ish library packages.
orphan-pkgs-termux Find Termux orphan packages.
list-installed-sizes List installed apt packages by installed size.
make-deb Create .deb files from apt cache for installed packages.
suggest-removals Suggest unused packages from bash history.
save-deb-names Save installed dpkg package names.
show-big-packages Scan apt packages by download size.
sort-csv Sort a package CSV by Installed-Size.
Options: --system-bin --work-dir --output-dir --chunk-size --report-style Simple = original check_system_missing_files.py output; audit = original chk_sys_missing.py output.
--filter-mode Parts = check_system_missing_files.py filter; substring = chk_sys_missing.py filter.
--timeout --output --missing-txt --dest-root ...
plus 16 more; run ``pkgtools2 --help`` for the full list.
Examples: $ pkgtools2 list-installed-sizes --exclude-substrings llvm $ pkgtools2 missing-files Command-line script: run ``python pkgtools2.py`` or the ``pkgtools2`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from datetime import datetime
import hashlib
import json
from multiprocessing import Pool, freeze_support
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger

DEFAULT_CHUNK_SIZE: int = 8192
DEFAULT_DEB_EXCLUDES = frozenset({
    "llvm",
    "clang",
    "libllvm",
    "libclang",
    "rust",
    "cargo",
    "lld",
    "lldb",
    "compiler-rt",
    "libc++",
    "libc++abi",
    "rust-stdlib",
    "rust-analyzer",
    "cargo-c",
})


def run_cmd(
    cmd: Sequence[str],
    *,
    timeout: Optional[float] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    value = float(num)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?)B?", re.IGNORECASE)


def parse_size(text: str) -> int:
    match = _SIZE_RE.match(text.strip())
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }
    return int(value * multipliers.get(unit, 1))


def sha256_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(chunk_size), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, PermissionError):
        return None


def ensure_parent(path: Path) -> None:
    if path.parent and path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)


def cmd_check_system_bin(args: argparse.Namespace) -> int:
    system_dir = Path(args.system_bin)
    if not system_dir.exists():
        print("⚠️  /system/bin directory not found!")
        return 1
    system_hashes: dict[str, str] = {}
    print("📂 Scanning /system/bin files...")
    for path in system_dir.iterdir():
        try:
            if path.is_file() or path.is_symlink():
                digest = sha256_file(path, args.chunk_size)
                if digest:
                    system_hashes[digest] = path.name
        except (PermissionError, OSError):
            continue
    print(f"✅ Scanned {len(system_hashes)} files in /system/bin\n")
    work_dir = Path(args.work_dir)
    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = work_dir / out_dir
    out_dir.mkdir(exist_ok=True)
    matches: list[tuple[str, str]] = []
    moved: list[tuple[str, str]] = []
    print("🔍 Scanning current directory...")
    for path in work_dir.iterdir():
        try:
            if path.is_file() and not path.name.startswith("."):
                digest = sha256_file(path, args.chunk_size)
                if digest and digest in system_hashes:
                    system_name = system_hashes[digest]
                    matches.append((path.name, system_name))
                    if path.name == system_name:
                        dest = out_dir / path.name
                        counter = 1
                        base = dest
                        while dest.exists():
                            dest = base.parent / f"{base.stem}_{counter}{base.suffix}"
                            counter += 1
                        shutil.move(str(path), str(dest))
                        moved.append((path.name, dest.name))
                        print(f"  📦 Moved: {path.name} -> {dest.name}")
                    else:
                        print(f"  ⚠️  Hash matches but filename differs: {path.name} (system: {system_name})")
        except (PermissionError, OSError) as exc:
            print(f"  ⚠️  Error with {path.name}: {exc}")
            continue
    print("\n" + "=" * 40)
    print("📊 SUMMARY")
    print("-" * 40)
    if matches:
        print(f"⚠️  Found {len(matches)} files with matching hashes:")
        for local_name, system_name in matches:
            status = "✅ MOVED" if local_name == system_name else "❌ Name mismatch"
            print(f"  • {local_name} matches /system/bin/{system_name} - {status}")
        if moved:
            print(f"\n📦 Moved {len(moved)} files to '{out_dir}/':")
            for old_name, new_name in moved:
                print(f"  • {old_name} -> {new_name}")
    else:
        print("✅ No matching files found.")
    print("-" * 40)
    return 0


def get_dpkg_installed_packages() -> list[str]:
    try:
        result = run_cmd(["dpkg", "-l"], check=False)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    packages: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("ii"):
            parts = line.split()
            if len(parts) >= 2:
                packages.append(parts[1])
    return packages


def is_doc_path(path_str: str, mode: str) -> bool:
    if mode == "none":
        return False
    parts = Path(path_str).parts
    if mode == "parts":
        for i in range(len(parts) - 1):
            if parts[i] == "share" and parts[i + 1] in {
                "man",
                "info",
                "doc",
                "LICENSES",
            }:
                return True
        return False
    if mode == "substring":
        for i in range(len(parts) - 1):
            if parts[i] == "share" and parts[i + 1] in {"man", "info", "doc"}:
                return True
        text = str(path_str)
        return any(
            f"/{item}/" in text or text.endswith(f"/{item}") for item in ("share/man", "share/info", "share/doc")
        )
    msg = f"Unknown filter mode: {mode}"
    raise ValueError(msg)


def check_package_missing_full(pkg: str, filter_mode: str, timeout: float) -> dict[str, Any]:
    try:
        result = run_cmd(["dpkg", "-L", pkg], timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {
            "package": pkg,
            "total_checked": 0,
            "missing_count": 0,
            "missing_files": [],
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "package": pkg,
            "total_checked": 0,
            "missing_count": 0,
            "missing_files": [],
            "error": str(exc),
        }
    if result.returncode != 0:
        return {
            "package": pkg,
            "total_checked": 0,
            "missing_count": 0,
            "missing_files": [],
            "error": f"dpkg -L returned {result.returncode}",
        }
    missing: list[str] = []
    checked = 0
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        if is_doc_path(line, filter_mode):
            continue
        path = Path(line)
        if path.is_dir():
            continue
        checked += 1
        if not path.exists():
            missing.append(line)
    return {
        "package": pkg,
        "total_checked": checked,
        "missing_count": len(missing),
        "missing_files": missing,
    }


def cmd_missing_files(args: argparse.Namespace) -> int:
    packages = get_dpkg_installed_packages()
    if not packages:
        print("No installed dpkg packages found.")
        return 1
    workers = MAX_WORKERS or (os.cpu_count() or 1)
    print(f"Scanning {len(packages)} packages...")
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_pkg = {
            executor.submit(check_package_missing_full, pkg, args.filter_mode, args.timeout): pkg for pkg in packages
        }
        for index, future in enumerate(as_completed(future_to_pkg), 1):
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "package": future_to_pkg[future],
                    "total_checked": 0,
                    "missing_count": 0,
                    "missing_files": [],
                    "error": str(exc),
                }
            if result["missing_count"] > 0:
                results.append(result)
            if index % 10 == 0:
                print(f"  {index}/{len(packages)}")
    if args.report_style == "simple":
        output_path = Path(args.output or "missing_files.json")
        missing_txt_path = Path(args.missing_txt)
        data = {item["package"]: item["missing_files"] for item in results}
        ensure_parent(output_path)
        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        ensure_parent(missing_txt_path)
        missing_txt_path.write_text("\n".join(data.keys()), encoding="utf-8")
        print(f"\n✓ {len(data)} packages with missing files → {output_path}")
        print(f"  Total missing: {sum(len(v) for v in data.values())}")
        return 0
    output_path = Path(args.output or (Path.home() / "pkg_audit_report.json"))
    payload = {
        "timestamp": datetime.now().isoformat(),
        "total_packages": len(packages),
        "summary": {
            "packages_with_missing": len(results),
            "total_missing_files": sum(item["missing_count"] for item in results),
        },
        "packages": results,
    }
    ensure_parent(output_path)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nReport: {payload['summary']}")
    print(f"Saved to: {output_path}")
    return 0


def get_package_files(pkg: str) -> list[str]:
    for cmd in (["dpkg", "-L", pkg], ["rpm", "-ql", pkg]):
        try:
            result = run_cmd(cmd, check=True)
            return [line for line in result.stdout.strip().splitlines() if line]
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    msg = f"Error: could not find package '{pkg}' via dpkg or rpm."
    raise SystemExit(msg)


def cmd_copy_pkg_files(args: argparse.Namespace) -> int:
    pkg = args.package
    dest_root = Path(args.dest_root).expanduser() / pkg
    dest_root.mkdir(parents=True, exist_ok=True)
    files = get_package_files(pkg)
    copied = 0
    skipped = 0
    for file_name in files:
        src = Path(file_name)
        if not src.exists() or not src.is_file():
            skipped += 1
            continue
        relative = src.relative_to(src.anchor)
        dest = dest_root / relative
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
        except (PermissionError, OSError) as exc:
            print(f"Warning: could not copy '{src}': {exc}")
            skipped += 1
    print(f"\nDone. Package: {pkg}")
    print(f"Destination:   {dest_root}")
    print(f"Copied files:  {copied}")
    print(f"Skipped:       {skipped} (missing or non-file entries)")
    return 0


def parse_dpkg_status(status_text: str) -> dict[str, dict[str, list[str]]]:
    packages: dict[str, dict[str, list[str]]] = {}
    for block in re.split(r"\n\s*\n", status_text.strip()):
        pkg_match = re.search(r"^Package:\s*(.+)$", block, flags=re.MULTILINE)
        status_match = re.search(r"^Status:\s*(.+)$", block, flags=re.MULTILINE)
        provides_matches = re.findall(r"^Provides:\s*(.+)$", block, flags=re.MULTILINE)
        depends_matches = re.findall(r"^Depends:\s*(.+)$", block, flags=re.MULTILINE)
        if not pkg_match or not status_match:
            continue
        name = pkg_match.group(1).strip()
        status = status_match.group(1).strip()
        if "install ok installed" not in status:
            continue
        depends: list[str] = []
        for dep_line in depends_matches:
            for alt in dep_line.split(","):
                alt = alt.strip()
                if not alt:
                    continue
                first = alt.split("|", 1)[0].strip()
                match = re.match(r"^([A-Za-z0-9+_.:-]+)\s*(?:\(|$)", first)
                if match:
                    depends.append(match.group(1))
        provides: list[str] = []
        for prov_line in provides_matches:
            for token in prov_line.split(","):
                token = token.strip()
                match = re.match(r"^([A-Za-z0-9+_.:-]+)", token)
                if match:
                    provides.append(match.group(1))
        packages[name] = {"depends": depends, "provides": provides}
    return packages


def cmd_orphan_libs_debian(args: argparse.Namespace) -> int:
    status_path = Path(args.status_file)
    if not status_path.exists():
        msg = f"Missing {status_path}. This script expects a Debian-style dpkg database."
        raise SystemExit(msg)
    packages = parse_dpkg_status(status_path.read_text(errors="replace"))
    providers: dict[str, set[str]] = {}
    for pkg, info in packages.items():
        providers.setdefault(pkg, set()).add(pkg)
        for provided in info["provides"]:
            providers.setdefault(provided, set()).add(pkg)
    reverse_deps: dict[str, set[str]] = {pkg: set() for pkg in packages}
    for pkg, info in packages.items():
        for dep in info["depends"]:
            for provider in providers.get(dep, []):
                reverse_deps.setdefault(provider, set()).add(pkg)
    orphans = sorted(pkg for pkg in packages if pkg.startswith(args.lib_prefix) and not reverse_deps.get(pkg))
    print("Orphan-ish libraries (no installed package depends on them):")
    for pkg in orphans:
        print(pkg)
    return 0


def load_keep_file(path: Path) -> set[str]:
    try:
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}
    except FileNotFoundError:
        return set()


def save_keep_file(path: Path, keep: Iterable[str]) -> None:
    ensure_parent(path)
    path.write_text("\n".join(sorted(keep)) + ("\n" if keep else ""), encoding="utf-8")


def termux_get_installed(pkg_cmd: str) -> set[str]:
    result = run_cmd([pkg_cmd, "list-installed"], check=False)
    installed: set[str] = set()
    for line in result.stdout.strip().splitlines():
        if line:
            installed.add(line.split("/")[0])
    return installed


def termux_get_deps(pkg: str, pkg_cmd: str) -> set[str]:
    result = run_cmd([pkg_cmd, "show", pkg], check=False)
    deps: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith(("Depends:", "Pre-Depends:")):
            value = line.split(":", 1)[1].strip()
            for dep in value.split(","):
                name = dep.strip().split()[0]
                if name:
                    deps.add(name)
    return deps


def analyze_termux_orphans(pkg_cmd: str, keep_file: Path) -> tuple[list[str], set[str]]:
    installed = termux_get_installed(pkg_cmd)
    keep = load_keep_file(keep_file)
    depended: set[str] = set()
    for pkg in installed:
        for dep in termux_get_deps(pkg, pkg_cmd):
            if dep in installed:
                depended.add(dep)
    orphans = sorted(pkg for pkg in installed if pkg not in depended and pkg not in keep)
    return orphans, keep


def cmd_orphan_pkgs_termux(args: argparse.Namespace) -> int:
    keep_file = Path(args.keep_file).expanduser()
    orphans, keep = analyze_termux_orphans(args.pkg_cmd, keep_file)
    if not orphans:
        print("✓ No orphaned packages found!")
        return 0
    print(f"⚠ Found {len(orphans)} orphaned packages:\n")
    for index, pkg in enumerate(orphans, 1):
        print(f"{index}. {pkg}")
    if not args.interactive:
        print(f"\nTotal orphaned packages: {len(orphans)}")
        return 0
    print("\n--- Interactive Mode ---")
    print("Commands: 'keep <pkg>', 'remove <pkg>', 'list', 'save', 'quit'")
    print("-" * 40)
    while True:
        command = input("\n> ").strip()
        if command.startswith("keep "):
            pkg = command[5:].strip()
            if pkg in orphans:
                keep.add(pkg)
                print(f"Added '{pkg}' to keep list")
            else:
                print(f"Package '{pkg}' not found in orphans")
        elif command.startswith("remove "):
            pkg = command[7:].strip()
            keep.discard(pkg)
            print(f"Removed '{pkg}' from keep list")
        elif command == "list":
            if keep:
                print("\nKeep list:")
                for pkg in sorted(keep):
                    print(f"  - {pkg}")
            else:
                print("Keep list is empty")
        elif command == "save":
            save_keep_file(keep_file, keep)
            print(f"Saved keep list to {keep_file}")
        elif command == "quit":
            print("Exiting...")
            break
        else:
            print("Unknown command")
    return 0


def cmd_list_installed_sizes(args: argparse.Namespace) -> int:
    try:
        result = run_cmd(["apt", "list", "--installed"], check=False)
    except FileNotFoundError:
        print("apt command not found.")
        return 1
    package_names: list[str] = []
    for line in result.stdout.splitlines():
        if line and not line.startswith("Listing"):
            parts = line.split()
            if parts:
                package_names.append(parts[0].split("/")[0])
    rows: list[tuple[str, int]] = []
    for pkg in package_names:
        try:
            show = run_cmd(["apt", "show", pkg], check=False)
            for line in show.stdout.splitlines():
                if line.startswith("Installed-Size:"):
                    match = re.search(r"\d+", line)
                    if match:
                        rows.append((pkg, int(match.group()) * 1024))
                    break
        except Exception:
            continue
    rows.sort(key=lambda item: item[1], reverse=True)
    print("=" * 40)
    print(f"{'Package':<30} {'Size':>20}")
    print("-" * 40)
    total = 0
    for pkg, size in rows:
        print(f"{pkg:<30} {human_size(size):>20}")
        total += size
    print("-" * 40)
    print(f"{'TOTAL':<30} {human_size(total):>20}")
    return 0


def _make_deb_one(pkg: str, out_dir_str: str, log_path: str) -> bool:
    try:
        import apt

        out_dir = Path(out_dir_str)
        out_dir.mkdir(parents=True, exist_ok=True)
        deb_path = out_dir / f"{pkg}.deb"
        if deb_path.exists():
            print(f"✓ {pkg}.deb already exists, skipping...")
            return True
        cache = apt.Cache()
        if pkg not in cache:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"Package {pkg} not found in apt cache\n")
            return False
        candidate = cache[pkg].candidate
        if candidate is None:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"No candidate version for {pkg}\n")
            return False
        print(f"⟳ Creating .deb for {pkg}...")
        fetched = candidate.fetch_binary(dest_dir=str(out_dir))
        if fetched and Path(fetched).exists():
            print(f"✓ Successfully created {pkg}.deb")
            return True
        if deb_path.exists():
            print(f"✓ Successfully created {pkg}.deb")
            return True
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"Fetch returned no file for {pkg}\n")
        return False
    except Exception as exc:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"Error creating {pkg}.deb: {exc}\n")
        return False


def cmd_make_deb(args: argparse.Namespace) -> int:
    try:
        import apt
        from loguru import logger
    except ImportError:
        msg = (
            "make-deb requires python-apt and loguru. Install with `apt install python3-apt` and `pip install loguru`."
        )
        raise SystemExit(msg)
    out_dir = Path(args.output_dir).expanduser()
    log_path = Path(args.log).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_parent(log_path)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        log_path,
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}",
    )
    exact_excludes = {item.strip().lower() for item in args.exclude_exact.split(",") if item.strip()}
    substring_excludes = [item.strip().lower() for item in args.exclude_substrings.split(",") if item.strip()]

    def excluded(name: str) -> bool:
        lowered = name.lower()
        if lowered in exact_excludes:
            return True
        return any(sub in lowered for sub in substring_excludes)

    if args.packages:
        packages = args.packages
        print(f"Processing specified packages: {', '.join(packages)}")
    else:
        print("Getting list of all installed packages...")
        cache = apt.Cache()
        packages = sorted(pkg.name for pkg in cache if pkg.is_installed and not excluded(pkg.name))
        print(f"Found {len(packages)} installed packages (after exclusions)")
    if not packages:
        logger.error("No packages to process")
        return 1
    print(f"Processing {len(packages)} packages with {MAX_WORKERS} workers...")
    with Pool(processes=MAX_WORKERS) as pool:
        results = pool.starmap(
            _make_deb_one,
            [(pkg, str(out_dir), str(log_path)) for pkg in packages],
        )
    success = sum(1 for result in results if result)
    failed = len(results) - success
    print("=" * 40)
    print(f"Summary: {success} successful, {failed} failed")
    print(f"Total: {len(results)}")
    print(f".deb files saved in: {out_dir}")
    print(f"Log file: {log_path}")
    if failed:
        logger.warning(f"Some packages failed. Check {log_path} for details.")
        return 1
    return 0


def cmd_suggest_removals(args: argparse.Namespace) -> int:
    try:
        result = run_cmd(
            ["dpkg-query", "-W", "-f=${binary:Package} ${Installed-Size}\n"],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"Error querying dpkg: {exc}")
        return 1
    package_sizes: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            package_sizes.append((parts[0], int(parts[1])))
    history_path = Path(args.history).expanduser()
    if not history_path.exists():
        history_lines: list[str] = []
    else:
        history_lines = history_path.read_text(encoding="utf-8", errors="replace").splitlines()
    used_packages: set[str] = set()
    for line in history_lines:
        for pkg, _ in package_sizes:
            if re.search(rf"\b{re.escape(pkg)}\b", line):
                used_packages.add(pkg)
    build_essential = {
        "build-essential",
        "gcc",
        "make",
        "libc6-dev",
        "pkg-config",
        "libtool",
        "dpkg-dev",
        "autoconf",
        "automake",
    }
    candidates = [(pkg, size) for pkg, size in package_sizes if pkg not in used_packages and pkg not in build_essential]
    candidates.sort(key=lambda item: item[1], reverse=True)
    filter_substrings = [item.strip() for item in args.filter_substrings.split(",") if item.strip()]
    print("Top unused packages (sorted by size):")
    shown = 0
    for pkg, size in candidates:
        if any(sub in pkg for sub in filter_substrings):
            continue
        print(f"{pkg}: {size / 1024:.1f} MB")
        shown += 1
        if shown >= args.top:
            break
    return 0


def cmd_save_deb_names(args: argparse.Namespace) -> int:
    try:
        result = run_cmd(
            ["dpkg-query", "-f", "${binary:Package}\n", "-W"],
            check=True,
        )
    except FileNotFoundError:
        print("Error: dpkg-query command not found. Are you running this script on a Debian-based system?")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: Failed to retrieve installed packages. {exc}")
        return 1
    output_path = Path(args.output)
    ensure_parent(output_path)
    output_path.write_text(result.stdout, encoding="utf-8")
    print(f"Installed package names saved to '{output_path}'")
    return 0


def _show_big_worker(pkg: str) -> tuple[str, int, bool]:
    try:
        result = run_cmd(["apt", "show", pkg], timeout=10, check=False)
        if result.returncode != 0:
            return (pkg, 0, False)
        for line in result.stdout.splitlines():
            if line.startswith("Download-Size:"):
                size_text = line.split(":", 1)[1].strip()
                return (pkg, parse_size(size_text), True)
        return (pkg, 0, False)
    except Exception:
        return (pkg, 0, False)


def _save_package_json(path: str, payload: dict[str, Any]) -> bool:
    try:
        output_path = Path(path)
        ensure_parent(output_path)
        output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        print(f"Error saving JSON: {exc}")
        return False


def cmd_show_big_packages(args: argparse.Namespace) -> int:
    threshold_bytes = int(args.threshold_mb * 1024 * 1024)
    print(f"🔍 Scanning ALL available packages larger than {args.threshold_mb}MB...")
    print("-" * 40)
    try:
        list_result = run_cmd(["apt", "list", "--all-versions"], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"❌ No packages found or error retrieving package list: {exc}")
        print("Make sure you have internet connection and run 'pkg update' first.")
        return 1
    package_names: list[str] = []
    seen: set[str] = set()
    for line in list_result.stdout.strip().splitlines():
        if line and not line.startswith("Listing") and "/" in line:
            pkg = line.split("/", 1)[0]
            if pkg not in seen:
                seen.add(pkg)
                package_names.append(pkg)
    if not package_names:
        print("❌ No packages found.")
        return 1
    print(f"📦 Found {len(package_names)} available packages.")
    print(f"🚀 Using {MAX_WORKERS} parallel processes...")
    large_packages: dict[str, int] = {}
    all_packages: dict[str, int] = {}
    no_size = 0
    with Pool(processes=MAX_WORKERS) as pool:
        for index, (pkg, size, has_size) in enumerate(
            pool.imap_unordered(_show_big_worker, package_names, chunksize=10),
            1,
        ):
            all_packages[pkg] = size if has_size else 0
            if has_size and size >= threshold_bytes:
                large_packages[pkg] = size
            if not has_size:
                no_size += 1
            if index % 50 == 0 or index == len(package_names):
                progress = index / len(package_names) * 40
                print(
                    f"⏳ Progress: {index}/{len(package_names)} ({progress:.1f}%)",
                    end="\r",
                )
    print("\n✅ Processing complete!                    \n")
    print("-" * 40)
    print(f"\n📊 RESULTS: Found {len(large_packages)} packages larger than {args.threshold_mb}MB")
    print("-" * 40)
    if large_packages:
        sorted_large = sorted(large_packages.items(), key=lambda item: item[1], reverse=True)
        total_size = 0
        print(f"{'PACKAGE NAME':<40} {'DOWNLOAD SIZE':>15}")
        print("-" * 40)
        for pkg, size in sorted_large[:50]:
            print(f"{pkg:<40} {human_size(size):>15}")
            total_size += size
        if len(sorted_large) > 50:
            print(f"\n... and {len(sorted_large) - 50} more packages")
            for _, size in sorted_large[50:]:
                total_size += size
        print("-" * 40)
        print(f"{'TOTAL SIZE:':<40} {human_size(total_size):>15}")
        print(f"{'TOTAL PACKAGES:':<40} {len(large_packages):>15}")
    else:
        print("✅ No packages found exceeding the threshold.")
    print("\n📈 Statistics:")
    print(f"   Total packages checked: {len(package_names)}")
    print(f"   Packages with size info: {len(package_names) - no_size}")
    print(f"   Packages below threshold: {len(package_names) - len(large_packages) - no_size}")
    print(f"   Packages with no size info: {no_size}")
    payload = {
        "metadata": {
            "threshold_mb": args.threshold_mb,
            "threshold_bytes": threshold_bytes,
            "scan_date": datetime.now().isoformat(),
            "total_packages": len(all_packages),
            "packages_above_threshold": len(large_packages),
        },
        "packages": large_packages,
    }
    if large_packages:
        save_large = args.save_large or f"packages_above_{args.threshold_mb:g}mb.json"
        if _save_package_json(save_large, payload):
            print(f"\n💾 Results saved to: {save_large}")
            print(f"   Format: {{'package_name': size_in_bytes}} for packages > {args.threshold_mb}MB")
    interactive = not args.non_interactive and sys.stdin.isatty()
    save_all_path = args.save_all
    if save_all_path or (
        interactive and input("\n💾 Save ALL packages (including smaller ones) to JSON? (y/n): ").lower() == "y"
    ):
        all_payload = dict(payload)
        all_payload["all_packages"] = all_packages
        all_path = save_all_path or "all_packages_sizes.json"
        if _save_package_json(all_path, all_payload):
            print(f"✅ All packages saved to: {all_path}")
            print("   Format: {'package_name': size_in_bytes} for ALL packages")
    simple_path = args.save_simple
    if simple_path or (interactive and input("\n💾 Save simple JSON (just {package: size})? (y/n): ").lower() == "y"):
        simple_path = simple_path or f"packages_sizes_{args.threshold_mb:g}mb.json"
        try:
            output_path = Path(simple_path)
            ensure_parent(output_path)
            output_path.write_text(
                json.dumps(large_packages, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"✅ Simple JSON saved to: {simple_path}")
            print('   Format: {"package1": 12345678, "package2": 98765432}')
        except Exception as exc:
            print(f"Error saving simple JSON: {exc}")
    return 0


def cmd_sort_csv(args: argparse.Namespace) -> int:
    csv_path = Path(args.csvfile)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames
    if not fieldnames or "Installed-Size" not in fieldnames:
        print("Error: 'Installed-Size' column not found in CSV")
        return 1
    rows.sort(key=lambda row: int(row.get("Installed-Size") or 0), reverse=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"File '{csv_path}' sorted by Installed-Size and overwritten.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merged Debian/Termux package and system-bin toolkit.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    p = subparsers.add_parser(
        "check-system-bin",
        help="Scan /system/bin and move same-name matching files from CWD.",
    )
    p.add_argument("--system-bin", default="/system/bin")
    p.add_argument("--work-dir", default=".")
    p.add_argument("--output-dir", default="matched_system_files")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    p.set_defaults(func=cmd_check_system_bin)
    p = subparsers.add_parser(
        "missing-files",
        help="Audit installed dpkg packages for missing files.",
    )
    p.add_argument(
        "--report-style",
        choices=["simple", "audit"],
        default="simple",
        help="simple = original check_system_missing_files.py output; audit = original chk_sys_missing.py output.",
    )
    p.add_argument(
        "--filter-mode",
        choices=["parts", "substring", "none"],
        default="parts",
        help="parts = check_system_missing_files.py filter; substring = chk_sys_missing.py filter.",
    )
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--output", default=None)
    p.add_argument("--missing-txt", default="missing.txt")
    p.set_defaults(func=cmd_missing_files)
    p = subparsers.add_parser(
        "copy-pkg-files",
        help="Copy files belonging to one dpkg/rpm package.",
    )
    p.add_argument("package")
    p.add_argument("--dest-root", default="~/tmp/deb")
    p.set_defaults(func=cmd_copy_pkg_files)
    p = subparsers.add_parser(
        "orphan-libs-debian",
        help="Find Debian orphan-ish library packages.",
    )
    p.add_argument("--status-file", default="/var/lib/dpkg/status")
    p.add_argument("--lib-prefix", default="lib")
    p.set_defaults(func=cmd_orphan_libs_debian)
    p = subparsers.add_parser(
        "orphan-pkgs-termux",
        help="Find Termux orphan packages.",
    )
    p.add_argument("--keep-file", default="~/.deborphan-keep")
    p.add_argument("--pkg-cmd", default="pkg")
    p.add_argument("--interactive", action="store_true")
    p.set_defaults(func=cmd_orphan_pkgs_termux)
    p = subparsers.add_parser(
        "list-installed-sizes",
        help="List installed apt packages by installed size.",
    )
    p.set_defaults(func=cmd_list_installed_sizes)
    p = subparsers.add_parser(
        "make-deb",
        help="Create .deb files from apt cache for installed packages.",
    )
    p.add_argument("packages", nargs="*")
    p.add_argument("--output-dir", default="~/debs")
    p.add_argument("--log", default="~/make_deb.log")
    p.add_argument(
        "--exclude-exact",
        default=",".join(sorted(DEFAULT_DEB_EXCLUDES)),
        help="Comma-separated exact package names to exclude.",
    )
    p.add_argument(
        "--exclude-substrings",
        default="llvm,clang,rust,cargo",
        help="Comma-separated substrings to exclude.",
    )
    p.set_defaults(func=cmd_make_deb)
    p = subparsers.add_parser(
        "suggest-removals",
        help="Suggest unused packages from bash history.",
    )
    p.add_argument("--history", default="~/.bash_history")
    p.add_argument("--top", type=int, default=100)
    p.add_argument(
        "--filter-substrings",
        default="python,l8b,static",
        help="Comma-separated substrings to hide from output.",
    )
    p.set_defaults(func=cmd_suggest_removals)
    p = subparsers.add_parser(
        "save-deb-names",
        help="Save installed dpkg package names.",
    )
    p.add_argument("--output", default="installed.txt")
    p.set_defaults(func=cmd_save_deb_names)
    p = subparsers.add_parser(
        "show-big-packages",
        help="Scan apt packages by download size.",
    )
    p.add_argument("--threshold-mb", type=float, default=10.0)
    p.add_argument("--save-large", default=None)
    p.add_argument("--save-all", default=None)
    p.add_argument("--save-simple", default=None)
    p.add_argument("--non-interactive", action="store_true")
    p.set_defaults(func=cmd_show_big_packages)
    p = subparsers.add_parser(
        "sort-csv",
        help="Sort a package CSV by Installed-Size.",
    )
    p.add_argument("csvfile")
    p.set_defaults(func=cmd_sort_csv)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
