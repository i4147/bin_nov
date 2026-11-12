#!/data/data/com.termux/files/usr/bin/env python
"""Unified wheel toolkit: check, prune, strip, validate, size and PyPI queries."""

import argparse
import importlib.metadata
import json
from multiprocessing import Pool
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Any, Optional, Sequence
import zipfile

from dh import MAX_WORKERS

try:
    from packaging.tags import parse_tag
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    _HAVE_PACKAGING = True
except ImportError:
    _HAVE_PACKAGING = False
    parse_tag = None
    canonicalize_name = None
    Version = None
try:
    import requests

    _HAVE_REQUESTS = True
except ImportError:
    _HAVE_REQUESTS = False
    requests = None
try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

    _HAVE_RICH = True
except ImportError:
    _HAVE_RICH = False
    Console = None


def human_size(num_bytes: float) -> str:
    size = float(num_bytes)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024.0
        if abs(size) < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} PiB"


def run_command(
    cmd: Sequence[str],
    show_output: bool = False,
) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if show_output:
        if proc.stdout:
            sys.stdout.write(proc.stdout)
            sys.stdout.flush()
        if proc.stderr:
            sys.stderr.write(proc.stderr)
            sys.stderr.flush()
    return proc.returncode, proc.stdout, proc.stderr


def find_wheels(
    directory: Path,
    recursive: bool = False,
    exclude: Optional[Path] = None,
) -> list[Path]:
    iterator = directory.rglob("*.whl") if recursive else directory.glob("*.whl")
    wheels: list[Path] = []
    for candidate in iterator:
        if not candidate.is_file():
            continue
        if exclude is not None:
            try:
                candidate.relative_to(exclude)
                continue
            except ValueError:
                pass
        wheels.append(candidate)
    return sorted(wheels)


def read_wheel_metadata(whl: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        with zipfile.ZipFile(whl, "r") as zf:
            meta_name = next((n for n in zf.namelist() if n.endswith("METADATA")), None)
            if meta_name is None:
                return None, None
            name = version = None
            with zf.open(meta_name) as fh:
                for raw in fh:
                    line = raw.decode("utf-8", errors="ignore")
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                    if name and version:
                        return name, version
    except Exception as exc:
        print(f"Error reading {whl.name}: {exc}", file=sys.stderr)
    return None, None


def split_wheel_filename(whl: Path) -> tuple[str, str]:
    parts = whl.name.split("-")
    if len(parts) >= 3:
        return parts[0], parts[1]
    return whl.name, "unknown"


def parse_metadata_version(value: str):
    try:
        return tuple(int(part) for part in value.split(".") if part.isdigit())
    except Exception:
        return (value,)


_BAD_ROOT_SUFFIXES: tuple[str, ...] = (".py", ".pyc", ".pyd", ".so", ".dll")


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    suspicious = root / args.dest
    print(f"Scanning for .whl files recursively in: {root}\n")
    wheels = find_wheels(root, recursive=True, exclude=suspicious)
    if not wheels:
        print("No .whl files found.")
        return 0
    moved = 0
    for whl in wheels:
        try:
            with zipfile.ZipFile(whl, "r") as zf:
                names = zf.namelist()
                offenders = [
                    name
                    for name in names
                    if "/" not in name and not name.endswith("/") and name.endswith(_BAD_ROOT_SUFFIXES)
                ]
                if offenders:
                    moved += 1
                    rel = whl.relative_to(root)
                    print(f"MISCONFIGURED WHEEL: {rel}")
                    print(f"   Dumps into site-packages root: {offenders}")
                    suspicious.mkdir(parents=True, exist_ok=True)
                    target = suspicious / whl.name
                    if target.exists():
                        target = suspicious / f"{whl.stem}_duplicate_{moved}{whl.suffix}"
                    if args.dry_run:
                        print(f"   -> Would move to: {target.relative_to(root)}")
                    else:
                        shutil.move(str(whl), str(target))
                        print(f"   -> Moved to: {target.relative_to(root)}")
                    print("-" * 40)
        except zipfile.BadZipFile:
            print(f"Error: {whl.name} is a corrupt or invalid zip/wheel file.")
        except Exception as exc:
            print(f"Error processing {whl.name}: {exc}")
    verb = "Would move" if args.dry_run else "Moved"
    print(
        f"\nScan complete. {verb} {moved} misconfigured wheel(s) "
        f"to './{args.dest}/' out of {len(wheels)} total checked."
    )
    return 0


def wheel_has_entry_points(whl: Path) -> tuple[bool, Optional[str]]:
    try:
        with zipfile.ZipFile(whl, "r") as zf:
            for info in zf.filelist:
                if info.filename.endswith("entry_points.txt"):
                    return True, str(Path(info.filename).parent)
            return False, None
    except zipfile.BadZipFile:
        return False, None
    except Exception as exc:
        print(f"Error reading {whl}: {exc}", file=sys.stderr)
        return False, None


def cmd_entry_points(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.exists():
        print(f"Error: Directory '{directory}' not found", file=sys.stderr)
        return 1
    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory", file=sys.stderr)
        return 1
    wheels = find_wheels(directory, recursive=True)
    if not wheels:
        print(f"No .whl files found in '{directory}'")
        return 0
    print(f"Checking {len(wheels)} .whl file(s) in '{directory}'...\n")
    with_ep: list[tuple[Path, str, str, str]] = []
    without_ep: list[tuple[Path, str, str]] = []
    for whl in wheels:
        name, version = split_wheel_filename(whl)
        has_ep, dist_info = wheel_has_entry_points(whl)
        if has_ep:
            with_ep.append((whl, name, version, dist_info or ""))
        else:
            without_ep.append((whl, name, version))
    if with_ep:
        print("-" * 40)
        print(f"Found {len(with_ep)} wheel(s) with entry_points.txt:")
        print("-" * 40)
        for whl, name, version, dist_info in with_ep:
            print(f"\n{name} ({version})")
            print(f"   File: {whl}")
            print(f"   Dist-info: {dist_info}")
    else:
        print("No wheels found with entry_points.txt")
    if not args.quiet and without_ep:
        print("\n" + "=" * 40)
        print(f"{len(without_ep)} wheel(s) WITHOUT entry_points.txt:")
        print("-" * 40)
        if args.verbose:
            for whl, name, version in without_ep:
                print(f"   {name} ({version}): {whl}")
        else:
            print("   (use -v to see full list)")
    print("\n" + "=" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Total wheels checked:  {len(wheels)}")
    print(f"With entry_points.txt: {len(with_ep)}")
    print(f"Without:               {len(without_ep)}")
    return 0


def _query_pypi(name: str, timeout: float) -> int:
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception as exc:
        print(f"{name}: request failed: {exc}")
        return 1
    if resp.status_code == 200:
        data = resp.json()
        version = data["info"]["version"]
        for release_file in data["releases"][version]:
            if "pure" in release_file:
                print(f"{name}: pure={release_file['pure']}")
                print(f"  Has wheels: {'wheel' in release_file.get('packagetype', '')}")
                return 0
    print(f"{name}: Not found or no pure info")
    return 0


def cmd_pypi(args: argparse.Namespace) -> int:
    if not _HAVE_REQUESTS:
        print(
            "Error: the 'requests' package is required for the 'pypi' subcommand.\n"
            "       Install it with: pip install requests",
            file=sys.stderr,
        )
        return 2
    if not args.packages:
        print("Error: at least one package name is required.", file=sys.stderr)
        return 2
    rc = 0
    for pkg in args.packages:
        rc |= _query_pypi(pkg, timeout=args.timeout)
    return rc


def cmd_prune(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory", file=sys.stderr)
        return 2
    wheels = find_wheels(directory, recursive=False)
    if not wheels:
        print("Run this script in a directory containing .whl files.")
        return 1
    installed: dict[str, str] = {
        dist.metadata["Name"].lower(): dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    }
    for whl in wheels:
        name, version = read_wheel_metadata(whl)
        if not (name and version):
            continue
        current = installed.get(name.lower())
        if not current:
            continue
        try:
            current_tuple = parse_metadata_version(current)
            wheel_tuple = parse_metadata_version(version)
            if current_tuple == wheel_tuple:
                print(f"{name} == {version} already installed, deleting {whl.name}")
                if not args.dry_run:
                    whl.unlink()
            elif current_tuple > wheel_tuple:
                print(f"Installed version ({current}) is newer than wheel ({version}), deleting {whl.name}")
                if not args.dry_run:
                    whl.unlink()
        except TypeError:
            print(
                f"Warning: cannot compare versions '{current}' and '{version}'",
                file=sys.stderr,
            )
    return 0


_SO_RE = re.compile(r"\.so(\.\d+)*$")


def _strip_file(path: Path, strip_tool: str) -> None:
    run_command([strip_tool, str(path)], show_output=True)


def _strip_with_rich(
    targets: list[Path],
    strip_tool: str,
    total_bytes: int,
) -> None:
    console = Console()
    console.print(f"[bold cyan]Total number of .so files:[/] [bold yellow]{len(targets)}[/]")
    console.print(f"[bold cyan]Total size of .so files:[/] [bold yellow]{human_size(total_bytes)}[/]")
    console.print("[bold green]Starting .so stripping process...[/]")
    with Progress(
        TextColumn("[bold blue]{task.description}[/]"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[bold]{task.completed}/{task.total}[/]"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Stripping .so files...[/]", total=len(targets))
        for target in targets:
            _strip_file(target, strip_tool)
            progress.update(task, advance=1)
    console.print(f"[bold green]Done![/] Processed [bold yellow]{len(targets)}[/] .so files.")


def cmd_strip(args: argparse.Namespace) -> int:
    if shutil.which(args.strip_tool) is None:
        print(f"Error: '{args.strip_tool}' not found on PATH", file=sys.stderr)
        return 2
    if args.files:
        candidates = [Path(f) for f in args.files]
    else:
        root = Path(args.directory)
        if not root.is_dir():
            print(f"Error: '{root}' is not a directory", file=sys.stderr)
            return 2
        candidates = [p for p in root.rglob("*") if p.is_file()]
    targets = [p for p in candidates if p.is_file() and (p.suffix == ".so" or _SO_RE.search(p.name))]
    if not targets:
        print("No .so files found.")
        return 0
    total_before = sum(p.stat().st_size for p in targets)
    if _HAVE_RICH and not args.plain:
        _strip_with_rich(targets, args.strip_tool, total_before)
    else:
        print(f"Total number of .so files: {len(targets)}")
        print(f"Total size of .so files: {human_size(total_before)}")
        print("Starting .so stripping process...")
        for i, target in enumerate(targets, 1):
            _strip_file(target, args.strip_tool)
            print(f"  [{i}/{len(targets)}] {target}")
        print(f"Done! Processed {len(targets)} .so files.")
    total_after = sum(p.stat().st_size for p in targets if p.exists())
    if not (_HAVE_RICH and not args.plain):
        print(f"New total size of .so files: {human_size(total_after)}")
    else:
        Console().print(f"[bold cyan]New total size of .so files:[/] [bold yellow]{human_size(total_after)}[/]")
    return 0


_WHEEL_NAME_RE = re.compile(
    r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])" r"-([^-]+)-(\d[^-]*)-([^-]+)-([^-]+)-([^-]+)\.whl$",
    re.IGNORECASE,
)


def _matches_wheel_regex(whl: Path) -> bool:
    return _WHEEL_NAME_RE.match(whl.name) is not None


def _passes_structural_check(whl: Path) -> bool:
    if not _HAVE_PACKAGING:
        return True
    try:
        stem = whl.name[:-4]
        parts = stem.split("-")
        if len(parts) != 5:
            return False
        dist, version, py_tag, abi_tag, plat_tag = parts
        if canonicalize_name(dist) != dist.lower():
            return False
        try:
            Version(version)
        except Exception:
            return False
        if not py_tag[0].isdigit():
            return False
        try:
            parse_tag(py_tag + "-" + abi_tag + "-" + plat_tag.split("-")[-1])
        except Exception:
            return False
        return True
    except Exception:
        return False


def cmd_validate(args: argparse.Namespace) -> int:
    if not _HAVE_PACKAGING:
        print(
            "Error: the 'packaging' package is required for the 'validate' "
            "subcommand.\n       Install it with: pip install packaging",
            file=sys.stderr,
        )
        return 2
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory", file=sys.stderr)
        return 2
    if not args.move:
        print("to move wheels with invalid names rerun with -m")
    dest = directory / args.dest
    wheels = find_wheels(directory, recursive=args.recursive, exclude=dest)
    invalid_count = 0
    for whl in wheels:
        if _passes_structural_check(whl) and _matches_wheel_regex(whl):
            continue
        invalid_count += 1
        print(f"Invalid wheel name: {whl}")
        if args.move:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / whl.name
            if target.exists():
                target = dest / f"{whl.stem}_{invalid_count}{whl.suffix}"
            shutil.move(str(whl), str(target))
    if invalid_count == 0:
        print(f"All {len(wheels)} wheel name(s) are valid.")
    return 0


def _wheel_unpacked_size(whl: Path) -> tuple[Path, int, Optional[str]]:
    try:
        if not whl.exists():
            return whl, 0, f"File not found: {whl}"
        if not whl.is_file():
            return whl, 0, f"Not a file: {whl}"
        total = 0
        try:
            with zipfile.ZipFile(whl, "r") as zf:
                for info in zf.filelist:
                    total += info.file_size
        except Exception as exc:
            return whl, 0, f"Failed to read wheel: {exc}"
        return whl, total, None
    except Exception as exc:
        return whl, 0, f"Unexpected error: {exc}"


def cmd_size(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.exists():
        print(f"Error: Directory not found: {directory}", file=sys.stderr)
        return 1
    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}", file=sys.stderr)
        return 1
    print(f"Scanning directory: {directory}")
    if args.recursive:
        print("   (recursive mode)")
    wheels = find_wheels(directory, recursive=args.recursive)
    if not wheels:
        print("No .whl files found!")
        return 0
    print(f"Found {len(wheels)} .whl file(s)\n")
    jobs = max(1, MAX_WORKERS)
    print(f"Processing wheels ({jobs} worker(s))...\n")
    results: list[tuple[Path, int]] = []
    errors: list[tuple[Path, str]] = []
    with Pool(jobs) as pool:
        for whl, size, error in pool.imap_unordered(_wheel_unpacked_size, wheels):
            if error:
                errors.append((whl, error))
            else:
                results.append((whl, size))
    if args.sort == "size":
        results.sort(key=lambda item: item[1], reverse=True)
    else:
        results.sort(key=lambda item: item[0].name)
    total = sum(size for _, size in results)
    if args.json:
        payload = {
            "directory": str(directory),
            "recursive": args.recursive,
            "summary": {
                "total_wheels": len(wheels),
                "processed": len(results),
                "errors": len(errors),
                "total_unpacked_size_bytes": total,
                "total_unpacked_size_formatted": human_size(total),
            },
            "wheels": [
                {
                    "name": whl.name,
                    "path": str(whl),
                    "size_bytes": size,
                    "size_formatted": human_size(size),
                }
                for whl, size in results
            ],
        }
        if errors:
            payload["errors"] = [{"path": str(whl), "error": err} for whl, err in errors]
        print(json.dumps(payload, indent=2))
        return 0
    print("-" * 40)
    print(f"{'Wheel File':<50} {'Unpacked Size':>20}")
    print("-" * 40)
    for whl, size in results:
        label = whl.name
        if len(label) > 45:
            label = "..." + label[-42:]
        print(f"{label:<50} {human_size(size):>20}")
    print("-" * 40)
    print(f"{'TOTAL':<50} {human_size(total):>20}")
    print("-" * 40)
    if args.verbose:
        print("\nSummary:")
        print(f"   Total wheels found:      {len(wheels)}")
        print(f"   Successfully processed:  {len(results)}")
        print(f"   Errors:                  {len(errors)}")
        avg = human_size(total // len(results)) if results else "N/A"
        print(f"   Average size per wheel:  {avg}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for whl, err in errors:
            print(f"   - {whl.name}: {err}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wheel_tools.py",
        description=(
            "Unified command-line toolkit for inspecting, validating and cleaning Python wheel (*.whl) files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            original script mapping:
              check_wheels.py       ->  check [DIR]
              have_script.py        ->  entry-points [DIR]
              ispure.py             ->  pypi PKG [PKG ...]
              mip.py                ->  prune [DIR]
              strep.py              ->  strip [FILES ...]
              valwheel.py           ->  validate [DIR]
              whl_unpacked_size.py  ->  size [-d DIR]
        """),
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{check,entry-points,pypi,prune,strip,validate,size}",
    )
    p = sub.add_parser(
        "check",
        help="find wheels that dump code into site-packages root",
        description="Find and relocate wheels that ship top-level importable files (.py/.pyc/.pyd/.so/.dll).",
    )
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory to scan recursively (default: '.')",
    )
    p.add_argument(
        "-d",
        "--dest",
        default="suspicious",
        help="subdirectory to move bad wheels into (default: 'suspicious')",
    )
    p.add_argument("--dry-run", action="store_true", help="only report, do not move any files")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser(
        "entry-points",
        help="find wheels that contain entry_points.txt",
        description="List wheels that declare entry points.",
    )
    p.add_argument("directory", nargs="?", default=".", help="directory to scan (default: '.')")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the full list of wheels without entry_points.txt",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only show wheels WITH entry_points.txt",
    )
    p.set_defaults(func=cmd_entry_points)
    p = sub.add_parser(
        "pypi",
        help="query PyPI for 'pure' info about packages (requires requests)",
        description="Query the PyPI JSON API for one or more package names.",
    )
    p.add_argument("packages", nargs="*", help="package name(s) to query")
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    p.set_defaults(func=cmd_pypi)
    p = sub.add_parser(
        "prune",
        help="delete wheels already installed at >= version",
        description="Delete wheels whose distribution is already installed at the same or a newer version.",
    )
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory containing .whl files (default: '.')",
    )
    p.add_argument("--dry-run", action="store_true", help="only report, do not delete any files")
    p.set_defaults(func=cmd_prune)
    p = sub.add_parser(
        "strip",
        help="strip .so files (requires the 'strip' binary)",
        description="Strip debug symbols from loose .so files.",
    )
    p.add_argument("files", nargs="*", help="specific .so files to strip (default: scan -d)")
    p.add_argument(
        "-d",
        "--directory",
        default=".",
        help="directory to scan recursively (default: '.')",
    )
    p.add_argument(
        "--strip-tool",
        default="strip",
        help="name or path of the strip binary (default: 'strip')",
    )
    p.add_argument(
        "--plain",
        action="store_true",
        help="disable rich progress output even if rich is installed",
    )
    p.set_defaults(func=cmd_strip)
    p = sub.add_parser(
        "validate",
        help="validate wheel filenames (requires packaging)",
        description="Validate that wheel filenames match PEP 427.",
    )
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory containing .whl files (default: '.')",
    )
    p.add_argument("-m", "--move", action="store_true", help="move invalid wheels into --dest")
    p.add_argument(
        "-d",
        "--dest",
        default="invalid_wheels",
        help="destination subdirectory (default: 'invalid_wheels')",
    )
    p.add_argument("-r", "--recursive", action="store_true", help="scan subdirectories recursively")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser(
        "size",
        help="report total unpacked size of .whl files",
        description="Report total unpacked size (sum of uncompressed sizes) of .whl files.",
    )
    p.add_argument(
        "-d",
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="directory to scan (default: current directory)",
    )
    p.add_argument("-r", "--recursive", action="store_true", help="scan subdirectories recursively")
    p.add_argument("-v", "--verbose", action="store_true", help="show detailed summary")
    p.add_argument("--json", action="store_true", help="output results as JSON")
    p.add_argument(
        "-s",
        "--sort",
        choices=["name", "size"],
        default="name",
        help="sort output by name or size (default: name)",
    )
    p.set_defaults(func=cmd_size)
    return parser


def print_usage() -> None:
    usage = f"""
# Recursive scan, dry-run (nothing touched)
python wheel_tools.py check ./wheels --dry-run
# Suppress the "without entry points" list entirely
python wheel_tools.py entry-points . -q
# PyPI lookup with a longer HTTP timeout
python wheel_tools.py pypi flask --timeout 30
# Preview which wheels would be pruned
python wheel_tools.py prune . --dry-run
# Plain (no rich) strip output for CI logs
python wheel_tools.py strip -d ./native --plain
# Validate + auto-move invalid names into ./badwheels
python wheel_tools.py validate . -m -d badwheels -r
# Machine-readable unpacked-size report
python wheel_tools.py size -d ./wheels -r --json -s size
"""
    print(usage)


def main(argv: Optional[Sequence[str]] = None) -> int:
    print_usage()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
