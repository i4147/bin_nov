#!/data/data/com.termux/files/usr/bin/env python
"""Render a ``setup.py`` from ``pyproject.toml`` for the detected build backend.
Reads the PEP 621 metadata plus the backend-specific tables (flit, hatchling, maturin, meson, pdm, poetry, scikit-build, setuptools), normalises the dependencies, authors, README and license and writes a ``setup.py`` that reproduces the project without ``pyproject.toml``.
Usage: new2old Examples: $ new2old Runs as a script (``python new2old.py``) in the directory it should act on; the module can also be imported."""

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable


if sys.version_info < (3, 11):
    import tomli as tomllib
else:
    import tomllib
PYPROJECT_PATH = Path("pyproject.toml")
SETUP_PATH = Path("setup.py")


@dataclass
class ProjectInfo:
    config: dict[str, Any]
    project: dict[str, Any]
    name: str
    version_static: str
    description: str
    author: str
    author_email: str
    readme_path: str
    license_str: str
    python_requires: str
    classifiers: list[str]
    install_requires: list[str]
    extras_require: dict[str, list[str]]
    dynamic_fields: list[str]
    module_name: str


def _split_authors(authors: list[Any]) -> tuple[str, str]:
    names: list[str] = []
    emails: list[str] = []
    for author in authors:
        if isinstance(author, dict):
            if "name" in author:
                names.append(author["name"])
            if "email" in author:
                emails.append(author["email"])
        elif isinstance(author, str):
            if "<" in author and ">" in author:
                n, e = author.split("<", 1)
                names.append(n.strip())
                emails.append(e.replace(">", "").strip())
            else:
                names.append(author.strip())
    return ", ".join(names), ", ".join(emails)


def _clean_poetry_deps(deps: dict[str, Any]) -> list[str]:
    reqs: list[str] = []
    for pkg, constraint in deps.items():
        if pkg.lower() == "python":
            continue
        if isinstance(constraint, dict):
            if constraint.get("optional", False):
                continue
            version_str: str = constraint.get("version", "")
        else:
            version_str = constraint
        if version_str.startswith("^"):
            ver = version_str[1:]
            parts = ver.split(".")
            if parts[0] == "0":
                next_minor = int(parts[1]) + 1 if len(parts) > 1 else 1
                version_str = f">={ver}, <0.{next_minor}.0"
            else:
                next_major = int(parts[0]) + 1
                version_str = f">={ver}, <{next_major}.0.0"
        elif version_str.startswith("~"):
            ver = version_str[1:]
            parts = ver.split(".")
            next_minor = int(parts[1]) + 1 if len(parts) > 1 else 1
            version_str = f">={ver}, <{parts[0]}.{next_minor}.0"
        reqs.append(f"{pkg} {version_str}".strip())
    return reqs


def _resolve_readme(project: dict[str, Any], fallback: str = "") -> str:
    readme = project.get("readme", fallback)
    if isinstance(readme, dict):
        return readme.get("file", "")
    return readme or ""


def _resolve_license(project: dict[str, Any], fallback: str = "") -> str:
    lic = project.get("license", fallback)
    if isinstance(lic, dict):
        return lic.get("text", "")
    return lic or ""


def load_project_info() -> ProjectInfo:
    if not PYPROJECT_PATH.is_file():
        print("Error: No pyproject.toml found in this directory.")
        sys.exit(1)
    with PYPROJECT_PATH.open("rb") as f:
        config: dict[str, Any] = tomllib.load(f)
    project: dict[str, Any] = config.get("project", {})
    poetry_tool: dict[str, Any] = config.get("tool", {}).get("poetry", {})
    name = project.get("name") or poetry_tool.get("name", "")
    if not name:
        print("Error: Project name is missing from pyproject.toml.")
        sys.exit(1)
    authors = project.get("authors") or project.get("maintainers") or poetry_tool.get("authors") or []
    author, author_email = _split_authors(authors)
    version_static = project.get("version") or poetry_tool.get("version", "0.1.0")
    description = project.get("description") or poetry_tool.get("description", "")
    readme = project.get("readme") or poetry_tool.get("readme", "")
    readme_path = readme.get("file", "") if isinstance(readme, dict) else (readme or "")
    license_str = _resolve_license(project, poetry_tool.get("license", ""))
    python_requires = project.get("requires-python", "")
    install_requires = list(project.get("dependencies", []))
    extras_require = dict(project.get("optional-dependencies", {}))
    poetry_deps = poetry_tool.get("dependencies", {})
    if not install_requires and poetry_deps:
        if "python" in poetry_deps:
            python_requires = poetry_deps["python"]
        install_requires = _clean_poetry_deps(poetry_deps)
    if not extras_require and "extras" in poetry_tool:
        extras_require = {g: list(items) for g, items in poetry_tool["extras"].items()}
    classifiers = list(project.get("classifiers", []) or poetry_tool.get("classifiers", []))
    return ProjectInfo(
        config=config,
        project=project,
        name=name,
        version_static=version_static,
        description=description,
        author=author,
        author_email=author_email,
        readme_path=readme_path,
        license_str=license_str,
        python_requires=python_requires,
        classifiers=classifiers,
        install_requires=install_requires,
        extras_require=extras_require,
        dynamic_fields=list(project.get("dynamic", [])),
        module_name=name.replace("-", "_"),
    )


def _render_setup(
    info: ProjectInfo,
    *,
    version_init: str,
    extraction_body: str,
    prelude: str,
    setup_kwargs: str,
) -> str:
    return f"""import ast
import re
import subprocess
import sys
from pathlib import Path
if sys.version_info < (3, 11):
    import tomli as tomllib
else:
    import tomllib
from setuptools import setup, find_packages
def read_readme(path):
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8")
    return ""
version = {version_init!r}
description = {info.description!r}
def extract_metadata():
    global version, description
{extraction_body}
extract_metadata()
{prelude}
setup(
    name={info.name!r},
    version=version,
    author={info.author!r},
    author_email={info.author_email!r},
    description=description,
    long_description=read_readme({info.readme_path!r}),
    long_description_content_type="text/markdown" if str({info.readme_path!r}).endswith(".md") else "text/x-rst",
    license={info.license_str!r},
    python_requires={info.python_requires!r},
    classifiers={info.classifiers!r},
    install_requires={info.install_requires!r},
    extras_require={info.extras_require!r},
{setup_kwargs}    zip_safe=False,
)
"""


def build_flit(info: ProjectInfo) -> str:
    import_name = info.config.get("tool", {}).get("flit", {}).get("module", {}).get("name", info.name)
    extraction = f"""    candidates = [Path({import_name!r} + ".py"), Path({import_name!r}) / "__init__.py"]
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        return
    content = target.read_text(encoding="utf-8")
    if "description" in {info.dynamic_fields!r}:
        try:
            doc = ast.get_docstring(ast.parse(content))
            if doc:
                description = doc.split("\\n")[0].strip()
        except Exception:
            pass
    if "version" in {info.dynamic_fields!r}:
        try:
            for line in content.splitlines():
                if line.startswith("__version__"):
                    version = ast.literal_eval(line.split("=")[1].strip())
                    break
        except Exception:
            pass"""
    setup_kwargs = f"    packages=find_packages(),\n    py_modules=[{import_name!r}],\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude="",
        setup_kwargs=setup_kwargs,
    )


def build_hatch(info: ProjectInfo) -> str:
    config = info.config
    hatch_cfg = config.get("tool", {}).get("hatch", {})
    wheel_cfg = hatch_cfg.get("build", {}).get("targets", {}).get("wheel", {})
    custom_packages = list(wheel_cfg.get("packages", []))
    custom_includes = hatch_cfg.get("build", {}).get("include", [])
    py_modules = [p.replace(".py", "") for p in custom_includes if p.endswith(".py")]
    version_path = hatch_cfg.get("version", {}).get("path", "")
    uses_vcs = any("hatch-vcs" in r for r in config.get("build-system", {}).get("requires", []))
    module = info.module_name
    search_paths = (
        [version_path]
        if version_path
        else [
            f"{module}/__about__.py",
            f"{module}/__init__.py",
            f"src/{module}/__about__.py",
            f"src/{module}/__init__.py",
        ]
    )
    vcs_block = ""
    if uses_vcs:
        vcs_block = """    try:
        git_version = subprocess.check_output(["git", "describe", "--tags", "--always"]).decode("utf-8").strip()
        if git_version:
            version = git_version
            return
    except Exception:
        pass
"""
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
{vcs_block}    for p in {search_paths!r}:
        path = Path(p)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("__version__"):
                    try:
                        version = ast.literal_eval(line.split("=")[1].strip())
                        return
                    except Exception:
                        pass"""
    if custom_packages:
        prelude = f"packages = {custom_packages!r}\npackage_dir = {{}}"
    else:
        prelude = (
            'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
            'package_dir = {"": "src"} if Path("src").is_dir() else {}'
        )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    if py_modules:
        setup_kwargs += f"    py_modules={py_modules!r},\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


def build_maturin(info: ProjectInfo) -> str:
    maturin_tool = info.config.get("tool", {}).get("maturin", {})
    python_source = maturin_tool.get("python-source", "")
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
    cargo = Path("Cargo.toml")
    if cargo.is_file():
        try:
            data = tomllib.loads(cargo.read_text(encoding="utf-8"))
            cargo_version = data.get("package", {{}}).get("version", "")
            if cargo_version:
                version = cargo_version
                return
        except Exception:
            pass"""
    if python_source:
        prelude = f'packages = find_packages(where={python_source!r})\npackage_dir = {{"": {python_source!r}}}'
    else:
        prelude = (
            'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
            'package_dir = {"": "src"} if Path("src").is_dir() else {}'
        )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


def build_meson(info: ProjectInfo) -> str:
    meson_meta = info.config.get("tool", {}).get("meson-python", {}).get("metadata", {})
    version_cfg = meson_meta.get("version", {})
    version_provider = version_cfg.get("provider", "") if isinstance(version_cfg, dict) else ""
    vcs_block = ""
    if version_provider and "DiscoverVersion" in version_provider:
        vcs_block = """    try:
        git_version = subprocess.check_output(["git", "describe", "--tags", "--always"]).decode("utf-8").strip()
        if git_version:
            version = git_version
            return
    except Exception:
        pass
"""
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
    meson_file = Path("meson.build")
    if meson_file.is_file():
        try:
            content = meson_file.read_text(encoding="utf-8")
            match = re.search(r"project\\s*\\([^)]*version\\s*:\\s*['\\"]([^'\\"]+)['\\"]", content, re.IGNORECASE)
            if match:
                version = match.group(1)
                return
        except Exception:
            pass
{vcs_block}"""
    prelude = (
        'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
        'package_dir = {"": "src"} if Path("src").is_dir() else {}'
    )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


def build_setuptools(info: ProjectInfo) -> str:
    st_tool = info.config.get("tool", {}).get("setuptools", {})
    st_dynamic = st_tool.get("dynamic", {})
    version_attr = ""
    if "version" in info.dynamic_fields and isinstance(st_dynamic.get("version"), dict):
        version_attr = st_dynamic["version"].get("attr", "")
    find_cfg = st_tool.get("packages", {}).get("find", {})
    where_list = find_cfg.get("where") or ["." if not Path("src").is_dir() else "src"]
    find_where = where_list[0] if where_list else "."
    find_include = list(find_cfg.get("include", ["*"]))
    find_exclude = list(find_cfg.get("exclude", []))
    module = info.module_name
    search_paths = [f"{module}/__init__.py", f"src/{module}/__init__.py"]
    attr_block = ""
    if version_attr:
        attr_block = f"""    attr_path = {version_attr!r}
    if attr_path:
        try:
            sys.path.insert(0, str(Path.cwd()))
            mod_name, obj_name = attr_path.rsplit(".", 1)
            if Path("src").is_dir():
                sys.path.insert(0, str(Path.cwd() / "src"))
            import importlib
            mod = importlib.import_module(mod_name)
            version = getattr(mod, obj_name)
            return
        except Exception:
            pass
"""
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
{attr_block}    for p in {search_paths!r}:
        path = Path(p)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("__version__"):
                    try:
                        version = ast.literal_eval(line.split("=")[1].strip())
                        return
                    except Exception:
                        pass"""
    setup_kwargs = (
        f"    packages=find_packages(where={find_where!r}, include={find_include!r}, exclude={find_exclude!r}),\n"
    )
    if find_where != ".":
        setup_kwargs += f'    package_dir={{"": {find_where!r}}},\n'
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude="",
        setup_kwargs=setup_kwargs,
    )


def build_pdm(info: ProjectInfo) -> str:
    pdm_tool = info.config.get("tool", {}).get("pdm", {})
    pdm_build = pdm_tool.get("build", {})
    pdm_version_cfg = pdm_tool.get("version", {})
    package_dir_src = pdm_build.get("package-dir", "")
    custom_includes = pdm_build.get("includes", [])
    py_modules = [m.replace(".py", "") for m in custom_includes if m.endswith(".py")]
    version_source = pdm_version_cfg.get("source", "file" if "version" in info.dynamic_fields else "static")
    version_path = pdm_version_cfg.get("path", "")
    module = info.module_name
    search_paths = (
        [version_path]
        if version_path
        else [
            f"{module}/__init__.py",
            f"src/{module}/__init__.py",
        ]
    )
    scm_block = ""
    if version_source == "scm":
        scm_block = """    try:
        git_version = subprocess.check_output(["git", "describe", "--tags", "--always"]).decode("utf-8").strip()
        if git_version:
            version = git_version
            return
    except Exception:
        pass
"""
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
{scm_block}    for p in {search_paths!r}:
        if not p:
            continue
        path = Path(p)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("__version__") or stripped.startswith("VERSION"):
                    try:
                        version = ast.literal_eval(line.split("=")[1].strip())
                        return
                    except Exception:
                        pass"""
    if package_dir_src:
        prelude = f'packages = find_packages(where={package_dir_src!r})\npackage_dir = {{"": {package_dir_src!r}}}'
    else:
        prelude = (
            'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
            'package_dir = {"": "src"} if Path("src").is_dir() else {}'
        )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    if py_modules:
        setup_kwargs += f"    py_modules={py_modules!r},\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


def build_poetry(info: ProjectInfo) -> str:
    uses_dynamic = "poetry-dynamic-versioning" in info.config.get("tool", {})
    if uses_dynamic or "version" in info.dynamic_fields:
        extraction = """    try:
        git_version = subprocess.check_output(["git", "describe", "--tags", "--always"]).decode("utf-8").strip()
        if git_version:
            version = git_version
            return
    except Exception:
        pass"""
    else:
        extraction = "    pass"
    prelude = (
        'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
        'package_dir = {"": "src"} if Path("src").is_dir() else {}'
    )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


def build_skbuild(info: ProjectInfo) -> str:
    config = info.config
    version_regex_file = ""
    version_regex_pattern = ""
    version_is_scm = False
    dynamic_meta = config.get("tool", {}).get("dynamic-metadata", [])
    if isinstance(dynamic_meta, list):
        for table in dynamic_meta:
            if table.get("field") == "version":
                provider = table.get("provider", "")
                if "regex" in provider:
                    version_regex_file = table.get("input", "")
                    version_regex_pattern = table.get("regex", "")
                elif "setuptools_scm" in provider:
                    version_is_scm = True
    if not version_regex_file and not version_is_scm:
        legacy = config.get("tool", {}).get("scikit-build", {}).get("metadata", {}).get("version", {})
        provider = legacy.get("provider", "")
        if "regex" in provider:
            version_regex_file = legacy.get("input", "")
            version_regex_pattern = legacy.get("regex", "")
        elif "setuptools_scm" in provider:
            version_is_scm = True
    scm_block = ""
    if version_is_scm:
        scm_block = """    try:
        git_version = subprocess.check_output(["git", "describe", "--tags", "--always"]).decode("utf-8").strip()
        if git_version:
            version = git_version
            return
    except Exception:
        pass
"""
    extraction = f"""    if "version" not in {info.dynamic_fields!r}:
        return
{scm_block}    regex_file = {version_regex_file!r}
    if regex_file and Path(regex_file).is_file():
        pattern_str = {version_regex_pattern!r} or r'__version__\\s*=\\s*["\\']([^"\\']+)["\\']'
        try:
            content = Path(regex_file).read_text(encoding="utf-8")
            match = re.search(pattern_str, content)
            if match:
                version = match.group(1)
                return
        except Exception:
            pass"""
    prelude = (
        'packages = find_packages(where="src") if Path("src").is_dir() else find_packages()\n'
        'package_dir = {"": "src"} if Path("src").is_dir() else {}'
    )
    setup_kwargs = "    packages=packages,\n    package_dir=package_dir,\n"
    return _render_setup(
        info,
        version_init=info.version_static,
        extraction_body=extraction,
        prelude=prelude,
        setup_kwargs=setup_kwargs,
    )


BACKENDS: dict[str, Callable[[ProjectInfo], str]] = {
    "flit_core": build_flit,
    "hatchling": build_hatch,
    "maturin": build_maturin,
    "mesonpy": build_meson,
    "setuptools": build_setuptools,
    "pdm.backend": build_pdm,
    "poetry.core": build_poetry,
    "scikit_build_core": build_skbuild,
}


def _detect_backend(config: dict[str, Any]) -> str:
    return config.get("build-system", {}).get("build-backend", "")


def _find_builder(backend: str) -> Callable[[ProjectInfo], str] | None:
    for key, builder in BACKENDS.items():
        if key in backend:
            return builder
    return None


def main() -> None:
    info = load_project_info()
    backend = _detect_backend(info.config)
    if not backend:
        print("Error: No [build-system].build-backend found in pyproject.toml.")
        sys.exit(1)
    builder = _find_builder(backend)
    if builder is None:
        print(f"Error: Unsupported build backend: '{backend}'.")
        print(f"Supported: {', '.join(BACKENDS.keys())}")
        sys.exit(1)
    source = builder(info)
    SETUP_PATH.write_text(source, encoding="utf-8")
    print(f"Successfully generated setup.py for project: '{info.name}' (backend: {backend})")


if __name__ == "__main__":
    main()
