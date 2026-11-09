#!/data/data/com.termux/files/usr/bin/env python
"""Generate a ``requirements.txt`` by inspecting the Python files of a project.
Collects the third-party imports with ast, resolves their installed versions, skips the local packages and the stdlib and writes the pinned requirements; ``-s`` writes a separate file per subdirectory.
Usage: imports [-s] [paths ...] Options: -s, --save-separate Save separate requirements.txt for each subdirectory Examples: $ imports -s src/ Command-line script: run ``python imports.py`` or the ``imports`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
import importlib.metadata
import importlib.util
from multiprocessing import Pool
import numbers
from pathlib import Path
import time

from dh import NUM_WORKERS, SKIP_DIRS, STDLIB, get_installed_pkgs


class ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for node_name in node.names:
            self.imports.add(node_name.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self.imports.add(node.module.split(".")[0])
        self.generic_visit(node)


def get_local_packages(start_path: Path) -> set[str]:
    packages: set[str] = set()
    for init_file in start_path.rglob("__init__.py"):
        if any(part in SKIP_DIRS for part in init_file.parts):
            continue
        packages.add(init_file.parent.name)
    return packages


def _process_file(path: Path) -> tuple[Path, set[str], bool, str | None]:
    imports: set[str] = set()
    error: str | None = None
    try:
        code = path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        visitor = ImportVisitor()
        visitor.visit(tree)
        imports = visitor.imports
    except SyntaxError as e:
        error = f"SyntaxError: {e}"
    except UnicodeDecodeError as e:
        error = f"UnicodeDecodeError: {e}"
    except Exception as e:
        error = f"Error: {e}"
    return path, imports, error is None, error


def has_python_files(dir_path: Path) -> bool:
    try:
        for item in dir_path.rglob("*.py"):
            if any(part in SKIP_DIRS for part in item.parts):
                continue
            return True
    except (PermissionError, OSError):
        return False
    return False


def find_imports_for_directory(
    dir_path: Path, start_path: Path, std_libs: set[str], all_local_packages: set[str]
) -> list[str]:
    files: list[Path] = []
    for py_file in dir_path.rglob("*.py"):
        if py_file.is_file():
            if py_file.name.startswith(("test_", "tests_")) or py_file.name.endswith(("_test.py", "_tests.py")):
                continue
            if any(part in SKIP_DIRS for part in py_file.relative_to(dir_path).parts):
                continue
            files.append(py_file)
    if not files:
        return []
    all_imports: set[str] = set()
    with Pool(processes=NUM_WORKERS) as pool:
        results = pool.map(_process_file, files)
    for _path, imports, success, _error in results:
        if success:
            all_imports.update(imports)
    local_modules: set[str] = {p.stem for p in dir_path.glob("*.py") if not any(part in SKIP_DIRS for part in p.parts)}
    local_packages: set[str] = get_local_packages(dir_path)
    local_names: set[str] = local_modules | local_packages | all_local_packages
    result: list[str] = sorted([
        imp
        for imp in all_imports
        if imp not in std_libs and imp not in local_names and not imp.startswith(".") and imp != "__future__"
    ])
    return result


def save_requirements_file(modules: list[str], output_path: Path, pkgz: set[str]) -> bool:
    modules = sorted(set(modules))
    results: list[str] = []
    for mod in modules:
        if mod.startswith("_"):
            continue
        ver = get_version(mod)
        if "Not Installed" in ver:
            results.append(f"{mod}=={ver}")
    if not results:
        return False
    output_path.write_text("\n".join(results), encoding="utf-8")
    cleaned: list[str] = []
    with output_path.open(encoding="utf-8") as fin:
        lines = fin.readlines()
        cleaned.extend(
            line
            .rstrip()
            .replace("Not Installed", "")
            .replace("==(NA)", "")
            .replace("==(unknown)", "")
            .replace("==", "")
            for line in lines
        )
    seen: set[str] = set()
    unique_cleaned: list[str] = []
    for p in cleaned:
        if p and p not in pkgz and not p.startswith("_") and p not in seen:
            seen.add(p)
            unique_cleaned.append(p)
    if not unique_cleaned:
        if output_path.exists():
            output_path.unlink()
        return False
    with output_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(unique_cleaned))
    return True


def get_version(module_name: str) -> str:
    try:
        return importlib.metadata.version(module_name)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return "Not Installed"
        mod = importlib.import_module(module_name)
        for k, v in mod.__dict__.items():
            if ("version" in k.lower() or "ver" in k.lower()) and isinstance(v, (str, numbers.Number)):
                return str(v)
    except Exception:
        return "Not Installed(unknown)"
    return "Not Installed(NA)"


def get_valid_subdirs(start_path: Path) -> list[Path]:
    subdirs: list[Path] = []
    for d in start_path.iterdir():
        if not d.is_dir():
            continue
        if d.name.startswith("."):
            continue
        if d.name.startswith("__"):
            continue
        if d.name in SKIP_DIRS:
            continue
        if has_python_files(d):
            subdirs.append(d)
    return sorted(subdirs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate requirements.txt by inspecting Python files")
    parser.add_argument(
        "-s",
        "--save-separate",
        action="store_true",
        help="Save separate requirements.txt for each subdirectory",
    )
    args = parser.parse_args()
    overall_start = time.time()
    cwd = Path.cwd()
    output_file = cwd / "requirements.txt"
    std_libs: set[str] = set(STDLIB)
    pkgz: set[str] = set(get_installed_pkgs())
    all_local_packages: set[str] = get_local_packages(cwd)
    subdirs: list[Path] = get_valid_subdirs(cwd)
    if args.save_separate and subdirs:
        total_imports: set[str] = set()
        created_count: int = 0
        skipped_count: int = 0
        for idx, subdir in enumerate(subdirs, 1):
            dir_start = time.time()
            print(
                f"[{idx}/{len(subdirs)}] Processing {subdir.name}... ",
                end="",
                flush=True,
            )
            modules = find_imports_for_directory(subdir, cwd, std_libs, all_local_packages)
            if not modules:
                elapsed = time.time() - dir_start
                print(f"⏭️  no third-party imports ({elapsed:.2f}s)")
                skipped_count += 1
                continue
            subdir_req = subdir / "requirements.txt"
            created = save_requirements_file(modules, subdir_req, pkgz)
            elapsed = time.time() - dir_start
            if created:
                print(f"✅ created ({len(modules)} packages, {elapsed:.2f}s)")
                total_imports.update(modules)
                created_count += 1
            else:
                print(f"⏭️  all installed ({elapsed:.2f}s)")
                skipped_count += 1
        if total_imports:
            print("\n📦 Generating root requirements.txt with all unique imports...")
            root_modules: list[str] = sorted(set(total_imports))
            root_created = save_requirements_file(root_modules, output_file, pkgz)
            if root_created:
                print(f"✅ {output_file} ({len(root_modules)} unique packages)")
            else:
                print("⏭️  All combined packages already installed, no root requirements.txt created")
                if output_file.exists():
                    output_file.unlink()
        else:
            print("\n⏭️  No third-party imports found in any subdirectory")
            if output_file.exists():
                output_file.unlink()
    else:
        print("\n📦 Processing entire directory...")
        files: list[Path] = []
        for py_file in cwd.rglob("*.py"):
            if "test" in py_file.name:
                continue
            if py_file.is_file():
                try:
                    rel_path = py_file.relative_to(cwd)
                    if any(part in SKIP_DIRS for part in rel_path.parts):
                        continue
                except ValueError:
                    pass
                files.append(py_file)
        if not files:
            print("No Python files found.")
            if output_file.exists():
                output_file.unlink()
            return
        files_by_dir: dict[str, list[Path]] = defaultdict(list)
        for f in files:
            try:
                rel_path = f.relative_to(cwd)
                if len(rel_path.parts) > 1:
                    subdir = rel_path.parts[0]
                else:
                    subdir = "."
            except ValueError:
                subdir = str(f.parent)
            files_by_dir[subdir].append(f)
        show_progress = len(files_by_dir) > 50
        if show_progress:
            print(f"Processing {len(files_by_dir)} directories with {len(files)} total files...")
            print("-" * 40)
        all_imports: set[str] = set()
        dir_count: int = 0
        for subdir, dir_files in sorted(files_by_dir.items()):
            dir_count += 1
            if show_progress:
                start_time = time.time()
            with Pool(processes=NUM_WORKERS) as pool:
                results = pool.map(_process_file, dir_files)
            for _path, imports, success, _error in results:
                if success:
                    all_imports.update(imports)
            if show_progress:
                elapsed = time.time() - start_time
                print(f"[{dir_count}/{len(files_by_dir)}] {subdir:<30} ({len(dir_files):>4} files, {elapsed:.2f}s)")
        if show_progress:
            print("-" * 40)
        local_modules: set[str] = {p.stem for p in cwd.glob("*.py") if not any(part in SKIP_DIRS for part in p.parts)}
        local_names: set[str] = local_modules | all_local_packages
        modules: list[str] = sorted({
            imp
            for imp in all_imports
            if imp not in std_libs and imp not in local_names and not imp.startswith(".") and imp != "__future__"
        })
        if modules:
            print(f"\n{'Module':<20} | {'Version':<15}")
            print("-" * 40)
            for mod in modules:
                if mod.startswith("_"):
                    continue
                ver = get_version(mod)
                line = f"{mod:<20} | {ver:<15}"
                print(line)
            created = save_requirements_file(modules, output_file, pkgz)
            if created:
                print(f"\n✅ Created {output_file} ({len(modules)} unique packages)")
            else:
                print("\n✅ All packages already installed, no requirements.txt created")
                if output_file.exists():
                    output_file.unlink()
        else:
            print("\n✅ No third-party imports found")
            if output_file.exists():
                output_file.unlink()
    overall_elapsed = time.time() - overall_start
    if overall_elapsed > 1.0:
        print(f"\n⏱️  Total time: {overall_elapsed:.2f}s")


if __name__ == "__main__":
    raise SystemExit(main())
