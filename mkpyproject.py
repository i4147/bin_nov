#!/data/data/com.termux/files/usr/bin/env python
"""Generate a pyproject.toml from an unpacked wheel's .dist-info directory.

Reads METADATA from *.dist-info, maps the core fields (name, version, dependencies, ...) to [project] TOML and writes pyproject.toml; .dist-info handling is configurable (keep/backup/remove).
"""

from __future__ import annotations
import argparse
import configparser
from email.parser import HeaderParser
import json
import os
from pathlib import Path
import shutil
import sys


def toml_str(val: str) -> str:
    """Safely formats strings for TOML using JSON escaping."""
    return json.dumps(val)


def toml_list(items: list[str], indent: int = 4) -> str:
    """Formats a Python list as a clean multi-line TOML array."""
    if not items:
        return "[]"
    pad = " " * indent
    inner = ",\n".join(f"{pad}{toml_str(i)}" for i in items)
    return f"[\n{inner}\n]"


def _safe_move(src: Path, dst: Path, *, overwrite: bool = False) -> bool:
    """Moves a file or directory safely, handling existing targets and cross-device moves."""
    if not src.exists():
        return False
    if dst.exists():
        if not overwrite:
            return False
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst)
    except OSError:
        shutil.move(str(src), str(dst))
    return True


def _write_text_atomic(path: Path, content: str) -> None:
    """Writes UTF-8 text atomically via a temporary file in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def handle_data_folder(
    base_dir: Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> tuple[dict[str, list[str]], list[str]]:
    """Unpacks standard .data subdirectories into the root so setuptools finds them."""
    data_dirs = sorted(base_dir.glob("*.data"))
    if not data_dirs:
        return {}, []

    data_dir = data_dirs[0]
    mapped_data_files: dict[str, list[str]] = {}
    script_files: list[str] = []

    with os.scandir(data_dir) as entries:
        subdirs = [entry for entry in entries if entry.is_dir()]

    for sub_entry in subdirs:
        sub = sub_entry.name
        sub_path = Path(sub_entry.path)

        if sub in ("purelib", "platlib"):
            with os.scandir(sub_path) as items:
                for item in items:
                    src = Path(item.path)
                    dst = base_dir / item.name
                    if dry_run:
                        continue
                    _safe_move(src, dst, overwrite=overwrite)

        elif sub == "scripts":
            scripts_dest = base_dir / "_wheel_scripts"
            with os.scandir(sub_path) as items:
                for item in items:
                    src = Path(item.path)
                    dst = scripts_dest / item.name
                    if not dry_run:
                        _safe_move(src, dst, overwrite=overwrite)
                    script_files.append(f"_wheel_scripts/{item.name}")

        elif sub in ("data", "headers"):
            for root, _, files in os.walk(sub_path):
                if not files:
                    continue
                root_path = Path(root)
                rel_dir = root_path.relative_to(sub_path)
                target_dir = "." if str(rel_dir) == "." else rel_dir.as_posix()
                local_store = base_dir / "_wheel_data" / sub / rel_dir

                moved_paths: list[str] = []
                for fname in sorted(files):
                    src_file = root_path / fname
                    dst_file = local_store / fname
                    if not dry_run:
                        _safe_move(src_file, dst_file, overwrite=overwrite)
                    norm_path = (Path("_wheel_data") / sub / rel_dir / fname).as_posix()
                    moved_paths.append(norm_path)

                mapped_data_files.setdefault(target_dir, []).extend(moved_paths)

    if not dry_run:
        shutil.rmtree(data_dir, ignore_errors=True)

    return mapped_data_files, script_files


def detect_top_level_modules(base_dir: Path, exclude_names: set[str]) -> list[str]:
    """Finds standalone top-level .py modules (excluding setup/generator scripts)."""
    py_modules: list[str] = []
    with os.scandir(base_dir) as entries:
        for entry in entries:
            if entry.is_file() and entry.name.endswith(".py") and entry.name not in exclude_names:
                py_modules.append(Path(entry.name).stem)
    return sorted(py_modules)


def preserve_wheel_tags(
    base_dir: Path,
    dist_info: Path,
    *,
    dry_run: bool = False,
) -> list[str] | None:
    """Reads *.dist-info/WHEEL and writes [bdist_wheel] options to setup.cfg."""
    wheel_file = dist_info / "WHEEL"
    if not wheel_file.is_file():
        return None

    wheel_headers = HeaderParser().parsestr(wheel_file.read_text(encoding="utf-8"))
    tags = wheel_headers.get_all("Tag") or []
    if not tags:
        return None

    py_tags: list[str] = []
    abi_tags: list[str] = []
    plat_tags: list[str] = []

    for tag in tags:
        parts = tag.strip().split("-")
        if len(parts) == 3:
            py, abi, plat = parts
            if py not in py_tags:
                py_tags.append(py)
            if abi not in abi_tags:
                abi_tags.append(abi)
            if plat not in plat_tags:
                plat_tags.append(plat)

    is_pure_universal = set(py_tags) <= {"py3", "py2", "py2.py3"} and abi_tags == ["none"] and plat_tags == ["any"]
    if is_pure_universal:
        return None

    cfg_lines = ["[bdist_wheel]"]
    if set(py_tags) == {"py2", "py3"} or py_tags == ["py2.py3"]:
        cfg_lines.append("universal = 1")
    elif py_tags and py_tags != ["py3"]:
        cfg_lines.append(f"python-tag = {'.'.join(py_tags)}")

    if plat_tags and plat_tags != ["any"]:
        cfg_lines.append(f"plat-name = {plat_tags[0]}")

    if not dry_run:
        _write_text_atomic(base_dir / "setup.cfg", "\n".join(cfg_lines) + "\n")

    return tags


def generate_pyproject(
    base_dir: str | Path = ".",
    cleanup_dist_info: str = "backup",
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> Path:
    root = Path(base_dir).resolve()
    dist_info_dirs = sorted(p for p in root.glob("*.dist-info") if p.is_dir())
    if not dist_info_dirs:
        msg = f"No .dist-info directory found in '{root}'."
        raise FileNotFoundError(msg)
    dist_info = dist_info_dirs[0]

    # 1. Process .data folder first
    data_files_map, script_files = handle_data_folder(root, overwrite=overwrite, dry_run=dry_run)

    # 2. Preserve platform/ABI wheel tags into setup.cfg if platform-specific
    preserved_tags = preserve_wheel_tags(root, dist_info, dry_run=dry_run)

    # 3. Parse METADATA
    metadata_path = dist_info / "METADATA"
    raw_meta = metadata_path.read_text(encoding="utf-8")

    parts = raw_meta.split("\n\n", 1)
    headers = HeaderParser().parsestr(parts[0])
    long_description = parts[1].strip() if len(parts) > 1 else ""

    name = headers.get("Name", "unknown-package")
    version = headers.get("Version", "0.0.0")
    summary = headers.get("Summary", "")
    author = headers.get("Author", "")
    author_email = headers.get("Author-email", "")
    url = headers.get("Home-page", "")
    license_name = headers.get("License", "")
    python_requires = headers.get("Requires-Python", "")
    classifiers = headers.get_all("Classifier") or []

    readme_field = ""
    if long_description:
        content_type = headers.get("Description-Content-Type", "text/markdown")
        readme_ext = "rst" if "rst" in content_type else "md"
        readme_name = f"README.{readme_ext}"
        if not dry_run:
            _write_text_atomic(root / readme_name, long_description + "\n")
        readme_field = f"readme = {toml_str(readme_name)}"

    # 4. Parse Dependencies & Optional Extras
    dependencies: list[str] = []
    optional_deps: dict[str, list[str]] = {}
    for req in headers.get_all("Requires-Dist") or []:
        if "; extra ==" in req:
            pkg_part, extra_part = req.split("; extra ==", 1)
            extra_name = extra_part.strip().strip("'\"").split(";")[0].strip("'\" ")
            optional_deps.setdefault(extra_name, []).append(pkg_part.strip())
        else:
            dependencies.append(req.strip())

    # 5. Parse Entry Points
    console_scripts: dict[str, str] = {}
    gui_scripts: dict[str, str] = {}
    other_entry_points: dict[str, dict[str, str]] = {}
    ep_path = dist_info / "entry_points.txt"
    if ep_path.is_file():
        config = configparser.ConfigParser()
        config.read(ep_path, encoding="utf-8")
        for section in config.sections():
            if section == "console_scripts":
                console_scripts = dict(config.items(section))
            elif section == "gui_scripts":
                gui_scripts = dict(config.items(section))
            else:
                other_entry_points[section] = dict(config.items(section))

    # 6. Detect top-level single-file .py modules
    self_script = Path(__file__).name if "__file__" in globals() else ""
    py_modules = detect_top_level_modules(root, exclude_names={self_script, "setup.py"})

    # 7. Build pyproject.toml content
    lines: list[str] = [
        "[build-system]",
        'requires = ["setuptools>=61.0"]',
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {toml_str(name)}",
        f"version = {toml_str(version)}",
    ]

    if summary:
        lines.append(f"description = {toml_str(summary)}")
    if readme_field:
        lines.append(readme_field)
    if python_requires:
        lines.append(f"requires-python = {toml_str(python_requires)}")
    if license_name:
        lines.append(f"license = {{ text = {toml_str(license_name)} }}")
    if author or author_email:
        author_parts: list[str] = []
        if author:
            author_parts.append(f"name = {toml_str(author)}")
        if author_email:
            author_parts.append(f"email = {toml_str(author_email)}")
        lines.append(f"authors = [{{ {', '.join(author_parts)} }}]")
    if classifiers:
        lines.append(f"classifiers = {toml_list(classifiers)}")
    if dependencies:
        lines.append(f"dependencies = {toml_list(dependencies)}")

    if url:
        lines.extend(["", "[project.urls]", f"Homepage = {toml_str(url)}"])

    if optional_deps:
        lines.extend(["", "[project.optional-dependencies]"])
        for extra, pkgs in optional_deps.items():
            lines.append(f"{toml_str(extra)} = {toml_list(pkgs)}")

    if console_scripts:
        lines.extend(["", "[project.scripts]"])
        for k, v in console_scripts.items():
            lines.append(f"{toml_str(k)} = {toml_str(v)}")

    if gui_scripts:
        lines.extend(["", "[project.gui-scripts]"])
        for k, v in gui_scripts.items():
            lines.append(f"{toml_str(k)} = {toml_str(v)}")

    for section, items in other_entry_points.items():
        lines.extend(["", f"[project.entry-points.{toml_str(section)}]"])
        for k, v in items.items():
            lines.append(f"{toml_str(k)} = {toml_str(v)}")

    # 8. Setuptools configuration
    lines.extend([
        "",
        "[tool.setuptools]",
        "include-package-data = true",
    ])
    if py_modules:
        lines.append(f"py-modules = {toml_list(py_modules)}")
    if script_files:
        lines.append(f"script-files = {toml_list(script_files)}")

    lines.extend([
        "",
        "[tool.setuptools.packages.find]",
        'where = ["."]',
        ('exclude = ["*.dist-info*", "_dist_info_backup*", "_wheel_data*", "_wheel_scripts*"]'),
    ])

    if preserved_tags:
        lines.extend([
            "",
            "[tool.setuptools.package-data]",
            '"*" = ["*.so", "*.so.*", "*.pyd", "*.dylib", "*.dll"]',
        ])

    if data_files_map:
        lines.extend(["", "[tool.setuptools.data-files]"])
        for target, files in data_files_map.items():
            lines.append(f"{toml_str(target)} = {toml_list(files)}")

    output_path = root / "pyproject.toml"
    if not dry_run:
        _write_text_atomic(output_path, "\n".join(lines) + "\n")

        # 9. Clean up or back up *.dist-info
        for d in dist_info_dirs:
            if cleanup_dist_info == "delete":
                shutil.rmtree(d, ignore_errors=True)
            elif cleanup_dist_info == "backup":
                backup_dir = root / "_dist_info_backup"
                dest = backup_dir / d.name
                _safe_move(d, dest, overwrite=True)

    if not quiet:
        prefix = "[dry-run] " if dry_run else ""
        print(f"{prefix}Generated {output_path} for {name} ({version})")
        if preserved_tags:
            print(f"{prefix}Preserved wheel tags in setup.cfg: {', '.join(preserved_tags)}")
        if py_modules:
            print(f"{prefix}Detected top-level modules: {', '.join(py_modules)}")
        if cleanup_dist_info != "keep":
            print(f"{prefix}Handled .dist-info via '{cleanup_dist_info}' mode (ready for rebuild).")

    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert an unpacked Python wheel (.dist-info / .data) into pyproject.toml."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target directory containing *.dist-info (default: current directory).",
    )
    parser.add_argument(
        "--cleanup",
        choices=("backup", "delete", "keep"),
        default="backup",
        help="How to handle *.dist-info after generating pyproject.toml (default: backup).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files when unpacking .data subdirectories.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview actions without modifying or writing files on disk.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational status output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        generate_pyproject(
            base_dir=args.directory,
            cleanup_dist_info=args.cleanup,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
