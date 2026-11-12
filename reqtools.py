#!/data/data/com.termux/files/usr/bin/env python
"""Unified command-line tool for cleaning requirements.txt files and inspecting installed packages."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
import importlib.metadata
from pathlib import Path
import re
import shutil
import site
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Sequence

from loguru import logger

try:
    STDLIB = set(sys.stdlib_module_names)
except AttributeError:
    STDLIB = {"os", "sys", "re", "pathlib", "shutil", "tempfile", "zipfile", "tarfile", "csv", "json", "math", "io"}


def get_installed_pkgs():
    try:
        return [dist.metadata["Name"] for dist in importlib.metadata.distributions()]
    except Exception:
        return []


def cmd_clean_req(args: list[Any]) -> str | None:
    req_path = Path(args.file)
    if not req_path.exists():
        print(f"Error: File '{req_path}' not found.", file=sys.stderr)
        sys.exit(1)
    op_re = re.compile(r"\s*(?:===|==|!=|>=|<=|~=|>|<)\s*")

    def clean_line(m_str: str) -> str:
        m_str = m_str.split("#", 1)[0].strip()
        if not m_str:
            return ""
        m_str = m_str.split(";", 1)[0].strip()
        if not m_str:
            return ""
        m_str = re.sub(r"\[.*?\]", "", m_str).strip()
        if not m_str:
            return ""
        parts = op_re.split(m_str, maxsplit=1)
        return parts[0].strip()

    def sort_key(o: Sequence[Any]):
        g = o[0]
        if g.isupper():
            return (0, o)
        if g.islower():
            return (1, o)
        return (2, o)

    with req_path.open(encoding="utf-8") as f:
        lines = f.readlines()
    cleaned = []
    seen = set()
    for line in lines:
        c = clean_line(line)
        if c and c not in seen:
            cleaned.append(c)
            seen.add(c)
    cleaned = sorted(cleaned, key=sort_key)
    req_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    print("\n===Cleaned Requirements===")
    for l in cleaned:
        print(l)


def cmd_clean_freeze(args: list[Any]):
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"Error: File '{file_path}' not found.", file=sys.stderr)
        sys.exit(1)
    freeze_re = re.compile(r"^\s*(?:-e\s+)?(?P<name>[A-Za-z0-9_.\-]+)", re.VERBOSE)

    def parse_line(k: str):
        k = k.strip()
        if not k or k.startswith("#"):
            return None
        if k.startswith(("git+", "http://", "https://")):
            return None
        if "@" in k:
            m = k.split("@", 1)[0].strip()
            return m or None
        h = freeze_re.match(k)
        if h:
            return h.group("name")
        return None

    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    names = []
    for k in lines:
        m = parse_line(k)
        if m:
            names.append(m)
    seen = set()
    unique_names = [p for p in names if not (p in seen or seen.add(p))]
    file_path.write_text("\n".join(unique_names) + "\n", encoding="utf-8")
    print(f"Cleaned freeze output in {file_path}")


def cmd_strip_versions(args: list[Any]) -> None:
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File '{file_path}' not found.", file=sys.stderr)
        sys.exit(1)
    content = file_path.read_text(encoding="utf-8").splitlines()
    pkgs = set()
    for line in content:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        g = (
            line
            .split("==")[0]
            .split(">=")[0]
            .split("<=")[0]
            .split("~=")[0]
            .split("!=")[0]
            .split("===")[0]
            .split(">")[0]
            .split("<")[0]
            .split("=")[0]
            .split("~")[0]
            .split(" @ ")[0]
            .split(" ")[0]
        )
        g = g.strip()
        if g:
            pkgs.add(g)
    sorted_pkgs = sorted(pkgs)
    file_path.write_text("\n".join(sorted_pkgs) + "\n", encoding="utf-8")
    print(f"Updated {file_path} with {len(sorted_pkgs)} unique packages.")


def cmd_keep_pure(args: list[Any]) -> Any | None:
    req_file = args.file
    binary_output = Path(args.binary_output)

    def get_site_packages():
        dirs = []
        for q in [site.getsitepackages(), [site.getusersitepackages()] if hasattr(site, "getusersitepackages") else []]:
            if isinstance(q, str):
                dirs.append(q)
            else:
                dirs.extend(q)
        return [d for d in dirs if d and Path(d).exists()]

    def get_dist_infos():
        h = []
        for q in get_site_packages():
            q_path = Path(q)
            if q_path.exists():
                h.extend([str(z) for z in q_path.iterdir() if z.name.endswith(".dist-info")])
        return h

    def check_binary(i: Path) -> str | None:
        i_path = Path(i)
        record_path = i_path / "RECORD"
        pkg_name = i_path.name.replace(".dist-info", "").split("-")[0].lower()
        if record_path.exists():
            try:
                with record_path.open(encoding="utf-8") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if row and any(row[0].endswith(ext) for ext in [".so", ".pyd"]):
                            return pkg_name
            except Exception:
                pass
        return None

    def find_binaries():
        h = get_dist_infos()
        with ProcessPoolExecutor() as executor:
            results = executor.map(check_binary, h)
        return {f for f in results if f}

    req_path = Path(req_file)
    if not req_path.exists():
        print(f"Error: {req_file} not found")
        return
    binaries = find_binaries()
    binary_output.parent.mkdir(parents=True, exist_ok=True)
    binary_output.write_text("\n".join(sorted(binaries)) + "\n", encoding="utf-8")
    print("binary_pkgs created.")
    with req_path.open(encoding="utf-8") as f:
        lines = [A.rstrip() for A in f]
    comments = [A for A in lines if A.startswith("#")]
    reqs = [A for A in lines if A and not A.startswith("#")]
    kept = []
    removed = []
    for G in reqs:
        o = G.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip().lower()
        if o in binaries:
            removed.append(G)
        else:
            kept.append(G)
    with req_path.open("w", encoding="utf-8") as f:
        f.writelines((f"{r}\n" for r in comments))
        f.writelines((f"{F}\n" for F in sorted(kept)))
    if removed:
        print(f"\n🗑️ Removed binary packages ({len(removed)}):")
        for F in sorted(removed):
            print(f"-{F}")
    else:
        print("✅ No binary packages found in requirements.txt")


def cmd_extract_reqs(args: list[Any]) -> bool | None:
    output_path = Path(args.output)
    meta_names = {"METADATA", "PKGINFO", "PKG-INFO"}
    req_prefix = "Requires-Dist:"
    extracted = []

    def parse_metadata_content(content: str) -> tuple[str,]:
        lines = content.splitlines()
        normal, reqs = [], []
        for line in lines:
            if line.startswith(req_prefix):
                reqs.append(line)
            else:
                normal.append(line)
        body = "\n".join(normal)
        if content.endswith("\n"):
            body += "\n"
        return body, reqs

    def process_file_meta(af: Path) -> None:
        try:
            text = af.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        normal, reqs = parse_metadata_content(text)
        if reqs:
            extracted.extend(reqs)
            af.write_text(normal, encoding="utf-8")

    def is_valid_archive(af: Path) -> bool:
        af = Path(af)
        try:
            if not af.is_file() or not af.stat().st_size:
                return False
            low = af.name.lower()
            if low.endswith((".zip", ".whl")):
                with zipfile.ZipFile(af, "r") as zf:
                    return zf.testzip() is None
            if low.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tbz")):
                with tarfile.open(af) as tf:
                    return tf.badfile() is None
            return False
        except Exception:
            return False

    def process_zip_archive(af: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(af, "r") as zf_in, zipfile.ZipFile(tmp_path, "w") as zf_out:
                for item in zf_in.infolist():
                    data = zf_in.read(item.filename)
                    fname = Path(item.filename).name
                    if fname in meta_names:
                        try:
                            content = data.decode("utf-8", errors="ignore")
                            normal, reqs = parse_metadata_content(content)
                            if reqs:
                                extracted.extend(reqs)
                            data = normal.encode("utf-8")
                        except Exception:
                            pass
                    zf_out.writestr(item, data)
            shutil.move(str(tmp_path), str(af))
        finally:
            tmp_path.unlink(missing_ok=True)

    def process_tar_archive(af: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            tar_temp = temp_path / "temp.tar.gz"
            with tarfile.open(af, "r:*") as tf:
                tf.extractall(temp_path, filter="data")
            for m in meta_names:
                for meta_file in temp_path.rglob(m):
                    if meta_file.is_file():
                        process_file_meta(meta_file)
            with tarfile.open(tar_temp, "w:gz") as tf:
                tf.add(temp_path, arcname="")
            shutil.move(str(tar_temp), str(af))

    cwd = Path.cwd()
    targets = []
    for path in cwd.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        low = name.lower()
        if name in meta_names or low.endswith((".metadata", ".zip", ".whl", ".tar.gz", ".tgz", ".tar")):
            targets.append(path)
    for path in targets:
        name = path.name
        low = name.lower()
        if name in meta_names or low.endswith(".metadata"):
            process_file_meta(path)
        elif low.endswith((".zip", ".whl")):
            if is_valid_archive(path):
                print(f"Processing ... {path}")
                process_zip_archive(path)
        elif low.endswith((".tar.gz", ".tgz", ".tar")) and is_valid_archive(path):
            process_tar_archive(path)
    if extracted:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("a", encoding="utf-8") as f:
                f.writelines((f"{ac}\n" for ac in extracted))
            print(f"---Saved {len(extracted)} lines to {output_path}---")
        except PermissionError:
            pass
        print("\nRemoved Lines:")
        print("-" * 40)
        for ac in extracted:
            print(ac)
        print("-" * 40)
    else:
        print("No matching lines were found or removed.")


def cmd_remove_req(args: list[Any]) -> None:
    req_to_remove = args.requirement
    site_pkgs = site.getsitepackages()
    target_dir = None
    for p in site_pkgs:
        p_path = Path(p)
        if p_path.exists():
            target_dir = p_path
            break
    if not target_dir:
        g, h = sys.version_info[:2]
        termux_path = Path(f"/data/data/com.termux/files/usr/lib/python{g}{h}/site-packages")
        target_dir = termux_path if termux_path.exists() else Path(site_pkgs[0]) if site_pkgs else Path.cwd()

    def process_metadata(meta_path: Path, req_str: str) -> None:
        meta_path = Path(meta_path)
        text = meta_path.read_text(encoding="utf-8", errors="ignore")
        target_line = "Requires-Dist:" + req_str
        if target_line in text:
            lines = text.splitlines()
            new_lines = [j for j in lines if target_line not in j]
            meta_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print(f"{meta_path.parent.name} updated.")

    if target_dir.exists():
        for metadata_file in target_dir.rglob("METADATA"):
            process_metadata(metadata_file, req_to_remove)


def cmd_strip_installed(args: list[Any]) -> list[str] | None:
    req_file = args.file
    req_path = Path(req_file)
    if not req_path.exists():
        print(f"Error: {req_file} not found", file=sys.stderr)
        return

    def get_reqs() -> list[str]:
        with req_path.open(encoding="utf-8") as f:
            return [l.strip().replace("-", "_").lower() for l in f if l.strip() and not l.startswith("#")]

    installed = get_installed_pkgs()
    installed = [p.lower().replace("-", "_") for p in installed if p]
    requested = get_reqs()
    filtered = [l for l in requested if l not in installed]
    filtered = [l for l in filtered if l not in STDLIB]
    filtered = sorted(set(filtered))
    req_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    removed_count = len(requested) - len(filtered)
    print(f"Removed {removed_count} packages")


def cmd_strip_stdlib(args: list[Any]) -> list[str] | None:
    req_file = args.file
    req_path = Path(req_file)
    if not req_path.exists():
        print(f"Error: {req_file} not found", file=sys.stderr)
        return

    def get_reqs() -> list[str]:
        with req_path.open(encoding="utf-8") as f:
            return [k.strip().replace("-", "_").lower() for k in f if k.strip() and not k.startswith("#")]

    requested = get_reqs()
    filtered = [k for k in requested if k not in STDLIB]
    req_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    removed_count = len(requested) - len(filtered)
    print(f"Removed {removed_count} packages")


def cmd_trim_imports(args: list[Any]) -> bool | None:
    log_path = Path(args.log_file)
    try:
        from loguru import logger

        logger.add(str(log_path), diagnose=True)
        has_loguru = True
    except ImportError:
        has_loguru = False

    def test_import(mod_name: str) -> bool:
        try:
            __import__(mod_name)
            print(f"✓ {mod_name}")
            return True
        except Exception as e:
            if has_loguru:
                logger.debug(f"X {mod_name}")
            else:
                logger.debug(f"X {mod_name}: {e}")
            return False

    packages = args.packages
    if packages:
        for pkg in packages:
            test_import(pkg)
    else:
        try:
            dists = importlib.metadata.distributions()
            for dist in dists:
                name = dist.metadata["name"]
                try:
                    __import__(name)
                    print(f"✓ {name}")
                except Exception:
                    if has_loguru:
                        logger.debug(f"X {name}")
                    else:
                        logger.debug(f"X {name}")
        except Exception as e:
            print(f"Error iterating distributions: {e}", file=sys.stderr)


def cmd_uv_req(args: list[Any]) -> None:
    lock_path = Path(args.lock_file)
    req_path = Path(args.req_file)
    if not lock_path.exists():
        print(f"Error: {lock_path} not found", file=sys.stderr)
        sys.exit(1)
    content = lock_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    for line in lines:
        if 'name="' in line:
            pkg_name = line.split('name="')[1].split('"')[0]
            print(pkg_name)
            with req_path.open("a", encoding="utf-8") as f:
                f.write(pkg_name + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Python Package and Requirements Management Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_clean_req = subparsers.add_parser("clean-req", help="Clean requirements file (comments, markers, versions, sort)")
    p_clean_req.add_argument("file", nargs="?", default="requirements.txt", help="Path to requirements.txt")
    p_clean_freeze = subparsers.add_parser("clean-freeze", help="Clean pip freeze output (keep package names)")
    p_clean_freeze.add_argument("file", help="Path to pip freeze file")
    p_strip_versions = subparsers.add_parser("strip-versions", help="Strip version specifiers from requirements file")
    p_strip_versions.add_argument("file", help="Path to requirements file")
    p_keep_pure = subparsers.add_parser("keep-pure", help="Identify binary packages and remove from requirements")
    p_keep_pure.add_argument("file", nargs="?", default="requirements.txt", help="Path to requirements.txt")
    p_keep_pure.add_argument(
        "--binary-output", default="/sdcard/data/binary", help="Path to output binary package list"
    )
    p_extract_reqs = subparsers.add_parser("extract-reqs", help="Extract Requires-Dist from metadata in archives")
    p_extract_reqs.add_argument("--output", default="/sdcard/reqz.txt", help="Path to output extracted requirements")
    p_remove_req = subparsers.add_parser("remove-req", help="Remove specific Requires-Dist from site-packages METADATA")
    p_remove_req.add_argument("requirement", help="Requirement specifier to remove")
    p_strip_installed = subparsers.add_parser(
        "strip-installed", help="Remove installed and stdlib packages from requirements"
    )
    p_strip_installed.add_argument("file", nargs="?", default="requirements.txt", help="Path to requirements.txt")
    p_strip_stdlib = subparsers.add_parser("strip-stdlib", help="Remove stdlib packages from requirements")
    p_strip_stdlib.add_argument("file", help="Path to requirements file")
    p_trim_imports = subparsers.add_parser("trim-imports", help="Test importing packages and log failures")
    p_trim_imports.add_argument("packages", nargs="*", help="Optional list of package names to test")
    p_trim_imports.add_argument("--log-file", default="/sdcard/allimport.log", help="Path to log file")
    p_uv_req = subparsers.add_parser("uv-req", help="Parse uv.lock and append package names to requirements")
    p_uv_req.add_argument("--lock-file", default="uv.lock", help="Path to uv.lock")
    p_uv_req.add_argument("--req-file", default="requirements.txt", help="Path to requirements.txt")
    args = parser.parse_args()
    if args.command == "clean-req":
        cmd_clean_req(args)
    elif args.command == "clean-freeze":
        cmd_clean_freeze(args)
    elif args.command == "strip-versions":
        cmd_strip_versions(args)
    elif args.command == "keep-pure":
        cmd_keep_pure(args)
    elif args.command == "extract-reqs":
        cmd_extract_reqs(args)
    elif args.command == "remove-req":
        cmd_remove_req(args)
    elif args.command == "strip-installed":
        cmd_strip_installed(args)
    elif args.command == "strip-stdlib":
        cmd_strip_stdlib(args)
    elif args.command == "trim-imports":
        cmd_trim_imports(args)
    elif args.command == "uv-req":
        cmd_uv_req(args)


if __name__ == "__main__":
    sys.exit(main())
