#!/data/data/com.termux/files/usr/bin/env python
"""Keep the newest package files and filter the ARMv7 wheel URLs to the latest versions.
The ``urls`` subcommand parses the wheel URLs and keeps the newest Android/linux-arm build of each package, ``clean`` deletes the older wheel/deb/tar.gz files of a directory (``--dry-run`` previews, ``--backup-dir`` moves them) and ``metadata`` prunes the older ``.metadata`` files.
Usage: klv {urls,clean,metadata} [options] [input] Positional arguments: input Input file or URL directory Directory to scan Subcommands: urls Filter latest ARMv7 wheels from URL list clean Keep latest package files in a directory metadata Keep latest .metadata files in a directory Options: --output, -o Output file to save URLs --download Generate download script --arm-keywords Architecture keywords to keep --type, -t Package type to clean --dry-run Simulate deletion --dir Directory to scan --verbose Show detailed information --recursive Scan recursively --backup-dir Move old files to backup directory --batch-size Batch size Examples: $ klv urls urls.txt -o arm_latest.txt $ klv clean --type whl --dry-run Command-line script: run ``python klv.py`` or the ``klv`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import multiprocessing as mp
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger

ANDROID_RE = re.compile(r"/([^/]+)-(\d+\.\d+\.\d+)-py3-none-android_24_([^/]+)\.whl")
LINUX_RE = re.compile(r"/([^/]+)-(\d+\.\d+\.\d+(?:\.\d+)?)-cp\d+-cp\d+-linux_([^/]+)\.whl")
PY_RE = re.compile(r"python3\.(\d+)")
METADATA_RE = re.compile(r"^(.+?)-(\d[\d._]*[a-zA-Z]*[\d]*)$")
NORMALIZE_RE = re.compile(r"[-_.]+")
DEFAULT_ARM_KEYWORDS: list[str] = ["armeabi_v7a", "armv7l", "linux_arm", "arm"]
DEFAULT_BATCH_SIZE: int = 100


def fallback_version_key(v: str) -> tuple[Any, ...]:
    parts = re.split(r"[._-]", v)
    key: list[tuple[Any, ...]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            m = re.match(r"^(\d*)([a-zA-Z]*)(\d*)$", part)
            if m:
                a, b, c = m.groups()
                key.append((1, int(a or 0), b, int(c or 0)))
            else:
                key.append((2, part, "", 0))
    return tuple(key)


def compare_versions(a: str, b: str) -> int:
    try:
        from packaging.version import parse

        v1 = parse(a)
        v2 = parse(b)
        if v1 < v2:
            return -1
        if v1 > v2:
            return 1
        return 0
    except ImportError:
        k1 = fallback_version_key(a)
        k2 = fallback_version_key(b)
        if k1 < k2:
            return -1
        if k1 > k2:
            return 1
        return 0
    except Exception:
        if a < b:
            return -1
        if a > b:
            return 1
        return 0


def parse_wheel_or_metadata(name: str) -> Optional[tuple[str, str]]:
    if name.endswith(".whl"):
        stem = name[:-4]
    elif name.endswith(".metadata"):
        stem = name[:-9]
    else:
        return None
    parts = stem.split("-")
    if len(parts) < 5:
        return None
    pkg_parts: list[str] = []
    ver_parts: list[str] = []
    in_ver = False
    for i, part in enumerate(parts):
        if not in_ver and (re.match(r"^\d", part) or part.lower() in ("v", "ver", "version")):
            in_ver = True
            ver_parts.append(part)
        elif not in_ver:
            pkg_parts.append(part)
        else:
            remaining = len(parts) - i
            if remaining <= 3:
                break
            ver_parts.append(part)
    if pkg_parts and ver_parts:
        return "-".join(pkg_parts), "-".join(ver_parts)
    return None


def parse_targz(name: str) -> Optional[tuple[str, str]]:
    if name.endswith(".tar.gz"):
        stem = name[:-7]
    elif name.endswith(".tgz"):
        stem = name[:-4]
    else:
        return None
    parts = stem.split("-")
    for i, part in enumerate(parts):
        if re.match(r"^\d", part):
            pkg = "-".join(parts[:i])
            ver = "-".join(parts[i:])
            ver = re.sub(r"\.(tar|tgz)$", "", ver)
            if pkg and ver:
                return pkg, ver
    return None


def parse_deb(name: str) -> Optional[tuple[str, str]]:
    parts = name.split("_")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def normalize_package_name(name: str) -> str:
    return NORMALIZE_RE.sub("-", name).lower()


def parse_metadata_filename(path: Path) -> tuple[str, str, Path]:
    stem = path.stem
    m = METADATA_RE.match(stem)
    if not m:
        logger.warning("Could not parse version from {}", path.name)
        return stem.lower(), "0.0.0", path
    pkg = m.group(1).lower()
    ver = m.group(2).replace("_", ".")
    return pkg, ver, path


def parse_url(url: str) -> Optional[tuple[str, str, tuple[int, ...], str, str]]:
    m = ANDROID_RE.search(url)
    if m:
        pkg = m.group(1)
        ver = tuple(map(int, m.group(2).split(".")))
        arch = m.group(3)
        return pkg, "android", ver, arch, url
    m = LINUX_RE.search(url)
    if m:
        pkg = m.group(1)
        ver = tuple(map(int, m.group(2).split(".")))
        arch = m.group(3)
        py_m = PY_RE.search(url)
        py = py_m.group(1) if py_m else "unknown"
        return pkg, py, ver, arch, url
    return None


def extensions_for_type(file_type: str) -> tuple[str, ...]:
    if file_type == "wheel":
        return (".whl", ".metadata")
    if file_type == "deb":
        return (".deb",)
    if file_type == "targz":
        return (".tar.gz", ".tgz")
    if file_type == "all":
        return (".whl", ".metadata", ".deb", ".tar.gz", ".tgz")
    return (".whl", ".metadata")


def find_files(directory: Path, extensions: Sequence[str], recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.glob("*")
    return [p for p in iterator if p.is_file() and any(p.name.endswith(ext) for ext in extensions)]


def process_clean_file(path: Path) -> Optional[tuple[str, str, Path]]:
    name = path.name
    parsed: Optional[tuple[str, str]] = None
    if name.endswith((".whl", ".metadata")):
        parsed = parse_wheel_or_metadata(name)
    elif name.endswith((".tar.gz", ".tgz")):
        parsed = parse_targz(name)
    elif name.endswith(".deb"):
        parsed = parse_deb(name)
    if parsed:
        pkg, ver = parsed
        return pkg, ver, path
    return None


def process_clean_files(files: list[Path], workers: int) -> dict[str, list[tuple[str, Path]]]:
    if workers <= 1:
        results = [process_clean_file(f) for f in files]
    else:
        with mp.Pool(processes=workers) as pool:
            results = pool.map(process_clean_file, files)
    package_map: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for rec in results:
        if rec:
            pkg, ver, path = rec
            package_map[pkg].append((ver, path))
    return dict(package_map)


def process_metadata_files(files: list[Path], workers: int) -> dict[str, list[tuple[str, Path]]]:
    if workers <= 1:
        results = [parse_metadata_filename(f) for f in files]
    else:
        with mp.Pool(processes=workers) as pool:
            results = pool.map(parse_metadata_filename, files)
    package_map: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for pkg, ver, path in results:
        norm = normalize_package_name(pkg)
        package_map[norm].append((ver, path))
    return dict(package_map)


def process_metadata_batches(files: list[Path], workers: int, batch_size: int) -> dict[str, list[tuple[str, Path]]]:
    batches = [files[i : i + batch_size] for i in range(0, len(files), batch_size)]
    all_map: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for batch in batches:
        batch_map = process_metadata_files(batch, workers)
        for k, v in batch_map.items():
            all_map[k].extend(v)
    return dict(all_map)


def find_latest_version(versions: list[tuple[str, Path]]) -> Optional[tuple[str, Path]]:
    if not versions:
        return None
    best = versions[0]
    for ver, path in versions[1:]:
        if compare_versions(ver, best[0]) > 0:
            best = (ver, path)
    return best


def remove_old_versions(
    package_map: dict[str, list[tuple[str, Path]]],
    dry_run: bool,
    backup_dir: Optional[Path],
    verbose: bool,
) -> tuple[int, int]:
    deleted = 0
    kept = 0
    for pkg, versions in package_map.items():
        if len(versions) <= 1:
            kept += len(versions)
            continue
        latest = find_latest_version(versions)
        if latest is None:
            continue
        latest_ver, latest_path = latest
        print(f"Package: {pkg}")
        print(f"  Latest version: {latest_ver}-{latest_path.name}")
        print(f"  Total versions found: {len(versions)}")
        for ver, path in versions:
            if path == latest_path:
                continue
            if dry_run:
                print(f"  Would delete: {ver}-{path.name}")
            elif backup_dir is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                dest = backup_dir / path.name
                shutil.move(str(path), str(dest))
                print(f"  Moved to backup: {path.name}")
                deleted += 1
            else:
                try:
                    path.unlink()
                    print(f"  Deleted: {ver}-{path.name}")
                    deleted += 1
                except Exception as e:
                    logger.error("  Error deleting {}: {}", path.name, e)
        kept += 1
    return deleted, kept


def cmd_urls(args: argparse.Namespace) -> int:
    lines: list[str] = []
    if args.input:
        p = Path(args.input)
        if p.exists():
            lines = [line.strip() for line in p.read_text().splitlines() if line.strip()]
        else:
            lines = [args.input]
    else:
        lines = [line.strip() for line in sys.stdin if line.strip()]
    groups: dict[tuple[str, str], dict[str, tuple[tuple[int, ...], str]]] = defaultdict(dict)
    for url in lines:
        rec = parse_url(url)
        if not rec:
            continue
        pkg, py, ver, arch, url = rec
        if not any(k in arch.lower() for k in args.arm_keywords):
            continue
        key = (pkg, py)
        if arch not in groups[key] or ver > groups[key][arch][0]:
            groups[key][arch] = (ver, url)
    results: list[dict[str, str]] = []
    print("-" * 40)
    print("LATEST ARMv7 (armeabi_v7a/armv7l/linux_arm) WHEELS")
    print("-" * 40)
    for (pkg, py), arch_map in sorted(groups.items()):
        for arch, (ver, url) in arch_map.items():
            ver_str = ".".join(map(str, ver))
            print(f"\n📦 {pkg} (Python {py})")
            print(f"   Arch: {arch}")
            print(f"   Version: {ver_str}")
            print(f"   URL: {url}")
            results.append({
                "package": pkg,
                "python_version": py,
                "arch": arch,
                "version": ver_str,
                "url": url,
            })
    print("\n" + "=" * 40)
    print(f"SUMMARY: Found {len(results)} ARMv7 wheel(s)")
    print("-" * 40)
    for r in results:
        print(f"{r['package']}=={r['version']} (Python {r['python_version']})")
    if args.output:
        Path(args.output).write_text("\n".join(r["url"] for r in results) + "\n")
        print(f"\n✓ URLs saved to {args.output}")
    if args.download:
        script = "#!/bin/bash\n\n"
        for r in results:
            name = r["url"].split("/")[-1]
            script += f"echo 'Downloading {name}...'\n"
            script += f"wget {r['url']}\n\n"
        Path("download_armv7.sh").write_text(script)
        print("\n✓ Download script created: download_armv7.sh")
        print("  Run: chmod +x download_armv7.sh && ./download_armv7.sh")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    directory = Path(args.dir).resolve()
    if not directory.exists():
        logger.error("Directory '{}' does not exist", directory)
        return 1
    extensions = extensions_for_type(args.type)
    files = find_files(directory, extensions, args.recursive)
    print(f"Scanning directory: {directory}")
    print(f"File type: {args.type}")
    if args.dry_run:
        print("DRY RUN MODE - No files will be deleted")
    print("-" * 40)
    print(f"Found {len(files)} files to process...")
    if not files:
        print("No matching package files found.")
        return 0
    package_map = process_clean_files(files, MAX_WORKERS)
    total_versions = sum(len(v) for v in package_map.values())
    print(f"\nFound {len(package_map)} package(s) with {total_versions} total version(s):")
    if args.verbose:
        for pkg, versions in package_map.items():
            print(f"\n  {pkg}: {len(versions)} version(s)")
            for ver, path in versions:
                print(f"    - {ver}: {path.name}")
    else:
        for pkg, versions in package_map.items():
            print(f"  {pkg}: {len(versions)} version(s)")
    print("\n" + "=" * 40)
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    deleted, kept = remove_old_versions(package_map, args.dry_run, backup_dir, args.verbose)
    print("\n" + "=" * 40)
    if deleted == 0:
        print("No files to delete. All packages have only one version.")
    elif args.dry_run:
        print(f"Dry run complete. Would delete {deleted} file(s), keep {kept} file(s).")
    else:
        print(f"Cleanup complete. Deleted {deleted} file(s), kept {kept} file(s).")
    return 0


def cmd_metadata(args: argparse.Namespace) -> int:
    directory = Path(args.directory).resolve()
    if not directory.exists():
        logger.error("Directory '{}' does not exist", directory)
        return 1
    files = find_files(directory, (".metadata",), recursive=False)
    print(f"Scanning directory: {directory}")
    print(f"Found {len(files)} metadata files")
    if not files:
        print("No metadata files found")
        return 0
    package_map = process_metadata_batches(files, MAX_WORKERS, args.batch_size)
    print(f"Processing {len(package_map)} unique packages...")
    if args.verbose:
        for pkg, versions in package_map.items():
            print(f"\n  {pkg}: {len(versions)} version(s)")
            for ver, path in versions:
                print(f"    - {ver}: {path.name}")
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    if backup_dir and not args.dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
    deleted, kept = remove_old_versions(package_map, args.dry_run, backup_dir, args.verbose)
    print("=" * 40)
    print("Summary:")
    print(f"  Total metadata files: {len(files)}")
    print(f"  Unique packages: {len(package_map)}")
    print(f"  Files to remove: {deleted}")
    if deleted:
        if args.dry_run:
            print("This was a dry run. Use without --dry-run to actually delete files.")
        else:
            print(f"Cleanup complete. Deleted/moved {deleted} file(s), kept {kept} file(s).")
    else:
        print("No duplicate versions found. All packages have single versions.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified package/version cleanup and ARMv7 wheel URL filtering tool.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_urls = sub.add_parser("urls", help="Filter latest ARMv7 wheels from URL list")
    p_urls.add_argument("input", nargs="?", default=None, help="Input file or URL")
    p_urls.add_argument("--output", "-o", help="Output file to save URLs")
    p_urls.add_argument("--download", action="store_true", help="Generate download script")
    p_urls.add_argument(
        "--arm-keywords",
        nargs="+",
        default=DEFAULT_ARM_KEYWORDS,
        help="Architecture keywords to keep",
    )
    p_urls.set_defaults(func=cmd_urls)
    p_clean = sub.add_parser("clean", help="Keep latest package files in a directory")
    p_clean.add_argument(
        "--type",
        "-t",
        choices=["wheel", "deb", "targz", "all"],
        default="wheel",
        help="Package type to clean",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="Simulate deletion")
    p_clean.add_argument("--dir", default=".", help="Directory to scan")
    p_clean.add_argument("--verbose", action="store_true", help="Show detailed information")
    p_clean.add_argument("--recursive", action="store_true", help="Scan recursively")
    p_clean.add_argument("--backup-dir", help="Move old files to backup directory")
    p_clean.set_defaults(func=cmd_clean)
    p_meta = sub.add_parser("metadata", help="Keep latest .metadata files in a directory")
    p_meta.add_argument("directory", nargs="?", default=".", help="Directory to scan")
    p_meta.add_argument("--dry-run", action="store_true", help="Simulate deletion")
    p_meta.add_argument("--backup-dir", help="Move old files to backup directory")
    p_meta.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Batch size")
    p_meta.add_argument("--verbose", action="store_true", help="Show detailed information")
    p_meta.set_defaults(func=cmd_metadata)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
