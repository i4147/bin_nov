#!/data/data/com.termux/files/usr/bin/env python
"""Repack the installed packages into wheels, and the unpacked wheels back into ``.whl`` files.
The ``repack`` subcommand rebuilds the wheels from a site-packages directory (from ``RECORD`` or from the plain directories), computing the hash lines and the platform/ABI tags, and ``pack-dirs`` zips the pre-unpacked wheel folders back, with ``--packages``/``--all`` and ``--source``/``--site-packages`` selection.
Usage: repacktools {repack,pack-dirs} [options] Subcommands: repack Repack installed packages from a site-packages dir into wheels pack-dirs Pack pre-unpacked wheel directories into .whl files Options: -v, --verbose Verbose output --source Where to look for site-packages (default: current) --site-packages Explicit site-packages directory (overrides --source) --output Output directory for wheels (default: ~/tmp/whl) --packages Repack only these packages (by name) --all Repack every package (explicit; default is implicit if --packages omitted) --method Simple = copy dirs; record = rebuild from RECORD (default: simple) --on-missing Behaviour for missing files in RECORD mode (default: warn) --missing-dir Where to stash packages that fail (--on-missing copy) --builder Low-level wheel writer (default: zip) ...
plus 7 more; run ``repacktools --help`` for the full list.
Examples: $ repacktools repack --site-packages ~/site-pkgs --output ~/tmp/whl --all Command-line script: run ``python repacktools.py`` or the ``repacktools`` command.
The module is importable as well."""

import argparse
import asyncio
import base64
import contextlib
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
import email
import hashlib
import json
from multiprocessing import Pool, cpu_count
from pathlib import Path
import platform
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any, Iterable, Optional, Sequence
import zipfile

from dh import MAX_WORKERS
from loguru import logger


try:
    from wheel.wheelfile import WheelFile

    _HAS_WHEEL = True
except ImportError:
    WheelFile = None
    _HAS_WHEEL = False
try:
    from tqdm import tqdm as _tqdm

    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False

    def _tqdm(it: Iterable[Any], **_kw: Any):
        return it


log = logger


def _b64_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return _b64_nopad(h.digest())


def _hash_bytes(data: bytes) -> str:
    return _b64_nopad(hashlib.sha256(data).digest())


def _human_size(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


def py_version_tag() -> str:
    a, b = sys.version_info[:2]
    return f"py{a}{b}"


def abi_tag() -> str:
    a, b = sys.version_info[:2]
    flags = getattr(sys, "abiflags", "") or ""
    if getattr(sys.implementation, "name", "cpython") == "cpython":
        return f"cp{a}{b}{flags}"
    return f"cp{a}{b}"


def platform_tag() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    table = {
        "linux": f"linux_{machine}",
        "darwin": f"macosx_10_9_{machine}",
        "windows": f"win_{machine}",
    }
    return table.get(sysname, f"{sysname}_{machine}")


def current_sys_tag() -> tuple[str, str, str]:
    try:
        from packaging.tags import sys_tags

        tag = next(sys_tags())
        return tag.interpreter, tag.abi, tag.platform
    except Exception:
        a, b = sys.version_info[:2]
        interp = f"cp{a}{b}"
        return interp, interp, platform_tag()


BINARY_SUFFIXES: set[str] = {".so", ".pyd", ".dll", ".dylib", ".sl"}


def _has_binary(root: Path) -> bool:
    return any(p.is_file() and p.suffix.lower() in BINARY_SUFFIXES for p in root.rglob("*"))


def _read_metadata(dist_info: Path) -> dict[str, str]:
    for name in ("METADATA", "PKG-INFO"):
        p = dist_info / name
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
            msg = email.parser.Parser().parsestr(txt)
            return dict(msg.items())
        except Exception:
            log.opt(exception=True).debug(
                "Could not parse {}",
                p,
            )
    return {}


def _parse_dist_info_stem(dist_info: Path) -> tuple[str, str]:
    stem = dist_info.name
    for suffix in (".dist-info", ".egg-info"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "-" in stem:
        name, version = stem.split("-", 1)
        return name, version
    return stem, "0.0.0"


def _read_record(dist_info: Path) -> Optional[list[tuple[str, str, str]]]:
    rec = dist_info / "RECORD"
    if not rec.exists():
        return None
    rows: list[tuple[str, str, str]] = []
    with rec.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0]:
                continue
            rows.append((
                row[0],
                row[1] if len(row) > 1 else "",
                row[2] if len(row) > 2 else "",
            ))
    return rows


def _console_script_names(dist_info: Path) -> list[str]:
    ep = dist_info / "entry_points.txt"
    if not ep.exists():
        return []
    names: list[str] = []
    in_section = False
    for raw in ep.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line[1:-1].strip() == "console_scripts"
            continue
        if in_section and line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].strip())
    return names


@dataclass(slots=True)
class PackageInfo:
    name: str
    version: str
    dist_info: Path
    site_packages: Path
    is_pure: bool = True
    has_binary: bool = False
    top_level: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def dist_name_underscored(self) -> str:
        return self.name.lower().replace("-", "_").replace(".", "_")

    def wheel_filename(self, tag: str) -> str:
        return f"{self.dist_name_underscored}-{self.version}-{tag}.whl"


def resolve_source(source: str, explicit: Optional[Path]) -> Path:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.exists():
            msg = f"site-packages directory does not exist: {p}"
            raise SystemExit(msg)
        return p
    if source == "current":
        return Path.cwd()
    if source == "user":
        return Path(site.getusersitepackages()).resolve()
    if source == "system":
        try:
            return Path(site.getsitepackages()[0]).resolve()
        except Exception:
            return Path(sysconfig.get_paths()["purelib"]).resolve()
    if source == "auto":
        candidates = [
            Path(sysconfig.get_paths()["purelib"]).resolve(),
            Path(site.getusersitepackages()).resolve(),
            Path.cwd(),
        ]
        for c in candidates:
            if c.exists() and list(c.glob("*.dist-info")):
                return c
        return Path.cwd()
    msg = f"unknown source: {source!r}"
    raise SystemExit(msg)


def discover_packages(
    site_packages: Path,
    names: Optional[Sequence[str]] = None,
) -> list[PackageInfo]:
    wanted = {n.lower().replace("-", "_") for n in names} if names else None
    out: list[PackageInfo] = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        dist_name, version = _parse_dist_info_stem(dist_info)
        key = dist_name.lower().replace("-", "_")
        if wanted is not None and key not in wanted:
            continue
        meta = _read_metadata(dist_info)
        name = meta.get("Name", dist_name)
        version = meta.get("Version", version)
        top_level = _guess_top_level(site_packages, dist_info, name)
        is_pure = not _has_binary(dist_info.parent) or not _package_has_binary(site_packages, top_level)
        has_bin = not is_pure
        out.append(
            PackageInfo(
                name=name,
                version=version,
                dist_info=dist_info,
                site_packages=site_packages,
                is_pure=is_pure,
                has_binary=has_bin,
                top_level=top_level,
                metadata=meta,
            )
        )
    if wanted is not None:
        found = {p.name.lower().replace("-", "_") for p in out}
        for missing in sorted(wanted - found):
            log.warning("Package not found: {}", missing)
    return out


def _package_has_binary(site_packages: Path, top_level: str) -> bool:
    for cand in (
        site_packages / top_level,
        site_packages / f"{top_level}.py",
    ):
        if cand.is_dir():
            return _has_binary(cand)
    return False


def _guess_top_level(site_packages: Path, dist_info: Path, name: str) -> str:
    tl = dist_info / "top_level.txt"
    if tl.exists():
        first = tl.read_text(encoding="utf-8", errors="ignore").splitlines()
        if first:
            return first[0].strip()
    underscored = name.lower().replace("-", "_").replace(".", "_")
    for cand in (
        site_packages / underscored,
        site_packages / f"{underscored}.py",
    ):
        if cand.exists():
            return underscored
    return _parse_dist_info_stem(dist_info)[0].replace("-", "_").lower()


def _write_wheel_metadata(
    dist_info_dir: Path,
    pkg: PackageInfo,
    tag: str,
) -> None:
    wheel_txt = (
        "Wheel-Version: 1.0\n"
        "Generator: repack_tool\n"
        f"Root-Is-Purelib: {'true' if pkg.is_pure else 'false'}\n"
        f"Tag: {tag}\n"
    )
    (dist_info_dir / "WHEEL").write_text(wheel_txt, encoding="utf-8")
    if not (dist_info_dir / "METADATA").exists():
        md = f"Metadata-Version: 2.1\nName: {pkg.name}\nVersion: {pkg.version}\nSummary: Repacked wheel (repack_tool)\n"
        (dist_info_dir / "METADATA").write_text(md, encoding="utf-8")


def _compute_record(base: Path, dist_info_name: str) -> str:
    rows: list[str] = []
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(base).as_posix()
        if rel == f"{dist_info_name}/RECORD":
            continue
        rows.append(f"{rel},{_hash_file(f)},{f.stat().st_size}")
    rows.append(f"{dist_info_name}/RECORD,,")
    return "\n".join(rows) + "\n"


def _zip_directory(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())


def build_wheel_simple(pkg: PackageInfo, output_dir: Path, verbose: bool) -> tuple[bool, str, Optional[Path]]:
    tag = "py3-none-any" if pkg.is_pure else "-".join(current_sys_tag())
    wheel_path = output_dir / pkg.wheel_filename(tag)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            sp = pkg.site_packages
            src_dir = sp / pkg.top_level
            src_file = sp / f"{pkg.top_level}.py"
            if src_dir.is_dir():
                shutil.copytree(src_dir, tmp_p / pkg.top_level)
            elif src_file.exists():
                shutil.copy2(src_file, tmp_p / src_file.name)
            else:
                fallback_dir = sp / pkg.name.lower().replace("-", "_")
                if fallback_dir.is_dir():
                    shutil.copytree(fallback_dir, tmp_p / fallback_dir.name)
                else:
                    return False, f"source files for {pkg.name} not found", None
            dst_di = tmp_p / pkg.dist_info.name
            shutil.copytree(pkg.dist_info, dst_di)
            _write_wheel_metadata(dst_di, pkg, tag)
            if not (dst_di / "top_level.txt").exists():
                (dst_di / "top_level.txt").write_text(pkg.top_level + "\n", encoding="utf-8")
            (dst_di / "RECORD").write_text(_compute_record(tmp_p, pkg.dist_info.name), encoding="utf-8")
            _zip_directory(tmp_p, wheel_path)
        if verbose:
            log.info(
                "  simple: wrote {} ({})",
                wheel_path.name,
                _human_size(wheel_path.stat().st_size),
            )
        return True, f"created {wheel_path.name}", wheel_path
    except Exception as e:
        log.opt(exception=True).debug(
            "build_wheel_simple failed",
        )
        return False, f"error: {e}", None


def build_wheel_from_record(
    pkg: PackageInfo,
    output_dir: Path,
    on_missing: str,
    missing_dir: Optional[Path],
    verbose: bool,
) -> tuple[bool, str, Optional[Path]]:
    rows = _read_record(pkg.dist_info)
    if rows is None:
        return False, f"no RECORD file in {pkg.dist_info.name}", None
    sp = pkg.site_packages
    missing: list[str] = []
    resolved: list[tuple[Path, str]] = []
    for rel, _h, _sz in rows:
        if not rel or rel.endswith("RECORD") or rel.startswith(("../", "/")):
            continue
        if rel.endswith(".pyc"):
            continue
        src = sp / rel
        if src.exists():
            resolved.append((src, rel))
        else:
            missing.append(rel)
    if missing:
        msg = f"{len(missing)} file(s) missing from RECORD"
        if on_missing == "abort":
            return False, f"aborting: {msg} -> {missing[:5]}", None
        if on_missing == "copy" and missing_dir is not None:
            missing_dir.mkdir(parents=True, exist_ok=True)
            dest = missing_dir / pkg.dist_info.name
            with contextlib.suppress(Exception):
                shutil.copytree(pkg.site_packages / pkg.top_level, dest)
            return False, f"copied to {missing_dir}: {msg}", None
        if on_missing == "warn" and verbose:
            log.warning("  {}: {}", pkg.name, msg)
    has_bin = any(s.suffix.lower() in BINARY_SUFFIXES for s, _ in resolved)
    if has_bin:
        tag = "-".join(current_sys_tag())
        pkg.is_pure = False
    else:
        tag = "py3-none-any"
        pkg.is_pure = True
    wheel_path = output_dir / pkg.wheel_filename(tag)
    dist_info_name = pkg.dist_info.name
    data_dir_name = f"{pkg.dist_name_underscored}-{pkg.version}.data"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            dst_di = tmp_p / dist_info_name
            dst_di.mkdir(parents=True, exist_ok=True)
            for src, rel in resolved:
                target = tmp_p / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, target)
            scripts = _console_script_names(pkg.dist_info)
            if scripts:
                scripts_dst = tmp_p / data_dir_name / "scripts"
                scripts_dst.mkdir(parents=True, exist_ok=True)
                bin_dir = _venv_bin_dir(pkg.site_packages)
                if bin_dir:
                    for sname in scripts:
                        for ext in ("", ".exe", ".bat", ".cmd", ".py"):
                            cand = bin_dir / f"{sname}{ext}"
                            if cand.exists():
                                shutil.copy2(cand, scripts_dst / cand.name)
                                break
            _write_wheel_metadata(dst_di, pkg, tag)
            (dst_di / "RECORD").write_text(_compute_record(tmp_p, dist_info_name), encoding="utf-8")
            _zip_directory(tmp_p, wheel_path)
        if verbose:
            log.info(
                "  record: wrote {} ({}) [{} files]",
                wheel_path.name,
                _human_size(wheel_path.stat().st_size),
                len(resolved),
            )
        return True, f"created {wheel_path.name}", wheel_path
    except Exception as e:
        log.opt(exception=True).debug(
            "build_wheel_from_record failed",
        )
        return False, f"error: {e}", None


def _venv_bin_dir(site_packages: Path) -> Optional[Path]:
    cur = site_packages.resolve()
    for _ in range(5):
        for name in ("bin", "Scripts"):
            d = cur / name
            if d.is_dir():
                return d
        cur = cur.parent
    return None


def build_wheel_via_subprocess(src_unpacked: Path, output_dir: Path, verbose: bool) -> tuple[bool, str, Optional[Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "wheel",
        "pack",
        str(src_unpacked),
        "-d",
        str(output_dir),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False, "wheel module not available", None
    if res.returncode != 0:
        return False, f"wheel pack failed: {res.stderr.strip()[:200]}", None
    wheels = sorted(output_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if verbose and wheels:
        log.info("  wheelpack: {}", wheels[0].name)
    return True, "ok", wheels[0] if wheels else None


@dataclass(slots=True)
class RepackStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    pure: int = 0
    with_c_ext: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class Repacker:
    def __init__(
        self,
        site_packages: Path,
        output_dir: Path,
        method: str = "simple",
        on_missing: str = "warn",
        missing_dir: Optional[Path] = None,
        builder: str = "zip",
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        self.site_packages = site_packages
        self.output_dir = output_dir
        self.method = method
        self.on_missing = on_missing
        self.missing_dir = missing_dir
        self.builder = builder
        self.dry_run = dry_run
        self.verbose = verbose
        self.stats = RepackStats()
        self.results: list[dict[str, Any]] = []

    def repack_one(self, pkg: PackageInfo) -> tuple[bool, str, Optional[Path]]:
        if self.dry_run:
            tag = "py3-none-any" if pkg.is_pure else "-".join(current_sys_tag())
            log.info("[dry-run] would build {}", pkg.wheel_filename(tag))
            return True, "dry-run", None
        if self.method == "simple":
            ok, msg, path = build_wheel_simple(pkg, self.output_dir, self.verbose)
        elif self.method == "record":
            ok, msg, path = build_wheel_from_record(
                pkg,
                self.output_dir,
                on_missing=self.on_missing,
                missing_dir=self.missing_dir,
                verbose=self.verbose,
            )
        else:
            return False, f"unknown method {self.method!r}", None
        if ok and self.builder == "wheelpack" and path is not None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_p = Path(tmp)
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(tmp_p)
                ok2, msg2, _ = build_wheel_via_subprocess(tmp_p, self.output_dir, self.verbose)
                if not ok2:
                    return False, f"wheelpack: {msg2}", path
        return ok, msg, path

    def run(self, packages: Sequence[PackageInfo]) -> RepackStats:
        if not packages:
            log.warning("No packages found to repack")
            return self.stats
        self.output_dir.mkdir(parents=True, exist_ok=True)
        iterator = _tqdm(packages, desc="Repacking", disable=not _HAS_TQDM)
        for i, pkg in enumerate(iterator, 1):
            self.stats.total += 1
            log.info("[{}/{}] {} {}", i, len(packages), pkg.name, pkg.version)
            ok, msg, path = self.repack_one(pkg)
            if ok:
                self.stats.success += 1
                if pkg.is_pure:
                    self.stats.pure += 1
                else:
                    self.stats.with_c_ext += 1
                log.info("  ✓ {}", msg)
            else:
                self.stats.failed += 1
                log.error("  ✗ {}", msg)
            self.results.append({
                "package": pkg.name,
                "version": pkg.version,
                "is_pure_python": pkg.is_pure,
                "success": ok,
                "wheel": path.name if path else None,
                "message": msg,
            })
        return self.stats

    def save_report(self, filename: str) -> Path:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "site_packages": str(self.site_packages),
            "output_directory": str(self.output_dir),
            "method": self.method,
            "statistics": self.stats.to_dict(),
            "results": self.results,
        }
        out = self.output_dir / filename
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out


def _worker_repack(args_tuple: tuple[dict[str, Any], PackageInfo]) -> dict[str, Any]:
    cfg, pkg = args_tuple
    if cfg["method"] == "simple":
        ok, msg, path = build_wheel_simple(pkg, Path(cfg["output_dir"]), cfg["verbose"])
    else:
        ok, msg, path = build_wheel_from_record(
            pkg,
            Path(cfg["output_dir"]),
            on_missing=cfg["on_missing"],
            missing_dir=Path(cfg["missing_dir"]) if cfg["missing_dir"] else None,
            verbose=cfg["verbose"],
        )
    return {
        "package": pkg.name,
        "version": pkg.version,
        "is_pure_python": pkg.is_pure,
        "success": ok,
        "wheel": path.name if path else None,
        "message": msg,
    }


def _find_dist_info_dir(root: Path) -> Optional[Path]:
    if root.name.endswith(".dist-info"):
        return root
    cands = [d for d in root.iterdir() if d.is_dir() and d.name.endswith(".dist-info")]
    if not cands:
        return None
    if len(cands) > 1:
        log.warning("Multiple dist-info dirs in {}; using {}", root, cands[0].name)
    return cands[0]


def _wheel_name_from_dist_info(dist_info: Path) -> tuple[str, str]:
    stem = dist_info.name[: -len(".dist-info")]
    if "-" in stem:
        return stem.split("-", 1)[0], stem.split("-", 1)[1]
    return stem, "0.0.0"


def _wheel_tag_from_dist_info(dist_info: Path) -> str:
    wf = dist_info / "WHEEL"
    if not wf.exists():
        return "py3-none-any"
    for line in wf.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("Tag:"):
            return line.split(":", 1)[1].strip()
    return "py3-none-any"


def pack_unpacked_dir_library(src: Path, output_dir: Path, verbose: bool) -> tuple[bool, str, Optional[Path]]:
    if not _HAS_WHEEL:
        return False, "wheel library not installed", None
    dist_info = _find_dist_info_dir(src)
    if dist_info is None:
        return False, "no *.dist-info found", None
    name, version = _wheel_name_from_dist_info(dist_info)
    tag = _wheel_tag_from_dist_info(dist_info)
    wheel_path = output_dir / f"{name}-{version}-{tag}.whl"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with WheelFile(str(wheel_path), "w", compression=zipfile.ZIP_DEFLATED) as wf:
            for f in src.rglob("*"):
                if f.is_file():
                    wf.write_to(str(f), f.relative_to(src).as_posix())
        if verbose:
            log.info("  packed {} -> {}", src.name, wheel_path.name)
        return True, wheel_path.name, wheel_path
    except Exception as e:
        if wheel_path.exists():
            wheel_path.unlink()
        return False, str(e), None


def pack_unpacked_dir_subprocess(src: Path, output_dir: Path, verbose: bool) -> tuple[bool, str, Optional[Path]]:
    ok, msg, path = build_wheel_via_subprocess(src, output_dir, verbose)
    if ok:
        return True, path.name if path else "ok", path
    return False, msg, None


async def _pack_async(src: Path, output_dir: Path, queue: "asyncio.Queue[tuple[str, bool]]", verbose: bool) -> None:
    loop = asyncio.get_running_loop()
    ok, msg, _path = await loop.run_in_executor(None, pack_unpacked_dir_library, src, output_dir, verbose)
    await queue.put((msg if ok else f"{src.name}: {msg}", ok))


def _pack_worker(args_tuple: tuple[Path, Path, str, bool]) -> tuple[bool, str]:
    src, output_dir, mode, verbose = args_tuple
    if mode == "subprocess":
        ok, msg, _ = pack_unpacked_dir_subprocess(src, output_dir, verbose)
    else:
        ok, msg, _ = pack_unpacked_dir_library(src, output_dir, verbose)
    return ok, msg


def run_pack_dirs(
    directory: Path,
    output_dir: Path,
    mode: str,
    parallel: bool,
    workers: int,
    verbose: bool,
) -> int:
    if not directory.is_dir():
        log.error("directory not found: {}", directory)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    dirs = [d for d in sorted(directory.iterdir()) if d.is_dir() and not d.name.endswith(".dist-info")]
    if not dirs:
        log.warning("no unpacked wheel directories found under {}", directory)
        return 0
    log.info("Found {} candidate directories in {}", len(dirs), directory)
    successes = 0
    failures = 0
    if mode == "async":
        if not _HAS_WHEEL:
            log.error("async mode requires the 'wheel' library")
            return 1

        async def _run_all() -> None:
            nonlocal successes, failures
            q: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
            tasks = [_pack_async(d, output_dir, q, verbose) for d in dirs]
            await asyncio.gather(*tasks)
            while not q.empty():
                msg, ok = await q.get()
                if ok:
                    successes += 1
                    log.info("✓ {}", msg)
                else:
                    failures += 1
                    log.error("✗ {}", msg)

        asyncio.run(_run_all())
    elif parallel and len(dirs) > 1:
        workers = workers or min(cpu_count(), 8)
        log.info("Using {} parallel workers", workers)
        payload = [(d, output_dir, mode, verbose) for d in dirs]
        with Pool(processes=workers) as pool:
            for ok, msg in pool.imap_unordered(_pack_worker, payload):
                if ok:
                    successes += 1
                    log.info("✓ {}", msg)
                else:
                    failures += 1
                    log.error("✗ {}", msg)
    else:
        for d in dirs:
            if mode == "subprocess":
                ok, msg, _ = pack_unpacked_dir_subprocess(d, output_dir, verbose)
            else:
                ok, msg, _ = pack_unpacked_dir_library(d, output_dir, verbose)
            if ok:
                successes += 1
                log.info("✓ {}", msg)
            else:
                failures += 1
                log.error("✗ {}", msg)
    log.info("Done: {} successful, {} failed", successes, failures)
    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repack_tool",
        description="Unified Python package -> wheel repacker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples", 1)[-1],
    )
    p.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    sub = p.add_subparsers(dest="command", required=True)
    rp = sub.add_parser(
        "repack",
        help="Repack installed packages from a site-packages dir into wheels",
    )
    rp.add_argument(
        "--source",
        choices=["auto", "current", "user", "system", "path"],
        default="current",
        help="where to look for site-packages (default: current)",
    )
    rp.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="explicit site-packages directory (overrides --source)",
    )
    rp.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "tmp" / "whl",
        help="output directory for wheels (default: ~/tmp/whl)",
    )
    rp.add_argument(
        "--packages",
        nargs="+",
        default=None,
        help="repack only these packages (by name)",
    )
    rp.add_argument(
        "--all",
        action="store_true",
        help="repack every package (explicit; default is implicit if --packages omitted)",
    )
    rp.add_argument(
        "--method",
        choices=["simple", "record"],
        default="simple",
        help="simple = copy dirs; record = rebuild from RECORD (default: simple)",
    )
    rp.add_argument(
        "--on-missing",
        choices=["skip", "warn", "abort", "copy"],
        default="warn",
        help="behaviour for missing files in RECORD mode (default: warn)",
    )
    rp.add_argument(
        "--missing-dir",
        type=Path,
        default=Path.home() / "tmp" / "not_repacked",
        help="where to stash packages that fail (--on-missing copy)",
    )
    rp.add_argument(
        "--builder",
        choices=["zip", "wheelpack", "wheellib"],
        default="zip",
        help="low-level wheel writer (default: zip)",
    )
    rp.add_argument("--parallel", action="store_true", help="multiprocessing mode")
    rp.add_argument("--dry-run", action="store_true", help="don't write any files")
    rp.add_argument(
        "--report",
        metavar="FILE",
        default=None,
        help="write a JSON report to FILE (inside --output)",
    )
    rp.add_argument(
        "--list-wheels",
        action="store_true",
        help="list every .whl in the output directory at the end",
    )
    rp.add_argument("--no-progress", action="store_true", help="disable tqdm progress")
    pd = sub.add_parser(
        "pack-dirs",
        help="Pack pre-unpacked wheel directories into .whl files",
    )
    pd.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="directory containing unpacked wheel trees (default: cwd)",
    )
    pd.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output dir (default: same as --directory)",
    )
    pd.add_argument(
        "--mode",
        choices=["library", "subprocess", "async"],
        default="library",
        help="packing strategy (default: library)",
    )
    pd.add_argument("--parallel", action="store_true", help="multiprocessing mode")
    return p


def cmd_repack(args: argparse.Namespace) -> int:
    explicit = args.site_packages
    if explicit is not None:
        site_packages = explicit.expanduser().resolve()
    else:
        site_packages = resolve_source(args.source, None)
    log.info("Source:  {}", site_packages)
    log.info("Output:  {}", args.output)
    log.info("Method:  {}", args.method)
    if args.parallel:
        log.info("Mode:    parallel")
    packages = discover_packages(site_packages, args.packages if not args.all else None)
    if not packages:
        log.warning("Nothing to do.")
        return 0
    if args.parallel and len(packages) > 1:
        return _run_parallel(args, packages)
    rep = Repacker(
        site_packages=site_packages,
        output_dir=args.output,
        method=args.method,
        on_missing=args.on_missing,
        missing_dir=args.missing_dir,
        builder=args.builder,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    stats = rep.run(packages)
    log.info("=" * 40)
    log.info(
        "Total={} success={} failed={} pure={} c-ext={}",
        stats.total,
        stats.success,
        stats.failed,
        stats.pure,
        stats.with_c_ext,
    )
    if args.report:
        out = rep.save_report(args.report)
        log.info("Report saved: {}", out)
    if args.list_wheels:
        _list_wheels(args.output)
    return 0 if stats.failed == 0 else 1


def _run_parallel(args: argparse.Namespace, packages: Sequence[PackageInfo]) -> int:
    workers = MAX_WORKERS or min(cpu_count(), 8)
    log.info("Running with {} workers", workers)
    cfg = {
        "method": args.method,
        "output_dir": str(args.output),
        "on_missing": args.on_missing,
        "missing_dir": str(args.missing_dir) if args.missing_dir else None,
        "verbose": args.verbose,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    payload = [(cfg, pkg) for pkg in packages]
    success = failed = pure = cext = 0
    results: list[dict[str, Any]] = []
    with Pool(processes=workers) as pool:
        for row in pool.imap_unordered(_worker_repack, payload):
            results.append(row)
            if row["success"]:
                success += 1
                if row["is_pure_python"]:
                    pure += 1
                else:
                    cext += 1
            else:
                failed += 1
                log.error("✗ {}: {}", row["package"], row["message"])
    log.info("=" * 40)
    log.info(
        "Total={} success={} failed={} pure={} c-ext={}",
        len(packages),
        success,
        failed,
        pure,
        cext,
    )
    if args.report:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "statistics": {
                "total": len(packages),
                "success": success,
                "failed": failed,
                "pure": pure,
                "with_c_ext": cext,
            },
            "results": results,
        }
        out = args.output / args.report
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("Report saved: {}", out)
    if args.list_wheels:
        _list_wheels(args.output)
    return 0 if failed == 0 else 1


def _list_wheels(output_dir: Path) -> None:
    wheels = sorted(output_dir.glob("*.whl"))
    if not wheels:
        log.info("No .whl files in {}", output_dir)
        return
    total = 0
    log.info("Generated wheels in {}:", output_dir)
    for w in wheels:
        size = w.stat().st_size
        total += size
        log.info("  {} {}", w.name, _human_size(size))
    log.info("Total: {}", _human_size(total))


def cmd_pack_dirs(args: argparse.Namespace) -> int:
    output = args.output or args.directory
    return run_pack_dirs(
        directory=args.directory.resolve(),
        output_dir=output.resolve(),
        mode=args.mode,
        parallel=args.parallel,
        workers=MAX_WORKERS or 0,
        verbose=args.verbose,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _HAS_TQDM:
        log.debug("tqdm not available; progress bars disabled")
    try:
        if args.command == "repack":
            return cmd_repack(args)
        if args.command == "pack-dirs":
            return cmd_pack_dirs(args)
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    raise SystemExit(main())
