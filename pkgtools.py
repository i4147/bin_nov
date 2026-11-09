#!/data/data/com.termux/files/usr/bin/env python
"""Copy, move and repack the installed Python packages.
Subcommands: ``record`` copies or moves the files listed in a ``RECORD`` from the current dist-info, ``site-copy`` copies the installed package directories out of ``site-packages``, ``repack`` rebuilds them into a wheel-like directory layout and ``entry-points`` lists the packages declaring console scripts.
Usage: pkgtools {record,site-copy,repack,entry-points} [options] [packages ...] Positional arguments: packages ...
Package names to process.
patterns ...
Package names or wildcard patterns.
Subcommands: record Copy/move files listed in RECORD from cwd dist-info.
site-copy Copy installed package directories from site-packages.
repack Repack installed packages into wheel-like directory structure.
entry-points Extract packages that declare entry points.
Options: -v, --verbose Enable debug logging (for subcommands that use it).
--all Process all dist-info directories in the current directory.
--move Move files instead of copying them.
--dest Destination root (default: ~/tmp/1 with --move, else ~/tmp/packages).
--per-package-subdir Place files under <dest>/<pkg>/...
(default: follows --move).
--clean-record Remove .pyc lines from RECORD files before processing.
--skip-pyc Skip .pyc files (default: True).
--no-skip-pyc Do not skip .pyc files.
--warn-missing Warn about missing files (default: True).
--no-warn-missing Do not warn about missing files.
...
plus 8 more; run ``pkgtools --help`` for the full list.
Examples: $ pkgtools site-copy requests --dest ~/tmp/packages Command-line script: run ``python pkgtools.py`` or the ``pkgtools`` command.
The module is importable as well."""

import argparse
import concurrent.futures
import csv
import fnmatch
import importlib.metadata
import os
from pathlib import Path
import shutil
import site
import sys
from typing import Any, Sequence

from dh import MAX_WORKERS
from loguru import logger


def configure_logging(verbose: bool = False) -> None:
    level = "DEBUG" if verbose else "INFO"


def get_user_site() -> Path:
    if not site.USER_SITE:
        site.main()
    return Path(site.USER_SITE).resolve()


def get_site_packages_paths(
    include_user: bool = True,
    include_pythonpath: bool = False,
) -> list[Path]:
    paths: list[Path] = [Path(p) for p in site.getsitepackages()]
    if include_user:
        user = get_user_site()
        if user.exists():
            paths.append(user)
    if include_pythonpath:
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
            if entry:
                paths.append(Path(entry))
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved.exists() and resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def find_site_packages_dirs(start: Path) -> list[Path]:
    found: set[Path] = set()
    venv_names = [".venv", "venv", "env", "virtualenv"]
    for venv in venv_names:
        try:
            for vdir in start.rglob(venv):
                if not vdir.is_dir():
                    continue
                candidates = [
                    vdir / "lib" / "python*" / "site-packages",
                    vdir / "lib" / "site-packages",
                    vdir / "Lib" / "site-packages",
                ]
                for candidate in candidates:
                    for sp in candidate.parent.glob(candidate.name):
                        if sp.is_dir():
                            found.add(sp.resolve())
        except (PermissionError, OSError) as exc:
            logger.debug("Permission denied while scanning {}: {}", venv, exc)
    for name in ("site-packages", "dist-packages"):
        try:
            for sp in start.rglob(name):
                if sp.is_dir():
                    found.add(sp.resolve())
        except (PermissionError, OSError) as exc:
            logger.debug("Permission denied while scanning {}: {}", name, exc)
    return sorted(found)


def find_dist_info(root: Path, pkg: str) -> Path:
    candidates = list(root.glob(f"{pkg}-*.dist-info"))
    if not candidates:
        normalized = pkg.replace("-", "_")
        candidates = list(root.glob(f"{normalized}-*.dist-info"))
    if not candidates:
        msg = f"dist-info not found for package {pkg!r} in {root}"
        raise FileNotFoundError(msg)
    if len(candidates) > 1:
        logger.warning(
            "Multiple dist-info directories found for {}, using {}",
            pkg,
            candidates[0],
        )
    return candidates[0]


def read_record(record_path: Path) -> list[list[str]]:
    with record_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def clean_record_pyc(dist_info: Path) -> None:
    record = dist_info / "RECORD"
    if not record.exists():
        return
    lines = record.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    for line in lines:
        if line.strip():
            parts = line.split(",")
            if parts[0].endswith(".pyc"):
                continue
        kept.append(line)
    record.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def read_metadata(dist_info: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": dist_info.stem.split("-")[0],
        "version": None,
        "requires_python": None,
    }
    metadata_file = dist_info / "METADATA"
    if metadata_file.exists():
        try:
            for line in metadata_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("Version:"):
                    meta["version"] = line.split(":", 1)[1].strip()
                elif line.startswith("Requires-Python:"):
                    meta["requires_python"] = line.split(":", 1)[1].strip()
        except Exception as exc:
            logger.warning("Could not read metadata from {}: {}", metadata_file, exc)
    if meta["version"] is None:
        parts = dist_info.stem.split("-")
        if len(parts) >= 2:
            meta["version"] = parts[-1]
    return meta


def get_dist_info_path(dist: importlib.metadata.Distribution) -> Path | None:
    path = getattr(dist, "_path", None)
    if path is not None:
        return Path(path)
    for file in dist.files or []:
        if file.name == "RECORD":
            return Path(file.locate()).parent
    return None


def has_entry_points(dist: importlib.metadata.Distribution) -> bool:
    try:
        return bool(dist.entry_points)
    except Exception:
        return False


def copy_record_files(
    dist_info: Path,
    source_root: Path,
    dest_root: Path,
    *,
    move: bool = False,
    per_package_subdir: bool = False,
    skip_pyc: bool = True,
    warn_missing: bool = True,
) -> tuple[int, int, int]:
    record = dist_info / "RECORD"
    if not record.exists():
        logger.warning("RECORD not found in {}", dist_info)
        return 0, 0, 0
    package_name = dist_info.name.replace(".dist-info", "").split("-")[0]
    base_dest = dest_root / package_name if per_package_subdir else dest_root
    copied = 0
    missing = 0
    errors = 0
    for row in read_record(record):
        try:
            if not row:
                continue
            path_str = row[0].strip()
            if not path_str:
                continue
            if skip_pyc and path_str.endswith(".pyc"):
                continue
            src = Path(path_str)
            if not src.is_absolute():
                src = source_root / src
            if not src.exists():
                if "dist-info" in str(src):
                    missing += 1
                    continue
                if src.suffix != ".pyc":
                    if warn_missing:
                        logger.warning("Missing file listed in RECORD: {}", src)
                    missing += 1
                    continue
                continue
            if src.is_absolute():
                dst = base_dest / src.name
            else:
                dst = base_dest / path_str
            dst.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(src), str(dst))
            else:
                shutil.copy2(src, dst)
            copied += 1
        except Exception as exc:
            logger.exception("Error processing RECORD entry {}: {}", row, exc)
            errors += 1
    return copied, missing, errors


def find_package_dir(pkg: str, site_paths: Sequence[Path]) -> Path | None:
    for sp in site_paths:
        candidate = sp / pkg
        if candidate.exists() and candidate.is_dir():
            return candidate
        normalized = pkg.replace("-", "_")
        candidate = sp / normalized
        if candidate.exists() and candidate.is_dir():
            return candidate
        for child in sp.iterdir():
            if child.is_dir() and child.name.lower().replace("-", "_") == pkg.lower().replace("-", "_"):
                return child
    return None


def copy_package_tree(
    pkg: str,
    output: Path,
    site_paths: Sequence[Path],
    skip_pyc: bool,
) -> tuple[str, bool, str]:
    try:
        src = find_package_dir(pkg, site_paths)
        if not src:
            return pkg, False, "Package directory not found"
        dest = output / pkg
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for path in src.rglob("*"):
            if skip_pyc and path.suffix == ".pyc":
                continue
            rel = path.relative_to(src)
            target = dest / rel
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        return pkg, True, f"Copied to {dest}"
    except Exception as exc:
        return pkg, False, f"Error: {exc}"


def generate_wheel_tags(purelib: bool) -> tuple[str, str, str]:
    if purelib:
        return "py3", "none", "any"
    try:
        from packaging.tags import sys_tags

        tag = next(sys_tags())
        return tag.interpreter, tag.abi, tag.platform
    except ImportError:
        import platform

        interp = f"cp{sys.version_info.major}{sys.version_info.minor}"
        abi = interp
        plat = f"{platform.system().lower()}_{platform.machine()}"
        return interp, abi, plat


def repack_package(
    dist_info: Path,
    site_packages: Path,
    output_base: Path,
    verbose: bool = False,
) -> Path | None:
    record = dist_info / "RECORD"
    if not record.exists():
        logger.warning("Skipping {}: RECORD file not found", dist_info.name)
        return None
    meta = read_metadata(dist_info)
    name = meta["name"]
    version = meta["version"] or "0.0.0"
    rows = read_record(record)
    paths = [row[0] for row in rows if row and row[0]]
    purelib = not any(path.endswith(".so") for path in paths)
    interp, abi, plat = generate_wheel_tags(purelib)
    wheel_dir = output_base / f"{name.replace('-', '_')}-{version}-{interp}-{abi}-{plat}"
    dist_info_dest = wheel_dir / f"{name}-{version}.dist-info"
    dist_info_dest.mkdir(parents=True, exist_ok=True)
    for path_str in paths:
        if path_str.endswith(".pyc"):
            continue
        src = site_packages / path_str
        if not src.exists():
            if verbose:
                logger.debug("Missing file from RECORD: {}", src)
            continue
        if ".dist-info" in path_str:
            dst = dist_info_dest / Path(path_str).name
        else:
            dst = wheel_dir / path_str
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    wheel_file = dist_info_dest / "WHEEL"
    wheel_file.write_text(
        "Wheel-Version: 1.0\n"
        "Generator: pkgtool 1.0\n"
        f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
        f"Tag: {interp}-{abi}-{plat}\n",
        encoding="utf-8",
    )
    src_meta = dist_info / "METADATA"
    dst_meta = dist_info_dest / "METADATA"
    if src_meta.exists() and not dst_meta.exists():
        shutil.copy2(src_meta, dst_meta)
    elif not dst_meta.exists():
        dst_meta.write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    shutil.copy2(record, dist_info_dest / "RECORD")
    return wheel_dir


def cmd_record(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.clean_record:
        for dist_info in cwd.glob("*.dist-info"):
            clean_record_pyc(dist_info)
    if args.all:
        packages = [(dist.stem.split("-")[0], dist) for dist in cwd.glob("*.dist-info")]
    else:
        if not args.packages:
            logger.error("Specify package names or use --all")
            return 1
        packages = []
        for pkg in args.packages:
            try:
                dist_info = find_dist_info(cwd, pkg)
                packages.append((pkg, dist_info))
            except FileNotFoundError as exc:
                logger.error(str(exc))
                return 1
    if not packages:
        logger.error("No dist-info directories found")
        return 1
    if args.dest:
        dest_root = Path(args.dest).expanduser().resolve()
    else:
        dest_root = Path.home() / "tmp" / "1" if args.move else Path.home() / "tmp" / "packages"
    per_pkg = args.per_package_subdir if args.per_package_subdir is not None else args.move

    def process(pkg: str, dist_info: Path) -> int:
        copied, missing, errors = copy_record_files(
            dist_info,
            cwd,
            dest_root,
            move=args.move,
            per_package_subdir=per_pkg,
            skip_pyc=args.skip_pyc,
            warn_missing=args.warn_missing,
        )
        logger.info(
            "{}: copied={} missing={} errors={}",
            pkg,
            copied,
            missing,
            errors,
        )
        return copied

    if MAX_WORKERS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            list(executor.map(lambda item: process(*item), packages))
    else:
        for pkg, dist_info in packages:
            process(pkg, dist_info)
    return 0


def cmd_site_copy(args: argparse.Namespace) -> int:
    site_paths = get_site_packages_paths(include_user=not args.no_user_site)
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                copy_package_tree,
                pkg,
                output,
                site_paths,
                args.skip_pyc,
            ): pkg
            for pkg in args.packages
        }
        for future in concurrent.futures.as_completed(futures):
            pkg = futures[future]
            try:
                name, ok, msg = future.result()
                if ok:
                    logger.info("{}: {}", name, msg)
                else:
                    logger.error("{}: {}", name, msg)
            except Exception as exc:
                logger.exception("Package {} failed: {}", pkg, exc)
    return 0


def cmd_repack(args: argparse.Namespace) -> int:
    if args.skip_scan:
        site_dirs = get_site_packages_paths(include_user=not args.no_user_site)
    else:
        site_dirs = find_site_packages_dirs(Path.cwd())
    output_base = Path(args.output).expanduser().resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    tasks: list[tuple[Path, Path, Path]] = []
    for site_packages in site_dirs:
        site_str = str(site_packages)
        env_name = "local_env"
        if any(marker in site_str for marker in (".venv", "venv", "env", "virtualenv")):
            env_name = site_packages.parent.parent.name or "local_env"
        env_output = output_base / env_name
        env_output.mkdir(parents=True, exist_ok=True)
        for dist_info in site_packages.glob("*.dist-info"):
            tasks.append((dist_info, site_packages, env_output))
    if not tasks:
        logger.warning("No dist-info directories found to repack")
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                repack_package,
                dist_info,
                site_packages,
                env_output,
                args.verbose,
            )
            for dist_info, site_packages, env_output in tasks
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    logger.info("Repacked to {}", result)
            except Exception as exc:
                logger.exception("Repack failed: {}", exc)
    return 0


def cmd_entry_points(args: argparse.Namespace) -> int:
    if args.all_sites:
        site_paths = get_site_packages_paths(
            include_user=True,
            include_pythonpath=True,
        )
        user_site_only = False
    else:
        site_paths = [get_user_site()]
        user_site_only = True
    selected: list[tuple[str, importlib.metadata.Distribution]] = []
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"]
        except Exception:
            continue
        if not has_entry_points(dist):
            continue
        location = dist.locate_file("").resolve()
        if not any(location == sp.resolve() or location.is_relative_to(sp.resolve()) for sp in site_paths):
            continue
        if args.patterns:
            if not any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in args.patterns):
                continue
        elif not args.all:
            continue
        selected.append((name, dist))
    seen: set[str] = set()
    unique: list[tuple[str, importlib.metadata.Distribution]] = []
    for name, dist in selected:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append((name, dist))
    selected = unique
    if args.list_only or args.show_entry_points:
        for name, dist in selected:
            print(f"- {name}")
            if args.show_entry_points:
                for ep in sorted(dist.entry_points, key=lambda x: (x.group, x.name)):
                    print(f"  [{ep.group}] {ep.name} = {ep.value}")
        if args.list_only:
            return 0
    if not selected:
        logger.warning("No matching entry-point packages found")
        return 0
    if args.dest:
        dest_root = Path(args.dest).expanduser().resolve()
    else:
        dest_root = Path.home() / "tmp" / "pkgs" if user_site_only else Path.home() / "tmp" / "packages"
    dest_root.mkdir(parents=True, exist_ok=True)

    def extract(
        name: str,
        dist: importlib.metadata.Distribution,
    ) -> tuple[str, bool, str]:
        dist_info = get_dist_info_path(dist)
        if not dist_info:
            return name, False, "dist-info not found"
        source_root = dist.locate_file("").resolve()
        copied, missing, errors = copy_record_files(
            dist_info,
            source_root,
            dest_root,
            move=False,
            per_package_subdir=True,
            skip_pyc=args.skip_pyc,
            warn_missing=True,
        )
        return (
            name,
            True,
            f"Copied {copied} files to {dest_root / name} (missing={missing}, errors={errors})",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(extract, name, dist): name for name, dist in selected}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                pkg_name, ok, msg = future.result()
                if ok:
                    logger.info("{}: {}", pkg_name, msg)
                else:
                    logger.error("{}: {}", pkg_name, msg)
            except Exception as exc:
                logger.exception("Package {} failed: {}", name, exc)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pkgtool.py",
        description="Copy, move, and repack Python packages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (for subcommands that use it).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_record = subparsers.add_parser(
        "record",
        help="Copy/move files listed in RECORD from cwd dist-info.",
    )
    p_record.add_argument(
        "packages",
        nargs="*",
        help="Package names to process.",
    )
    p_record.add_argument(
        "--all",
        action="store_true",
        help="Process all dist-info directories in the current directory.",
    )
    p_record.add_argument(
        "--move",
        action="store_true",
        help="Move files instead of copying them.",
    )
    p_record.add_argument(
        "--dest",
        default=None,
        help="Destination root (default: ~/tmp/1 with --move, else ~/tmp/packages).",
    )
    p_record.add_argument(
        "--per-package-subdir",
        action="store_true",
        default=None,
        help="Place files under <dest>/<pkg>/... (default: follows --move).",
    )
    p_record.add_argument(
        "--clean-record",
        action="store_true",
        help="Remove .pyc lines from RECORD files before processing.",
    )
    p_record.add_argument(
        "--skip-pyc",
        action="store_true",
        default=True,
        help="Skip .pyc files (default: True).",
    )
    p_record.add_argument(
        "--no-skip-pyc",
        action="store_false",
        dest="skip_pyc",
        help="Do not skip .pyc files.",
    )
    p_record.add_argument(
        "--warn-missing",
        action="store_true",
        default=True,
        help="Warn about missing files (default: True).",
    )
    p_record.add_argument(
        "--no-warn-missing",
        action="store_false",
        dest="warn_missing",
        help="Do not warn about missing files.",
    )
    p_site = subparsers.add_parser(
        "site-copy",
        help="Copy installed package directories from site-packages.",
    )
    p_site.add_argument("packages", nargs="+", help="Package names to copy.")
    p_site.add_argument(
        "--output",
        "-o",
        default="~/tmp/pkgs",
        help="Output base directory (default: ~/tmp/pkgs).",
    )
    p_site.add_argument(
        "--no-user-site",
        action="store_true",
        help="Do not include the user site-packages directory.",
    )
    p_site.add_argument(
        "--skip-pyc",
        action="store_true",
        default=True,
        help="Skip .pyc files (default: True).",
    )
    p_site.add_argument(
        "--no-skip-pyc",
        action="store_false",
        dest="skip_pyc",
        help="Do not skip .pyc files.",
    )
    p_repack = subparsers.add_parser(
        "repack",
        help="Repack installed packages into wheel-like directory structure.",
    )
    p_repack.add_argument(
        "--output",
        "-o",
        default="~/tmp/repack",
        help="Output base directory (default: ~/tmp/repack).",
    )
    p_repack.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output.",
    )
    p_repack.add_argument(
        "--skip-scan",
        action="store_true",
        help="Skip local scan and use the current active environment only.",
    )
    p_repack.add_argument(
        "--no-user-site",
        action="store_true",
        help="Do not include the user site-packages directory.",
    )
    p_ep = subparsers.add_parser(
        "entry-points",
        help="Extract packages that declare entry points.",
    )
    p_ep.add_argument(
        "patterns",
        nargs="*",
        help="Package names or wildcard patterns.",
    )
    ep_group = p_ep.add_mutually_exclusive_group()
    ep_group.add_argument(
        "--user-site-only",
        action="store_true",
        help="Only use the user site-packages directory (default).",
    )
    ep_group.add_argument(
        "--all-sites",
        action="store_true",
        help="Use all site-packages directories.",
    )
    p_ep.add_argument(
        "--all",
        action="store_true",
        help="Process all matching entry-point packages.",
    )
    p_ep.add_argument(
        "--list-only",
        action="store_true",
        help="Only list matching packages.",
    )
    p_ep.add_argument(
        "--show-entry-points",
        action="store_true",
        help="Show detailed entry-point information when listing.",
    )
    p_ep.add_argument(
        "--dest",
        default=None,
        help=("Destination root (default: ~/tmp/pkgs for --user-site-only, else ~/tmp/packages)."),
    )
    p_ep.add_argument(
        "--skip-pyc",
        action="store_true",
        default=True,
        help="Skip .pyc files (default: True).",
    )
    p_ep.add_argument(
        "--no-skip-pyc",
        action="store_false",
        dest="skip_pyc",
        help="Do not skip .pyc files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "verbose", False))
    if args.command == "record":
        return cmd_record(args)
    if args.command == "site-copy":
        return cmd_site_copy(args)
    if args.command == "repack":
        return cmd_repack(args)
    if args.command == "entry-points":
        return cmd_entry_points(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
