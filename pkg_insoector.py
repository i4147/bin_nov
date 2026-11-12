#!/data/data/com.termux/files/usr/bin/env python
"""Inspect the installed Python packages for duplicates, versions and scripts.
Subcommands: ``duplicates`` finds the packages in both the system and user site-packages, ``multi-version`` the repeated versions, ``missing-scripts``/``entrypoints`` check the console scripts, ``binary`` lists the packages with compiled extensions, ``orphans`` the files no distribution owns and ``small`` the undersized packages; ``--report`` and ``--split`` export the results.
Usage: pkg_insoector {duplicates,multi-version,missing-scripts,entrypoints,binary,orphans,small} [options] Positional arguments: input Path to the input JSON file.
Subcommands: duplicates Packages in both system and user site-packages.
multi-version Packages with more than one installed version.
missing-scripts Check that console-script shims exist in bin/.
entrypoints Analyze entry_points.txt / console-script presence.
binary List non-pure (compiled-extension) packages.
orphans Detect unowned files in system site-packages.
small List packages smaller than a threshold.
zpkg-list List pure single-top-level user-site packages.
git-urls Extract git repository URLs for installed packages.
save-deb Dump installed dpkg binary packages.
save-keys Extract 'pkgname' values from a JSON file.
rename-node Rename node_modules .../package dirs safely.
Options: --method Detection strategy (default: dist-info).
--report Optional path to write a report.
--mode --user-only Scan only the user site-packages directory.
--system-only Scan only system site-packages directories.
--write-files Classify mode: write noep_pure.txt / ep_*.txt etc.
--split Write pure.txt + notpure.txt instead of printing.
-o, --output Write non-pure package list to FILE.
--pure-output --split: pure list filename (default: pure.txt).
--nonpure-output --split: non-pure list filename (default: notpure.txt).
...
plus 5 more; run ``pkg_insoector --help`` for the full list.
Examples: $ pkg_insoector multi-version --report dupes.txt Command-line script: run ``python pkg_insoector.py`` or the ``pkg_insoector`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import configparser
import contextlib
import csv
from datetime import datetime
from importlib import metadata
import json
import os
from pathlib import Path
import re
import site
import subprocess
import sys
from typing import Any, Iterable, Iterator, Sequence

BINARY_EXTS: tuple[str, ...] = (".so", ".pyd", ".dll", ".dylib")
GIT_HOSTS: tuple[str, ...] = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "sourcehut.org",
    "codeberg.org",
)


def system_site_dirs() -> list[Path]:
    return [Path(p) for p in site.getsitepackages() if Path(p).exists()]


def user_site_dir() -> Path | None:
    with contextlib.suppress(Exception):
        p = Path(site.getusersitepackages())
        if p.exists():
            return p
    return None


def all_site_dirs(include_user: bool = True) -> list[Path]:
    dirs = system_site_dirs()
    if include_user:
        u = user_site_dir()
        if u:
            dirs.append(u)
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def pick_scan_dirs(user_only: bool, system_only: bool) -> list[Path]:
    if user_only:
        u = user_site_dir()
        return [u] if u else []
    if system_only:
        return system_site_dirs()
    return all_site_dirs()


def parse_dist_name(name: str) -> tuple[str | None, str | None]:
    m = re.match(r"^(.+?)-(\d+.*?)(\.dist-info|\.egg-info)$", name)
    return (m.group(1).lower(), m.group(2)) if m else (None, None)


def strip_dist_suffix(name: str) -> str:
    for suf in (".dist-info", ".egg-info"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    for pat in (r"-\d+\.\d+\.\d+.*$", r"-\d+\.\d+.*$", r"-py\d+\.\d+$", r"-py\d+$"):
        name = re.sub(pat, "", name)
    return name


def read_metadata_version(path: Path) -> str | None:
    for fname in ("METADATA", "PKG-INFO"):
        f = path / fname
        if f.exists():
            with contextlib.suppress(Exception):
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
    return None


def has_binary_ext(pkg_dir: Path, exts: Sequence[str] = BINARY_EXTS) -> bool:
    try:
        for f in pkg_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts:
                return True
    except (PermissionError, OSError):
        pass
    return False


def parse_entry_points(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"console_scripts": [], "gui_scripts": [], "other": []}
    if not path or not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text)
        if cp.has_section("console_scripts"):
            out["console_scripts"].extend(cp.options("console_scripts"))
        if cp.has_section("gui_scripts"):
            out["gui_scripts"].extend(cp.options("gui_scripts"))
        for sec in cp.sections():
            if sec not in ("console_scripts", "gui_scripts"):
                for opt in cp.options(sec):
                    out["other"].append(f"{sec}:{opt}")
    except configparser.Error:
        cur: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                cur = line[1:-1]
            elif cur and "=" in line:
                key = line.split("=", 1)[0].strip()
                if cur == "console_scripts":
                    out["console_scripts"].append(key)
                elif cur == "gui_scripts":
                    out["gui_scripts"].append(key)
                else:
                    out["other"].append(f"{cur}:{key}")
    return out


def find_bin_dir() -> Path | None:
    for cand in (
        Path(sys.prefix) / "bin",
        Path("/data/data/com.termux/files/usr/bin"),
        Path("/usr/bin"),
        Path("/usr/local/bin"),
    ):
        if cand.exists():
            return cand
    return None


def iter_dist_infos(dirs: Iterable[Path]) -> Iterator[Path]:
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_dir() and entry.name.endswith((".dist-info", ".egg-info")):
                    yield entry
        except (PermissionError, OSError):
            continue


def _is_within(needle: str, haystack: Sequence[str]) -> bool:
    return any(needle.startswith(h) for h in haystack)


def _iter_metadata_in_dirs(dirs: Sequence[Path]) -> Iterator[Path]:
    dir_strs = [str(d.resolve()) for d in dirs]
    for dist in metadata.distributions():
        try:
            dpath = getattr(dist, "_path", None)
            if not dpath:
                continue
            dpath_s = str(Path(dpath).resolve())
            if _is_within(dpath_s, dir_strs):
                yield dist, Path(dpath)
        except Exception:
            continue


def _distinfo_packages(dirs: Sequence[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_dir():
                    if entry.name.endswith((".dist-info", ".egg-info")):
                        name, ver = parse_dist_name(entry.name)
                        if name:
                            out[name] = ver or "?"
                    elif (entry / "__init__.py").exists():
                        out.setdefault(entry.name.lower(), "?")
                elif entry.is_file() and entry.suffix == ".py":
                    out.setdefault(entry.stem.lower(), "?")
        except PermissionError:
            continue
    return out


def _metadata_packages(dirs: Sequence[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dist, _ in _iter_metadata_in_dirs(dirs):
        try:
            name = (dist.metadata.get("Name") or "").lower()
            ver = dist.metadata.get("Version") or "?"
            if name:
                out[name] = ver
        except Exception:
            continue
    return out


def cmd_duplicates(args: argparse.Namespace) -> int:
    system = system_site_dirs()
    user = user_site_dir()
    if not user:
        print("No user site-packages directory found.")
        return 0
    if args.method == "metadata":
        sys_pkgs = _metadata_packages(system)
        usr_pkgs = _metadata_packages([user])
    else:
        sys_pkgs = _distinfo_packages(system)
        usr_pkgs = _distinfo_packages([user])
    dupes = sorted(set(sys_pkgs) & set(usr_pkgs))
    print(f"System packages: {len(sys_pkgs)}")
    print(f"User packages:   {len(usr_pkgs)}")
    print(f"Duplicate:       {len(dupes)}")
    for name in dupes:
        sv = sys_pkgs.get(name, "?")
        uv = usr_pkgs.get(name, "?")
        note = ""
        if sv != "?" and uv not in ("?", sv):
            note = "  [version mismatch]"
        print(f"  {name}: system={sv}, user={uv}{note}")
    return 0


def cmd_multi_version(_: argparse.Namespace) -> int:
    versions: dict[str, set[str]] = defaultdict(set)
    for di in iter_dist_infos(all_site_dirs()):
        name, ver = parse_dist_name(di.name)
        if name and ver:
            versions[name].add(ver)
    found = False
    for name, vers in sorted(versions.items()):
        if len(vers) > 1:
            found = True
            print(f"\nPackage: {name}")
            for v in sorted(vers):
                print(f"  - Version: {v}")
    if not found:
        print("No packages with multiple versions found.")
    return 0


def cmd_missing_scripts(args: argparse.Namespace) -> int:
    bin_dir = find_bin_dir()
    if not bin_dir:
        print("ERROR: could not find bin directory", file=sys.stderr)
        return 1
    scan_dir = user_site_dir()
    if scan_dir is None:
        dirs = system_site_dirs()
        if not dirs:
            print("ERROR: could not find site-packages directory", file=sys.stderr)
            return 1
        scan_dir = dirs[0]
    print(f"Site-packages: {scan_dir}")
    print(f"Bin directory: {bin_dir}")
    print(f"Python:        {sys.version.split()[0]}")
    results: list[tuple[str, list[str], list[str]]] = []
    for di in sorted(scan_dir.glob("*.dist-info")):
        ep = di / "entry_points.txt"
        if not ep.exists():
            continue
        scripts = parse_entry_points(ep)["console_scripts"]
        if not scripts:
            continue
        missing = [s for s in scripts if not (bin_dir / s).exists()]
        results.append((di.name.replace(".dist-info", ""), scripts, missing))
    broken = [r for r in results if r[2]]
    total_missing = sum(len(r[2]) for r in broken)
    print()
    print(f"Packages with console_scripts: {len(results)}")
    print(f"Packages with missing shims:   {len(broken)}")
    print(f"Total missing shims:           {total_missing}")
    for name, _all, missing in broken:
        print(f"\n{name}:")
        for s in missing:
            print(f"  missing: {s}")
    if args.report:
        report = Path(args.report).expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"Missing script report — {datetime.now().isoformat()}", ""]
        for name, _all, missing in broken:
            lines.append(f"{name}: {', '.join(missing)}")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nReport written to: {report}")
    return 1 if broken else 0


def _entrypoints_pip_show(_: argparse.Namespace) -> int:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
        pkgs = [p["name"] for p in json.loads(r.stdout)]
    except Exception as e:
        print(f"Error listing packages: {e}", file=sys.stderr)
        return 1
    hits: list[str] = []
    total = len(pkgs)
    for i, name in enumerate(pkgs, 1):
        print(f"[{i}/{total}] {name}...", end="\r", flush=True)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "show", "-f", name],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            continue
        has_bin = False
        for line in r.stdout.splitlines():
            low = line.strip().lower()
            if "bin/" not in low and "scripts/" not in low:
                continue
            ext = (str(Path(low).with_suffix("")), Path(low).suffix)[1]
            if ext in (".py", "") and not any(s in low for s in ("__pycache__", ".dist-info", ".egg-info", ".pth")):
                has_bin = True
                break
        if has_bin:
            hits.append(name)
    print()
    for n in hits:
        print(n)
    print(f"\nTotal packages with bin/ scripts: {len(hits)}")
    return 0


def _find_entry_points_file(entry: Path, site_dir: Path, name: str) -> Path | None:
    direct = entry / "entry_points.txt"
    if direct.exists():
        return direct
    patterns = (
        f"{name}*.dist-info",
        f"{name.replace('-', '_')}*.dist-info",
        f"{name}*.egg-info",
        f"{name.replace('-', '_')}*.egg-info",
    )
    for pat in patterns:
        for cand in site_dir.glob(pat):
            if cand.is_dir():
                ep = cand / "entry_points.txt"
                if ep.exists():
                    return ep
    return None


def cmd_entrypoints(args: argparse.Namespace) -> int:
    if args.mode == "pip-show":
        return _entrypoints_pip_show(args)
    dirs = pick_scan_dirs(args.user_only, args.system_only)
    seen: set[str] = set()
    with_ep: set[str] = set()
    without_ep: set[str] = set()
    pure_noep: set[str] = set()
    nonpure_noep: set[str] = set()
    pure_ep: set[str] = set()
    nonpure_ep: set[str] = set()
    for site_dir in dirs:
        if not site_dir.is_dir():
            continue
        try:
            entries = list(site_dir.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            is_pkg = (entry / "__init__.py").exists()
            is_dist = entry.name.endswith((".dist-info", ".egg-info"))
            if not (is_pkg or is_dist):
                continue
            name = strip_dist_suffix(entry.name)
            if not name or name in seen:
                continue
            seen.add(name)
            has_ep = _find_entry_points_file(entry, site_dir, name) is not None
            if is_dist:
                pkg_search = site_dir / name
                if not pkg_search.exists():
                    pkg_search = site_dir / name.replace("-", "_")
                if not pkg_search.exists():
                    pkg_search = entry
            else:
                pkg_search = entry
            is_pure = not has_binary_ext(pkg_search)
            if has_ep:
                with_ep.add(name)
                (pure_ep if is_pure else nonpure_ep).add(name)
            else:
                without_ep.add(name)
                (pure_noep if is_pure else nonpure_noep).add(name)
    if args.mode == "list-with-script":
        for n in sorted(with_ep):
            print(n)
        return 0
    if args.mode == "list-no-script":
        for n in sorted(without_ep):
            print(n)
        return 0
    print(f"Pure packages, no entry_points:      {len(pure_noep)}")
    print(f"Non-pure packages, no entry_points:  {len(nonpure_noep)}")
    print(f"Pure packages, with entry_points:    {len(pure_ep)}")
    print(f"Non-pure packages, with entry_points:{len(nonpure_ep)}")
    print(f"Grand total:                         {len(pure_noep) + len(nonpure_noep) + len(pure_ep) + len(nonpure_ep)}")
    if args.write_files:
        Path("noep_pure.txt").write_text("\n".join(sorted(pure_noep)), encoding="utf-8")
        Path("noep_nopure.txt").write_text("\n".join(sorted(nonpure_noep)), encoding="utf-8")
        Path("ep_pure.txt").write_text("\n".join(sorted(pure_ep)), encoding="utf-8")
        Path("ep_nopure.txt").write_text("\n".join(sorted(nonpure_ep)), encoding="utf-8")
        print("Wrote classification files: noep_pure.txt, noep_nopure.txt, ep_pure.txt, ep_nopure.txt")
    return 0


def cmd_binary(args: argparse.Namespace) -> int:
    dirs = pick_scan_dirs(args.user_only, args.system_only)
    pure: set[str] = set()
    nonpure: set[str] = set()
    for dist, _ in _iter_metadata_in_dirs(dirs):
        try:
            name = dist.metadata.get("Name")
            if not name:
                continue
            is_bin = any(str(f).lower().endswith(BINARY_EXTS) for f in (dist.files or []))
            (nonpure if is_bin else pure).add(name)
        except Exception:
            continue
    pure_sorted = sorted(pure, key=str.lower)
    nonpure_sorted = sorted(nonpure, key=str.lower)
    if args.split:
        Path(args.pure_output).write_text("\n".join(pure_sorted) + "\n", encoding="utf-8")
        Path(args.nonpure_output).write_text("\n".join(nonpure_sorted) + "\n", encoding="utf-8")
        print(f"Wrote {len(pure_sorted):4d} pure packages  → {args.pure_output}")
        print(f"Wrote {len(nonpure_sorted):4d} non-pure packages → {args.nonpure_output}")
        return 0
    if args.output:
        Path(args.output).write_text("\n".join(nonpure_sorted) + "\n", encoding="utf-8")
    print(f"Binary (non-pure) packages: {len(nonpure_sorted)}")
    for n in nonpure_sorted:
        print(f"  {n}")
    if args.output:
        print(f"\nList written to: {args.output}")
    return 0


_IGNORABLE_SUFFIXES: set[str] = {".pyc", ".pyo", ".pyd", ".egg-link", ".pth"}


def _is_ignorable(p: Path) -> bool:
    s = str(p).lower()
    if any(x in s for x in ("__pycache__", ".dist-info", ".egg-info")):
        return True
    if p.suffix in _IGNORABLE_SUFFIXES:
        return True
    return p.name in ("easy-install.pth", "site.py")


def cmd_orphans(args: argparse.Namespace) -> int:
    site_dirs = system_site_dirs()
    if not site_dirs:
        print("No system site-packages directories found.", file=sys.stderr)
        return 1
    package_files: set[str] = set()
    dir_strs = [str(d.resolve()) for d in site_dirs]
    for dist in metadata.distributions():
        try:
            dpath = getattr(dist, "_path", None)
            if not dpath:
                continue
            dpath_p = Path(dpath).resolve()
            if not _is_within(str(dpath_p), dir_strs):
                continue
            for f in dist.files or []:
                try:
                    fp = Path(dist.locate_file(f)).resolve()
                    package_files.add(str(fp))
                except Exception:
                    continue
            rec = dpath_p / "RECORD"
            if rec.exists():
                with contextlib.suppress(Exception), rec.open(encoding="utf-8") as fh:
                    for row in csv.reader(fh):
                        if row:
                            fp = (dpath_p.parent / row[0]).resolve()
                            package_files.add(str(fp))
        except Exception:
            continue
    orphans: set[Path] = set()
    for site_dir in site_dirs:
        for root, dirs, files in os.walk(site_dir):
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
            root_p = Path(root)
            for f in files:
                fp = (root_p / f).resolve()
                if str(fp) in package_files:
                    continue
                if _is_ignorable(fp):
                    continue
                orphans.add(fp)
    sorted_orphans = sorted(orphans)
    print(f"Found {len(sorted_orphans)} orphan file(s):")
    for o in sorted_orphans:
        if args.verbose and o.is_file():
            print(f"  {o} ({o.stat().st_size} bytes)")
        else:
            print(f"  {o}")
    print("\nWARNING: review these files carefully before removing them.")
    if args.export:
        out = Path(args.output).expanduser().resolve()
        out.write_text(
            json.dumps(
                {
                    "site_directories": [str(d) for d in site_dirs],
                    "count": len(sorted_orphans),
                    "files": [str(o) for o in sorted_orphans],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nOrphan file list exported to: {out}")
    return 0


def cmd_small(args: argparse.Namespace) -> int:
    results: list[tuple[str, int]] = []
    for dist, _ in _iter_metadata_in_dirs(all_site_dirs()):
        try:
            name = dist.metadata.get("Name")
            if not name:
                continue
            total = 0
            for f in dist.files or []:
                try:
                    fp = dist.locate_file(f)
                    if fp.is_file():
                        total += fp.stat().st_size
                except (OSError, FileNotFoundError):
                    continue
            if total < args.threshold:
                results.append((name, total))
        except Exception:
            continue
    results.sort(key=lambda x: x[0].lower())
    for name, size in results:
        print(f"{name}\t{size}")
    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(n for n, _ in results) + "\n", encoding="utf-8")
        print(f"\nWrote {len(results)} names to {out}")
    print(f"\nTotal: {len(results)} package(s) under {args.threshold} bytes")
    return 0


def cmd_zpkg_list(args: argparse.Namespace) -> int:
    user_lib = (Path.home() / ".local" / "lib").resolve()
    results: list[str] = []
    seen: set[str] = set()
    for dist in metadata.distributions():
        try:
            dpath = getattr(dist, "_path", None)
            if not dpath:
                continue
            dpath_p = Path(dpath).resolve()
            if str(user_lib) not in str(dpath_p):
                continue
            name = dist.name
            if not name or "-" in name or "_" in name:
                continue
            if name in seen:
                continue
            is_binary = any(f.suffix in (".so", ".pyd", ".dylib") for f in (dist.files or []))
            if is_binary:
                continue
            tl = dpath_p / "top_level.txt"
            if tl.exists():
                top = {ln.strip() for ln in tl.read_text().splitlines() if ln.strip()}
            else:
                top = set()
                for f in dist.files or []:
                    parts = f.parts
                    if parts and not parts[0].endswith(".dist-info"):
                        top.add(parts[0])
            if len(top) != 1:
                continue
            seen.add(name)
            results.append(name)
        except Exception:
            continue
    results.sort(key=str.lower)
    out = Path(args.output).expanduser().resolve() if args.output else Path.home() / "list.txt"
    out.write_text("\n".join(results) + "\n", encoding="utf-8")
    print(f"Saved {len(results)} package names to {out}")
    return 0


def cmd_git_urls(args: argparse.Namespace) -> int:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
        pkgs = [p["name"] for p in json.loads(r.stdout)]
    except Exception as e:
        print(f"Error listing packages: {e}", file=sys.stderr)
        return 1
    result: dict[str, str | None] = {}
    total = len(pkgs)
    for i, name in enumerate(pkgs, 1):
        print(f"[{i}/{total}] {name}...", end="\r", flush=True)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "show", name],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            result[name] = None
            continue
        info: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        candidates: list[str] = []
        home = info.get("Home-page", "").strip()
        if home and home.lower() not in ("none", "unknown", ""):
            candidates.append(home)
        proj = info.get("Project-URLs") or info.get("Project-URL", "")
        for line in proj.splitlines():
            line = line.strip()
            if not line:
                continue
            label, sep, url = line.partition(",")
            if sep:
                if any(k in label.strip().lower() for k in ("source", "repo", "code", "git", "homepage")):
                    candidates.insert(0, url.strip())
                else:
                    candidates.append(url.strip())
        dl = info.get("Download-URL", "").strip()
        if dl and dl.lower() not in ("none", "unknown", ""):
            candidates.append(dl)
        url = None
        for c in candidates:
            if any(h in c for h in GIT_HOSTS):
                url = c.rstrip("/")
                break
        result[name] = url
    print()
    with_url = sum(1 for v in result.values() if v)
    out = Path(args.output).expanduser().resolve() if args.output else Path.home() / "pkg_git_urls.json"
    out.write_text(
        json.dumps(
            {
                "summary": {
                    "total": len(result),
                    "with_git_url": with_url,
                    "without_git_url": len(result) - with_url,
                },
                "packages": result,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"{with_url}/{len(result)} packages have a git URL.")
    print(f"Results saved to: {out}")
    return 0


def cmd_save_deb(args: argparse.Namespace) -> int:
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${binary:Package}\n"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print("dpkg-query not found. Are you on a Debian-based system?", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(e.stderr.strip(), file=sys.stderr)
        return 1
    pkgs = sorted(p for p in r.stdout.splitlines() if p)
    out = Path(args.output).expanduser().resolve()
    out.write_text("\n".join(pkgs) + "\n", encoding="utf-8")
    print(f"Saved {len(pkgs)} packages to {out}")
    return 0


def cmd_save_keys(args: argparse.Namespace) -> int:
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        return 1
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        print("Error: JSON root must be an object or list", file=sys.stderr)
        return 1
    keys: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            print(
                f"Warning: item at index {i} is not an object, skipping",
                file=sys.stderr,
            )
            continue
        if "pkgname" not in item:
            print(f"Warning: item at index {i} lacks 'pkgname', skipping", file=sys.stderr)
            continue
        keys.append(item["pkgname"])
    out = Path(args.output).expanduser().resolve()
    out.write_text("\n".join(keys) + "\n", encoding="utf-8")
    print(f"Wrote {len(keys)} pkgname values to {out}")
    return 0


def cmd_rename_node(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else Path.cwd()

    def safe_name(name: str) -> str:
        name = name.lstrip("@").replace("/", "__")
        return re.sub(r"[^\w.-]", "_", name)

    for pkg_json in root.rglob("package.json"):
        parent = pkg_json.parent
        if parent.name != "package":
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = data.get("name")
        if not name:
            continue
        target_name = safe_name(name)
        target = parent.parent / target_name
        if parent.name == target_name:
            continue
        if target.exists():
            print(f"[SKIP] {target} already exists")
            continue
        if args.dry_run:
            print(f"[DRY]  {parent} -> {target}")
        else:
            print(f"[RENAME] {parent} -> {target}")
            parent.rename(target)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pkg_inspector.py",
        description="Unified Python package inspection toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("duplicates", help="Packages in both system and user site-packages.")
    sp.add_argument(
        "--method",
        choices=("dist-info", "metadata"),
        default="dist-info",
        help="Detection strategy (default: dist-info).",
    )
    sp.set_defaults(func=cmd_duplicates)
    sp = sub.add_parser("multi-version", help="Packages with more than one installed version.")
    sp.set_defaults(func=cmd_multi_version)
    sp = sub.add_parser("missing-scripts", help="Check that console-script shims exist in bin/.")
    sp.add_argument("--report", metavar="FILE", help="Optional path to write a report.")
    sp.set_defaults(func=cmd_missing_scripts)
    sp = sub.add_parser("entrypoints", help="Analyze entry_points.txt / console-script presence.")
    sp.add_argument(
        "--mode",
        required=True,
        choices=("list-with-script", "list-no-script", "pip-show", "classify"),
    )
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument(
        "--user-only",
        action="store_true",
        help="Scan only the user site-packages directory.",
    )
    grp.add_argument(
        "--system-only",
        action="store_true",
        help="Scan only system site-packages directories.",
    )
    sp.add_argument(
        "--write-files",
        action="store_true",
        help="classify mode: write noep_pure.txt / ep_*.txt etc.",
    )
    sp.set_defaults(func=cmd_entrypoints)
    sp = sub.add_parser("binary", help="List non-pure (compiled-extension) packages.")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--user-only", action="store_true")
    grp.add_argument("--system-only", action="store_true")
    sp.add_argument(
        "--split",
        action="store_true",
        help="Write pure.txt + notpure.txt instead of printing.",
    )
    sp.add_argument("-o", "--output", metavar="FILE", help="Write non-pure package list to FILE.")
    sp.add_argument(
        "--pure-output",
        default="pure.txt",
        help="--split: pure list filename (default: pure.txt).",
    )
    sp.add_argument(
        "--nonpure-output",
        default="notpure.txt",
        help="--split: non-pure list filename (default: notpure.txt).",
    )
    sp.set_defaults(func=cmd_binary)
    sp = sub.add_parser("orphans", help="Detect unowned files in system site-packages.")
    sp.add_argument("-v", "--verbose", action="store_true", help="Show file sizes.")
    sp.add_argument("-e", "--export", action="store_true", help="Write results as JSON.")
    sp.add_argument(
        "-o",
        "--output",
        default="orphan_files.json",
        help="JSON export path (default: orphan_files.json).",
    )
    sp.set_defaults(func=cmd_orphans)
    sp = sub.add_parser("small", help="List packages smaller than a threshold.")
    sp.add_argument(
        "--threshold",
        type=int,
        default=1024 * 1024,
        help="Size threshold in bytes (default: 1 MiB).",
    )
    sp.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Write package names (one per line) to FILE.",
    )
    sp.set_defaults(func=cmd_small)
    sp = sub.add_parser("zpkg-list", help="List pure single-top-level user-site packages.")
    sp.add_argument("-o", "--output", help="Output file (default: ~/list.txt).")
    sp.set_defaults(func=cmd_zpkg_list)
    sp = sub.add_parser("git-urls", help="Extract git repository URLs for installed packages.")
    sp.add_argument("-o", "--output", help="Output JSON file (default: ~/pkg_git_urls.json).")
    sp.set_defaults(func=cmd_git_urls)
    sp = sub.add_parser("save-deb", help="Dump installed dpkg binary packages.")
    sp.add_argument(
        "-o",
        "--output",
        default="installed_packages_deb.txt",
        help="Output file (default: installed_packages_deb.txt).",
    )
    sp.set_defaults(func=cmd_save_deb)
    sp = sub.add_parser("save-keys", help="Extract 'pkgname' values from a JSON file.")
    sp.add_argument("input", help="Path to the input JSON file.")
    sp.add_argument("-o", "--output", default="keys.txt", help="Output file (default: keys.txt).")
    sp.set_defaults(func=cmd_save_keys)
    sp = sub.add_parser("rename-node", help="Rename node_modules .../package dirs safely.")
    sp.add_argument("--root", help="Root directory to scan (default: cwd).")
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be renamed without touching anything.",
    )
    sp.set_defaults(func=cmd_rename_node)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
