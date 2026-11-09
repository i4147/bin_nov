#!/data/data/com.termux/files/usr/bin/env python
"""Generate a ``setup.py`` from the project's ``pyproject.toml``.
Detects the build backend (setuptools, poetry, flit, hatchling ...), reads the PEP 621 metadata (with TOML fallbacks), discovers packages and package data and writes a ``setup.py`` that reproduces that metadata for tools that still need one.
Usage: mksetuppy Examples: $ mksetuppy Runs as a script (``python mksetuppy.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
from collections.abc import Callable
import email.utils
import importlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from setuptools import find_packages, setup


ROOT = Path.cwd()


def raise_exception(error: OSError) -> None:
    raise error


def auto_find_packages(
    module_name: str,
    subdirectory: Path = ROOT,
) -> dict[str, Any]:
    package_path = subdirectory / module_name
    result: dict[str, Any] = {}
    if subdirectory != ROOT:
        result["package_dir"] = {"": str(subdirectory)}
    if package_path.is_dir():
        result["packages"] = find_packages(
            where=str(subdirectory),
            include=(module_name, f"{module_name}.*"),
        )
    elif (subdirectory / f"{module_name}.py").is_file():
        result["py_modules"] = [module_name]
    else:
        msg = f"No package matching {module_name!r} found in {subdirectory}"
        raise RuntimeError(msg)
    return result


def find_package_data(
    packages: list[str],
    package_dirs: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    package_dirs = package_dirs or {}
    result: defaultdict[str, list[str]] = defaultdict(list)
    result[""] = ["*"]
    for package in packages:
        package_root = package_dirs.get(package)
        if package_root is None:
            base_directory = package_dirs.get("", "")
            package_root = str(Path(base_directory) / Path(package.replace(".", "/")))
        package_path = Path(package_root)
        if not package_path.is_dir():
            continue
        for directory in package_path.rglob("*"):
            if not directory.is_dir():
                continue
            relative_parts = directory.relative_to(package_path).parts
            if "__pycache__" in relative_parts or any(part.startswith(".") for part in relative_parts):
                continue
            if not (directory / "__init__.py").exists():
                relative_path = directory.relative_to(package_path)
                result[package].append(f"{relative_path}/*")
    return {package: sorted(set(paths)) for package, paths in result.items()}


def get_pep621_metadata(
    data: dict[str, Any],
    allow_dynamic: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if "project" not in data:
        return None
    metadata = data["project"]
    authors: list[str] = []
    author_emails: list[str] = []
    for author in metadata.get("authors", []):
        if "name" in author:
            authors.append(author["name"])
        if "email" in author:
            author_emails.append(author["email"])
    entry_points: defaultdict[str, list[str]] = defaultdict(list)
    for name, target in metadata.get("scripts", {}).items():
        entry_points["console_scripts"].append(f"{name} = {target}")
    for name, target in metadata.get("gui-scripts", {}).items():
        entry_points["gui_scripts"].append(f"{name} = {target}")
    for group_name, group_content in metadata.get("entrypoints", {}).items():
        if group_name in {"console_scripts", "gui_scripts"}:
            msg = f"{group_name} is forbidden in [project.entrypoints]"
            raise ValueError(msg)
        for name, target in group_content.items():
            entry_points[group_name].append(f"{name} = {target}")
    for key in allow_dynamic:
        is_static = key in metadata
        is_dynamic = key in metadata.get("dynamic", [])
        if is_static and is_dynamic:
            msg = f"Key {key!r} is declared both statically and dynamically"
            raise ValueError(msg)
        if not is_static and not is_dynamic:
            msg = f"Key {key!r} must be declared either statically or dynamically"
            raise ValueError(msg)
    return {
        "name": metadata["name"],
        "version": metadata.get("version"),
        "description": metadata.get("description"),
        "author": ", ".join(authors),
        "author_email": ", ".join(author_emails),
        "classifiers": metadata.get("classifiers", []),
        "entry_points": dict(entry_points),
    }


def handle_flit(data: dict[str, Any]) -> None:
    setup_metadata = get_pep621_metadata(
        data,
        allow_dynamic=("version", "description"),
    )
    module_name: str | None = None
    if setup_metadata is not None:
        flit_metadata = data.get("tool", {}).get("flit", {}).get("metadata")
        if flit_metadata is not None:
            msg = "[project] and [tool.flit.metadata] cannot be used together"
            raise ValueError(msg)
        module_name = data.get("tool", {}).get("flit", {}).get("module", {}).get("name")
    else:
        flit_data = data["tool"]["flit"]
        metadata = flit_data["metadata"]
        entry_points: defaultdict[str, list[str]] = defaultdict(list)
        for name, target in flit_data.get("scripts", {}).items():
            entry_points["console_scripts"].append(f"{name} = {target}")
        for group_name, group_content in flit_data.get("entrypoints", {}).items():
            for name, target in group_content.items():
                entry_points[group_name].append(f"{name} = {target}")
        setup_metadata = {
            "name": metadata["module"],
            "version": None,
            "description": None,
            "author": metadata.get("author"),
            "author_email": metadata.get("author-email"),
            "url": metadata.get("home-page"),
            "classifiers": metadata.get("classifiers", []),
            "entry_points": dict(entry_points),
        }
    module_name = module_name or setup_metadata["name"]
    if setup_metadata["version"] is None or setup_metadata["description"] is None:
        sys.path.insert(0, str(ROOT))
        sys.path.insert(1, str(ROOT / "src"))
        module = importlib.import_module(module_name.replace("/", "."))
        if setup_metadata["version"] is None:
            setup_metadata["version"] = module.__version__
        if setup_metadata["description"] is None:
            setup_metadata["description"] = " ".join(module.__doc__.strip().splitlines())
    try:
        package_args = auto_find_packages(module_name)
    except RuntimeError:
        package_args = auto_find_packages(module_name, ROOT / "src")
    setup_metadata.update(package_args)
    setup_metadata["package_data"] = find_package_data(
        setup_metadata.get("packages", []),
        setup_metadata.get("package_dir", {}),
    )
    setup(**setup_metadata)


def handle_flit_thyself(data: dict[str, Any]) -> None:
    build_system = data["build-system"]
    backend_path = build_system["backend-path"]
    if isinstance(backend_path, str):
        backend_path = [backend_path]
    sys.path = [str(ROOT / path) for path in backend_path] + sys.path
    backend_name = build_system["build-backend"]
    module = importlib.import_module(backend_name)
    metadata = module.metadata_dict
    package_name = backend_name.split(".")[0]
    package_args = auto_find_packages(package_name)
    setup(
        name=module.metadata.name,
        version=module.metadata.version,
        description=module.metadata.summary,
        author=metadata["author"],
        author_email=metadata["author_email"],
        url=metadata.get("home_page"),
        classifiers=metadata.get("classifiers", []),
        **package_args,
    )


def handle_poetry(data: dict[str, Any]) -> None:
    metadata = data["tool"]["poetry"]
    authors: list[str] = []
    author_emails: list[str] = []
    for author in metadata.get("authors", []):
        name, address = email.utils.parseaddr(author)
        authors.append(name)
        author_emails.append(address)
    if "packages" not in metadata:
        canonical_name = re.sub(
            r"[-.]",
            "_",
            metadata["name"].lower(),
        )
        try:
            package_args = auto_find_packages(canonical_name)
        except RuntimeError:
            package_args = auto_find_packages(
                canonical_name,
                ROOT / "src",
            )
    else:
        package_args = {
            "packages": [],
            "package_dir": {},
        }
        for package in metadata["packages"]:
            if package.get("format", "") == "sdist":
                continue
            include = package["include"]
            subdirectory = ROOT / package.get("from", ".")
            packages = find_packages(
                where=str(subdirectory),
                include=(include, f"{include}.*"),
            )
            package_args["packages"].extend(packages)
            if subdirectory != ROOT:
                for package_name in packages:
                    package_args["package_dir"][package_name] = str(subdirectory / package_name.replace(".", "/"))
    package_args["package_data"] = find_package_data(
        package_args.get("packages", []),
        package_args.get("package_dir", {}),
    )
    if metadata.get("exclude"):
        msg = "Poetry's exclude option is not implemented"
        raise NotImplementedError(msg)
    entry_points: defaultdict[str, list[str]] = defaultdict(list)
    for name, target in metadata.get("scripts", {}).items():
        entry_points["console_scripts"].append(f"{name} = {target}")
    for group_name, group_content in metadata.get(
        "plugins",
        {},
    ).items():
        for name, target in group_content.items():
            entry_points[group_name].append(f"{name} = {target}")
    setup(
        name=metadata["name"],
        version=metadata["version"],
        description=metadata["description"],
        author=", ".join(authors),
        author_email=", ".join(author_emails),
        url=metadata.get("homepage"),
        classifiers=metadata.get("classifiers", []),
        entry_points=dict(entry_points),
        **package_args,
    )


def handle_setuptools(data: dict[str, Any]) -> None:
    setup_py = ROOT / "setup.py"
    if setup_py.exists():
        result = subprocess.run(
            [sys.executable, str(setup_py), *sys.argv[1:]],
            check=False,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
        return
    setup()


HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "flit.buildapi": handle_flit,
    "flit_core.buildapi": handle_flit,
    "flit_core.build_thyself": handle_flit_thyself,
    "poetry.masonry.api": handle_poetry,
    "poetry.core.masonry.api": handle_poetry,
    "setuptools.build_meta": handle_setuptools,
    "setuptools.build_meta:__legacy__": handle_setuptools,
}


def main() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    if not pyproject_path.exists():
        msg = f"Could not find {pyproject_path}"
        raise FileNotFoundError(msg)
    with pyproject_path.open("rb") as file:
        data = tomllib.load(file)
    backend = data["build-system"]["build-backend"]
    try:
        handler = HANDLERS[backend]
    except KeyError as error:
        msg = f"Build backend {backend!r} is unknown"
        raise NotImplementedError(msg) from error
    handler(data)


if __name__ == "__main__":
    main()
