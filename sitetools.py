#!/data/data/com.termux/files/usr/bin/env python
"""Unified tool for cleaning and repairing installed-package metadata (RECORD, dist-info, urecord)."""

import argparse
import base64
import contextlib
import csv
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
from typing import Any, Iterable, Iterator

from dh import MAX_WORKERS
from loguru import logger


def cprint(text: str, color: str = "reset") -> None:
    colors = {"cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m", "reset": "\033[0m"}
    print(f"{colors.get(color, '')}{text}{colors['reset']}")


def fsz(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def gsz(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def get_files(path: Path, ext = None) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    for p in path.rglob("*"):
        if p.is_file() and (ext is None or p.suffix in ext):
            yield p


def cmd_clean_meta(args: list[Any]) -> None:
    img_regex = re.compile(r"^\s*(\.\.\s+image::|:target:|:alt:)", re.IGNORECASE)
    paths = [Path(p) for p in args.paths] if args.paths else []
    target_dir = Path.cwd()
    m_files = paths or list(get_files(target_dir, ext=[".metadata", ".md"]))
    h_files = list(target_dir.rglob("METADATA"))
    if h_files and not paths:
        m_files.extend(h_files)
    initial_sz = gsz(target_dir)
    for s in m_files:
        if not s.exists():
            continue
        print(f"Processing {s.name}")
        try:
            k = s.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ Skipping {s}:{e}")
            continue
        n = k.splitlines(keepends=True)
        a = 0
        v = []
        for q in n:
            j = q.rstrip("\n").rstrip("\r")
            if j.lower().startswith("classifier"):
                v.append("\n")
                a += 1
                continue
            if j.startswith("[![") or j.lower().startswith("project-url"):
                v.append("\n")
                a += 1
                continue
            if j.startswith((
                "Metadata-Version",
                "Home-page",
                "Author",
                "Maintainer",
                "License",
                "Platform",
                "Requires-Python",
                "Description-Content-Type",
                "Provides-Extra",
            )):
                v.append("\n")
                a += 1
                continue
            if img_regex.match(j):
                v.append("\n")
                a += 1
                continue
            v.append(q)
        if not a:
            print(f"❌ {s.name}: (no change)")
            continue
        c = "".join(v)
        s.write_text(c, encoding="utf-8")
        print(f"✅ {s.name}", end="")
        cprint(f" {a}", "cyan")
    final_sz = gsz(target_dir)
    print(f"space saved : {fsz(initial_sz - final_sz)}")


def cmd_clean_record(args: list[Any]) -> None:
    allowed = {"METADATA", "RECORD", "WHEEL", "entry_points.txt", "top_level.txt"}
    for e in Path().glob("*.dist-info"):
        d_file = e / "RECORD"
        if d_file.exists():
            h = d_file.read_text().splitlines()
            f_list = []
            for k in h:
                if not k.strip():
                    continue
                i_val = k.split(",")
                n_val = i_val[0]
                g_path = Path(n_val)
                b_val = any((m.endswith(".dist-info") for m in g_path.parts))
                if b_val and g_path.name not in allowed:
                    print(f"Removed dist-info reference:{n_val}")
                    continue
                f_list.append(k)
            d_file.write_text("\n".join(f_list) + ("\n" if f_list else ""), encoding="utf-8")
            print(f"record file in {d_file.parent.name} cleaned.")


def cmd_clean_distinfo(args: list[Any]) -> None:
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    default_sp = Path(f"/data/data/com.termux/files/usr/lib/python{py_ver}/site-packages")
    sp = Path(args.site_dir) if args.site_dir else default_sp
    if not sp.exists():
        print(f"Site-packages directory {sp} does not exist.")
        return
    for h in sp.iterdir():
        if h.is_dir() and len(list(h.iterdir())) == 1 and "dist-info" in h.name:
            for k in h.iterdir():
                if k.name in {"top_level.txt", "entry_points.txt"}:
                    cprint(f"{h} removed", "cyan")
                    shutil.rmtree(h)


def cmd_fix_targz(args: list[Any]) -> None:
    target_dir = Path(args.directory)
    for l in target_dir.glob("*.tar.gz"):
        u, q = _extract_targz_meta(l)
        if u and q:
            new_name = f"{u}-{q}.tar.gz"
            k_path = target_dir / new_name
            if k_path != l:
                if k_path.exists():
                    print(f"SKIP:{l.name}->{new_name} (already exists)")
                else:
                    l.rename(k_path)
                    print(f"RENAMED:{l.name}->{new_name}")
        else:
            print(f"ERROR: Could not determine name/version for {l.name}")


def _extract_targz_meta(l: Any):
    with tarfile.open(l, "r:gz") as w:
        members = w.getmembers()
        if not members:
            return None, None
        p_prefix = members[0].name.split("/")[0]
        for g in ["PKG-INFO", "METADATA"]:
            try:
                r = w.getmember(f"{p_prefix}/{g}")
                m = w.extractfile(r).read().decode("utf-8", errors="ignore")
                u_match = re.search(r"^Name:\s*(.+)$", m, re.MULTILINE)
                q_match = re.search(r"^Version:\s*(.+)$", m, re.MULTILINE)
                if u_match and q_match:
                    return u_match.group(1).strip(), q_match.group(1).strip()
            except KeyError:
                continue
        for i_file in ["setup.py", "setup.cfg", "pyproject.toml"]:
            try:
                r = w.getmember(f"{p_prefix}/{i_file}")
                m = w.extractfile(r).read().decode("utf-8", errors="ignore")
                d_match = re.search(r"name\s*=\s*[\'\"]([^\'\"]+)[\'\"]", m)
                c_match = re.search(r"version\s*=\s*[\'\"]([^\'\"]+)[\'\"]", m)
                u_val = d_match.group(1) if d_match else None
                q_val = c_match.group(1) if c_match else None
                if u_val and q_val:
                    return u_val, q_val
            except KeyError:
                continue
        s_match = re.match(r"^(.+)-(\d[^-]*)$", p_prefix)
        if s_match:
            return s_match.group(1), s_match.group(2)
    return None, None


def cmd_rename_json(args: list[Any]) -> None:
    for b in [f for f in os.listdir(".") if f.endswith(".json")]:
        try:
            with Path(b).open(encoding="utf-8") as f:
                i_data = json.load(f)
            d_name = None
            if isinstance(i_data, dict):
                if "info" in i_data and isinstance(i_data["info"], dict) and "name" in i_data["info"]:
                    d_name = i_data["info"]["name"]
                elif "name" in i_data:
                    d_name = i_data["name"]
            if d_name:
                c_name = f"{d_name}.json"
                if b == c_name:
                    print(f"Skipping:{b} is already correctly named.")
                    continue
                Path(b).rename(c_name)
                print(f"Renamed:{b}->{c_name}")
            else:
                print(f"Warning: Could not find package name in {b}")
        except json.JSONDecodeError:
            print(f"Error:{b} is not a valid JSON file.")
        except Exception as e:
            print(f"An error occurred with {b}:{e}")


def cmd_rename_meta(args: list[Any]) -> None:
    target_c = Path(args.target_dir)
    target_c.mkdir(parents=True, exist_ok=True)
    p_path = Path.cwd()
    for o_file in get_files(p_path):
        if o_file.is_file() and (o_file.name == "METADATA" or o_file.suffix == ".metadata"):
            _process_meta_file(o_file, target_c)


def _process_meta_file(o: Path, c: Any) -> None:
    if not o.exists():
        return
    f_text = o.read_text(encoding="utf-8")
    k_lines = f_text.splitlines()
    if len(k_lines) < 3:
        return
    h_val, b_val = "", ""
    i_line = k_lines[1]
    j_line = k_lines[2]
    d_val = i_line.lower().strip()
    e_val = j_line.lower().strip()
    if d_val.startswith("name:"):
        h_val = d_val.replace("name:", "").lstrip()
    if e_val.startswith("version:"):
        b_val = e_val.replace("version:", "").lstrip()
    if b_val and h_val:
        l_name = Path(f"{h_val}-{b_val}.metadata")
        g_path = c / l_name
        if g_path.exists():
            g_path = unique_path(g_path)
        g_path.write_text(f_text, encoding="utf-8")
        cprint(f"{l_name} created.", "green")
    elif h_val and not b_val:
        l_name = Path(f"{h_val}.metadata")
        g_path = c / l_name
        if g_path.exists():
            g_path = unique_path(g_path)
        g_path.write_text(f_text, encoding="utf-8")
        cprint(f"{l_name} created.", "yellow")
    elif not h_val and not b_val:
        cprint(f"no data{o}", "cyan")
        with contextlib.suppress(EOFError):
            input("what u wanna do?")


def cmd_clean_pyc_record(args: list[Any]) -> None:
    import sysconfig

    lib_path = Path(sysconfig.get_paths()["purelib"])
    for e in lib_path.glob("*.dist-info"):
        h_file = e / "RECORD"
        if h_file.exists():
            _clean_record_pyc_file(h_file)
    print("Removed .pyc references from all RECORD files.")


def _clean_record_pyc_file(d: Path) -> None:
    i_lines = d.read_text(encoding="utf-8").splitlines()
    g_list = [k for k in i_lines if ".pyc" not in k]
    g_list = [k for k in g_list if "licenses" not in k]
    g_list = [k for k in g_list if "license.md" not in k.lower()]
    g_list = [k for k in g_list if "license.txt" not in k.lower()]
    d.write_text("\n".join(g_list) + "\n", encoding="utf-8")
    print(f"{d.name} in {d.parent.name} cleaned")


def cmd_update_record(args: list[Any]) -> None:
    m_path = Path.cwd()
    h_dirs = sorted(m_path.glob("*.dist-info"))
    if not h_dirs:
        logger.warning("you should run this script from a site packages folder.")
        sys.exit(0)
    print(f"Found {len(h_dirs)} distribution(s)")
    e_cnt, g_cnt = 0, 0
    with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
        for res in pool.imap_unordered(_update_single_record, h_dirs):
            if res:
                e_cnt += 1
            else:
                g_cnt += 1
    print(f"Summary:{e_cnt} updated,{g_cnt} failed")


def _hash_file(q: Path) -> str:
    r = hashlib.sha256()
    try:
        with q.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                r.update(chunk)
        digest = r.digest()
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"sha256={encoded}"
    except Exception:
        logger.exception("Error hashing {}", q)
        return ""


def _get_size(q: Path) -> int:
    try:
        return q.stat().st_size
    except Exception:
        logger.exception("Error getting size for {}", q)
        return 0


def _parse_record_line(line: str):
    parts = line.strip().split(",")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], "", ""


def _is_valid_record_file(q: Path) -> bool:
    p_name = q.name
    return not (q.suffix == ".pyc" or p_name.endswith(".pyc") or p_name in ("direct_url.json", "INSTALLER", "RECORD"))


def _update_single_record(i_dir: Path) -> bool:
    p_file = i_dir / "RECORD"
    print(f"Processing {p_file}")
    if not p_file.exists():
        logger.error("RECORD not found: {}", p_file)
        return False
    try:
        with p_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        logger.exception("Failed to read {}", p_file)
        return False
    v_list = []
    missing = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        l_path, _s_hash, _t_size = _parse_record_line(stripped)
        if l_path == "RECORD":
            continue
        u_file = i_dir.parent / l_path
        if not _is_valid_record_file(u_file):
            logger.debug("Skipping excluded file: {}", l_path)
            continue
        if not u_file.exists():
            missing.append(l_path)
            logger.warning("Missing file: {}", l_path)
            continue
        new_hash = _hash_file(u_file)
        if not new_hash:
            logger.warning("Hash failed for {}, keeping original line", l_path)
            v_list.append(line.rstrip("\n").rstrip("\r"))
            continue
        new_size = _get_size(u_file)
        v_list.append(f"{l_path},{new_hash},{new_size}")
    rel_p = str(p_file.relative_to(i_dir.parent))
    v_list.append(f"{rel_p},,")
    if missing:
        logger.warning("{} missing files in {}: {}", len(missing), i_dir.name, ",".join(missing))
    try:
        p_file.write_text("\n".join(v_list) + "\n", encoding="utf-8")
        print(f"Updated {p_file}")
    except Exception:
        logger.exception("Failed to write {}", p_file)
        return False
    try:
        self_hash = _hash_file(p_file)
        self_size = _get_size(p_file)
        with p_file.open("r", encoding="utf-8") as f:
            n_lines = f.readlines()
        if n_lines:
            last_parts = n_lines[-1].strip().split(",")
            if len(last_parts) >= 1:
                n_lines[-1] = f"{last_parts[0]},{self_hash},{self_size}\n"
        p_file.write_text("".join(n_lines), encoding="utf-8")
        logger.debug("Self-hash updated for {}", p_file)
    except Exception:
        logger.exception("Failed to update self-hash for {}", p_file)
    return True


def cmd_clean_urecord(args: list[Any]) -> None:
    site_dirs = args.site_dir or _get_default_site_packages()
    if not site_dirs:
        print("Error: Could not find site-packages directory", file=sys.stderr)
        sys.exit(1)
    print(f"Python version: {sys.version}")
    print(f"Site packages directories: {','.join(str(d) for d in site_dirs)}")
    total_found, total_updated = _scan_and_clean_urecord(site_dirs, args.verbose)
    print(f"\n{'=' * 40}")
    print("Summary:")
    print(f"  Total RECORD files found: {total_found}")
    print(f"  Files that would be/are updated: {total_updated}")


def _get_default_site_packages() -> list[Path]:
    import site

    try:
        paths = site.getsitepackages()
        valid = [Path(p) for p in paths if p is not None and Path(p).exists()]
        if not valid:
            user_sp = site.getusersitepackages()
            if user_sp and Path(user_sp).exists():
                valid = [Path(user_sp)]
        return valid
    except Exception:
        return []


def _clean_single_urecord_file(k_path: Path, verbose: bool) -> bool:
    try:
        with k_path.open(encoding="utf-8") as f:
            reader = list(csv.reader(f))
        h_len = len(reader)
        g_rows = []
        for row in reader:
            if not row:
                continue
            y_val = row[0] if row else ""
            if y_val.endswith(".pyc") or y_val in {"direct_url.json", "INSTALLER"} or y_val.startswith("LICENSE"):
                continue
            g_rows.append(row)
        if len(g_rows) == h_len:
            return False
        with k_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(g_rows)
        print(f"  Updated: {k_path} (removed {h_len - len(g_rows)} entries)")
        return True
    except Exception as e:
        print(f"  Error processing {k_path}:{e}", file=sys.stderr)
        return False


def _scan_and_clean_urecord(site_dirs: Iterable[Any], verbose: bool) -> tuple[int, int]:
    total_found = 0
    total_updated = 0
    for r in site_dirs:
        r_path = Path(r)
        if not r_path.exists():
            print(f"Directory does not exist: {r_path}")
            continue
        print(f"\nScanning: {r_path}")
        for y_path in r_path.rglob("*"):
            if y_path.name == "RECORD":
                total_found += 1
                if _clean_single_urecord_file(y_path, verbose):
                    total_updated += 1
    return total_found, total_updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Python Package and Metadata Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("clean-meta", help="Clean metadata and markdown files")
    subparsers.add_parser("clean-record", help="Clean dist-info RECORD files of invalid entries")
    p_cd = subparsers.add_parser("clean-distinfo", help="Remove empty or single-item dist-info dirs in site-packages")
    p_cd.add_argument("--site-dir", "-s", help="Specific site-packages directory")
    p_ft = subparsers.add_parser("fix-targz", help="Fix and standardize .tar.gz package filenames")
    p_ft.add_argument("directory", nargs="?", default=".", help="Directory containing .tar.gz files")
    subparsers.add_parser("rename-json", help="Rename JSON files based on internal package name")
    p_rm = subparsers.add_parser("rename-meta", help="Rename and move METADATA/.metadata files")
    p_rm.add_argument("--target-dir", default="/data/data/com.termux/files/home/tmp/metadata", help="Target directory")
    subparsers.add_parser("clean-pyc-record", help="Remove .pyc and license entries from purelib RECORD files")
    p_ur = subparsers.add_parser("update-record", help="Update SHA256 hashes and sizes in dist-info RECORD files")
    p_cu = subparsers.add_parser(
        "clean-urecord", help="Remove .pyc, direct_url.json, INSTALLER, LICENSE from RECORD files"
    )
    p_cu.add_argument("--site-dir", "-s", action="append", help="Specific site-packages directory")
    p_cu.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()
    if args.command == "clean-meta":
        cmd_clean_meta(args)
    elif args.command == "clean-record":
        cmd_clean_record(args)
    elif args.command == "clean-distinfo":
        cmd_clean_distinfo(args)
    elif args.command == "fix-targz":
        cmd_fix_targz(args)
    elif args.command == "rename-json":
        cmd_rename_json(args)
    elif args.command == "rename-meta":
        cmd_rename_meta(args)
    elif args.command == "clean-pyc-record":
        cmd_clean_pyc_record(args)
    elif args.command == "update-record":
        cmd_update_record(args)
    elif args.command == "clean-urecord":
        cmd_clean_urecord(args)


if __name__ == "__main__":
    raise SystemExit(main())
