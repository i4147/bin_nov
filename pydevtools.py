#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Iterable, NoReturn

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


README_SUFFIXES: set[str] = {".md", ".markdown", ".mdown", ".mkdn"}
BINARY_SUFFIXES: tuple[str, str, str] = (".so", ".pyd", ".dll")

console = Console()
err_console = Console(stderr=True)


class InitStyle(StrEnum):
    setuptools_src = "setuptools-src"
    hatchling_typer = "hatchling-typer"
    setuptools_cfg = "setuptools-cfg"


class SetupStyle(StrEnum):
    simple = "simple"
    detailed = "detailed"
    runtime = "runtime"


app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
setup_app = typer.Typer(no_args_is_help=True, help="Generate setup.py from various sources")
cargo_app = typer.Typer(no_args_is_help=True, help="Cargo.lock utilities")
app.add_typer(setup_app, name="setup")
app.add_typer(cargo_app, name="cargo")


def _init_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        colorize=True,
    )


def _fail(msg: str, code: int = 1) -> NoReturn:
    logger.error(msg)
    raise typer.Exit(code)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _write_text(path: Path, content: str, *, force: bool = False) -> bool:
    if path.exists() and not force:
        logger.warning("{} exists; use --force to overwrite", path)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        _fail("TOML support requires Python >=3.11 or the 'tomli' package.")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        _fail(f"file not found: {path}")
    except Exception as exc:
        _fail(f"invalid TOML in {path}: {exc}")


def _module_from_name(name: str) -> str:
    return name.replace("-", "_").replace(".", "_").lower()


def _load_user_info() -> dict[str, str]:
    path = Path.home() / ".myinfo"
    if not path.exists():
        return {}
    text = _read_text(path)
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        pass
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@dataclass(slots=True)
class Author:
    name: str | None = None
    email: str | None = None

    def to_string(self) -> str:
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.name or self.email or ""


@dataclass(slots=True)
class ProjectMeta:
    name: str = ""
    version: str = "0.0.0"
    description: str = ""
    readme_content: str = ""
    readme_content_type: str = "text/plain"
    readme_file: str | None = None
    requires_python: str = ""
    license_text: str = ""
    license_file: str | None = None
    authors: list[Author] = field(default_factory=list)
    maintainers: list[Author] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    classifiers: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)
    gui_scripts: dict[str, str] = field(default_factory=dict)
    entry_points: dict[str, dict[str, str]] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    optional_dependencies: dict[str, list[str]] = field(default_factory=dict)
    build_backend: str = ""
    build_requires: list[str] = field(default_factory=list)
    tool: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InitConfig:
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str | None = None
    email: str | None = None
    url: str | None = None
    requires_python: str = ">=3.11"
    simple_cli: bool = False


def _authors_from_pep621(raw: Iterable[Any]) -> list[Author]:
    out: list[Author] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(Author(name=item.get("name"), email=item.get("email")))
        elif isinstance(item, str):
            m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", item)
            if m:
                out.append(Author(name=m.group(1), email=m.group(2)))
            else:
                out.append(Author(name=item))
    return out


def _readme_content(readme: Any, root: Path) -> tuple[str, str]:
    if isinstance(readme, dict):
        content_type = readme.get("content-type", "text/markdown")
        if "file" in readme:
            return _read_text(root / readme["file"]), content_type
        return readme.get("text", ""), content_type
    if isinstance(readme, str):
        suffix = Path(readme).suffix.lower()
        ctype = "text/markdown" if suffix in README_SUFFIXES else ("text/x-rst" if suffix == ".rst" else "text/plain")
        return _read_text(root / readme), ctype
    return "", "text/plain"


def _extract_pep621_meta(pyproject: dict[str, Any], root: Path) -> ProjectMeta:
    v1 = pyproject.get("project", {})
    bs = pyproject.get("build-system", {})
    tool = pyproject.get("tool", {}) or {}
    if not isinstance(v1, dict):
        _fail("[project] must be a table")
    readme_content, readme_ctype = _readme_content(v1.get("readme", ""), root)
    readme_file = (
        v1.get("readme", {}).get("file")
        if isinstance(v1.get("readme"), dict)
        else (v1.get("readme") if isinstance(v1.get("readme"), str) else None)
    )
    license_field = v1.get("license", "")
    license_text = ""
    license_file: str | None = None
    if isinstance(license_field, dict):
        if "text" in license_field:
            license_text = license_field["text"]
        if "file" in license_field:
            license_file = license_field["file"]
            if not license_text:
                license_text = _read_text(root / license_field["file"])
    elif isinstance(license_field, str):
        license_text = license_field
    entry_points: dict[str, dict[str, str]] = {}
    for sec, val in (v1.get("entry-points") or {}).items():
        if isinstance(val, dict):
            entry_points[sec] = dict(val)
    return ProjectMeta(
        name=str(v1.get("name", "")),
        version=str(v1.get("version", "0.0.0")),
        description=str(v1.get("description", "")),
        readme_content=readme_content,
        readme_content_type=readme_ctype,
        readme_file=readme_file,
        requires_python=str(v1.get("requires-python", "")),
        license_text=license_text,
        license_file=license_file,
        authors=_authors_from_pep621(v1.get("authors", []) or []),
        maintainers=_authors_from_pep621(v1.get("maintainers", []) or []),
        keywords=list(v1.get("keywords", []) or []),
        classifiers=list(v1.get("classifiers", []) or []),
        urls=dict(v1.get("urls", {}) or {}),
        scripts=dict(v1.get("scripts", {}) or {}),
        gui_scripts=dict(v1.get("gui-scripts", {}) or {}),
        entry_points=entry_points,
        dependencies=list(v1.get("dependencies", []) or []),
        optional_dependencies={k: list(v) for k, v in (v1.get("optional-dependencies") or {}).items()},
        build_backend=str(bs.get("build-backend", "")),
        build_requires=list(bs.get("requires", []) or []),
        tool=tool,
    )


def _extract_poetry_meta(pyproject: dict[str, Any], root: Path) -> ProjectMeta:
    poetry = pyproject.get("tool", {}).get("poetry", {})
    if not isinstance(poetry, dict):
        _fail("[tool.poetry] missing or malformed")
    authors = _authors_from_pep621(poetry.get("authors", []) or [])
    deps_dict = poetry.get("dependencies", {}) or {}
    deps: list[str] = []
    for name, spec in deps_dict.items():
        if name == "python":
            continue
        if isinstance(spec, str):
            deps.append(name if spec == "*" else f"{name}{spec}")
        elif isinstance(spec, dict):
            version = spec.get("version", "")
            extras = spec.get("extras", [])
            token = name
            if extras:
                token += f"[{','.join(extras)}]"
            if version and version != "*":
                token += version
            deps.append(token)
    readme = poetry.get("readme", "")
    readme_content, readme_ctype = _readme_content(readme, root) if readme else ("", "text/plain")
    return ProjectMeta(
        name=str(poetry.get("name", "")),
        version=str(poetry.get("version", "0.0.0")),
        description=str(poetry.get("description", "")),
        readme_content=readme_content,
        readme_content_type=readme_ctype,
        readme_file=readme if isinstance(readme, str) else None,
        requires_python=str(poetry.get("dependencies", {}).get("python", "")),
        license_text=str(poetry.get("license", "")),
        authors=authors,
        keywords=list(poetry.get("keywords", []) or []),
        classifiers=list(poetry.get("classifiers", []) or []),
        urls={"Homepage": poetry["homepage"]} if poetry.get("homepage") else {},
        scripts=dict(poetry.get("scripts", {}) or {}),
        dependencies=deps,
        build_backend="poetry.core.masonry.api",
        build_requires=["poetry-core"],
        tool=pyproject.get("tool", {}) or {},
    )


def _parse_setup_cfg(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    if path.is_file():
        try:
            cfg.read(path, encoding="utf-8")
        except configparser.Error as exc:
            _fail(f"invalid setup.cfg: {exc}")
    return cfg


def _parse_cargo_lock(text: str) -> dict[str, Any]:
    m = re.search(r"^version\s*=\s*(\d+)", text, re.MULTILINE)
    version = int(m.group(1)) if m else 3
    packages: list[dict[str, Any]] = []
    chunks = re.split(r"\n\[\[package\]\]\n", text)
    for chunk in chunks[1:]:
        pkg = _parse_lock_pkg_v2(chunk) if version >= 2 else _parse_lock_pkg_v1(chunk)
        if pkg:
            packages.append(pkg)
    return {"version": version, "packages": packages}


def _parse_lock_pkg_v2(chunk: str) -> dict[str, Any] | None:
    name = re.search(r'^name\s*=\s*"([^"]*)"', chunk, re.MULTILINE)
    version = re.search(r'^version\s*=\s*"([^"]*)"', chunk, re.MULTILINE)
    source = re.search(r'^source\s*=\s*"([^"]*)"', chunk, re.MULTILINE)
    if not name or not version:
        return None
    pkg: dict[str, Any] = {"name": name.group(1), "version": version.group(1)}
    if source:
        pkg["source"] = source.group(1)
    deps: list[str] = []
    in_deps = False
    for line in chunk.split("\n"):
        if line.strip().startswith("dependencies = ["):
            in_deps = True
            deps.extend(re.findall(r'"([^"]*)"', line))
            if "]" in line:
                in_deps = False
        elif in_deps:
            deps.extend(re.findall(r'"([^"]*)"', line))
            if "]" in line:
                in_deps = False
    if deps:
        pkg["dependencies"] = deps
    return pkg


def _parse_lock_pkg_v1(chunk: str) -> dict[str, Any] | None:
    name = re.search(r'^name\s*=\s*"([^"]*)"', chunk, re.MULTILINE)
    version = re.search(r'^version\s*=\s*"([^"]*)"', chunk, re.MULTILINE)
    if not name or not version:
        return None
    pkg: dict[str, Any] = {"name": name.group(1), "version": version.group(1)}
    deps: list[str] = []
    for line in chunk.split("\n"):
        m = re.match(r'^\s*"([^"]+)\s+([^"]+)"', line)
        if m:
            deps.append(f"{m.group(1)} {m.group(2)}")
    if deps:
        pkg["dependencies"] = deps
    return pkg


def _render_cargo_toml(
    packages: list[dict[str, Any]],
    *,
    root_name: str | None = None,
    root_version: str = "0.1.0",
) -> str:
    out: list[str] = ["[package]"]
    if root_name:
        out.append(f'name = "{root_name}"')
    elif packages:
        out.append(f'name = "{packages[0]["name"]}"')
    else:
        out.append('name = "generated-project"')
    out.append(f'version = "{root_version}"')
    out.append('edition = "2021"')
    out.append("")
    if packages:
        out.append("[dependencies]")
        seen: set[str] = set()
        if "dependencies" in packages[0]:
            seen.update(packages[0]["dependencies"])
        if seen:
            for dep in sorted(seen):
                match = next((p for p in packages if p["name"] == dep), None)
                if match:
                    out.append(f'{match["name"]} = "{match["version"]}"')
                else:
                    out.append(f'{dep} = "*"')
        else:
            for pkg in packages[1:]:
                out.append(f'{pkg["name"]} = "{pkg["version"]}"')
    return "\n".join(out)


def _render_package_finder(package_name: str | None) -> str:
    if package_name:
        return (
            "from pathlib import Path\n"
            "from setuptools import find_packages\n"
            "_project_root = Path(__file__).parent\n"
            f"_package_name = {package_name!r}\n"
            "if (_project_root / _package_name).is_dir():\n"
            "    packages = find_packages(\n"
            "        where=str(_project_root),\n"
            '        include=(_package_name, f"{_package_name}.*"),\n'
            "    )\n"
            "    package_dir = {}\n"
            'elif (_project_root / "src" / _package_name).is_dir():\n'
            "    packages = find_packages(\n"
            '        where=str(_project_root / "src"),\n'
            '        include=(_package_name, f"{_package_name}.*"),\n'
            "    )\n"
            '    package_dir = {"": "src"}\n'
            "else:\n"
            "    packages = []\n"
            "    package_dir = {}\n"
        )
    return (
        "from pathlib import Path\n"
        "from setuptools import find_packages\n"
        "_project_root = Path(__file__).parent\n"
        'if (_project_root / "src").is_dir():\n'
        '    packages = find_packages(where=str(_project_root / "src"))\n'
        '    package_dir = {"": "src"}\n'
        "else:\n"
        "    packages = find_packages(where=str(_project_root))\n"
        "    package_dir = {}\n"
    )


def _setup_kwargs_simple(meta: ProjectMeta) -> str:
    lines: list[str] = []
    lines.append(f"    name={meta.name!r},")
    lines.append(f"    version={meta.version!r},")
    if meta.description:
        lines.append(f"    description={meta.description!r},")
    a_names = ", ".join(a.name for a in meta.authors if a.name)
    a_mails = ", ".join(a.email for a in meta.authors if a.email)
    if a_names:
        lines.append(f"    author={a_names!r},")
    if a_mails:
        lines.append(f"    author_email={a_mails!r},")
    if meta.readme_file:
        lines.append(f"    long_description=read_text({meta.readme_file!r}),")
        lines.append(f"    long_description_content_type={meta.readme_content_type!r},")
    elif meta.readme_content:
        lines.append(f"    long_description={meta.readme_content!r},")
        lines.append(f"    long_description_content_type={meta.readme_content_type!r},")
    if meta.license_text:
        lines.append(f"    license={meta.license_text!r},")
    if meta.requires_python:
        lines.append(f"    python_requires={meta.requires_python!r},")
    if meta.keywords:
        lines.append(f"    keywords={meta.keywords!r},")
    if meta.classifiers:
        lines.append(f"    classifiers={meta.classifiers!r},")
    if meta.urls:
        lines.append(f"    project_urls={meta.urls!r},")
    if meta.dependencies:
        lines.append(f"    install_requires={meta.dependencies!r},")
    if meta.optional_dependencies:
        lines.append(f"    extras_require={meta.optional_dependencies!r},")
    entry_points: dict[str, list[str]] = {}
    if meta.scripts:
        entry_points["console_scripts"] = [f"{k} = {v}" for k, v in meta.scripts.items()]
    if meta.gui_scripts:
        entry_points["gui_scripts"] = [f"{k} = {v}" for k, v in meta.gui_scripts.items()]
    for sec, items in meta.entry_points.items():
        entry_points[sec] = [f"{k} = {v}" for k, v in items.items()]
    if entry_points:
        lines.append(f"    entry_points={entry_points!r},")
    lines.append("    packages=packages,")
    lines.append("    package_dir=package_dir,")
    return "\n".join(lines)


def _render_setup_py_simple(meta: ProjectMeta, root: Path) -> str:
    finder = _render_package_finder(meta.name)
    header = (
        '"""Generated setup.py.\n'
        f"Original build backend: {meta.build_backend}\n"
        "Generated by pydevkit.\n"
        '"""\n'
        "from setuptools import setup\n"
    )
    readme_helper = (
        "from pathlib import Path\n"
        "def read_text(p):\n"
        "    q = Path(__file__).parent / p\n"
        "    return q.read_text(encoding='utf-8') if q.is_file() else ''\n"
    )
    kwargs = _setup_kwargs_simple(meta)
    return header + readme_helper + finder + "\nsetup(\n" + kwargs + "\n)\n"


def _detect_ext_backend(meta: ProjectMeta) -> str:
    if meta.tool.get("setuptools", {}).get("ext-modules"):
        return "setuptools"
    lowered = " ".join([meta.build_backend.lower(), *(str(r).lower() for r in meta.build_requires)])
    if "scikit-build" in lowered:
        return "scikit-build"
    if "meson" in lowered:
        return "meson"
    if "cmake" in lowered:
        return "cmake"
    return "none"


def _render_setup_py_detailed(
    meta: ProjectMeta,
    root: Path,
    *,
    cfg: configparser.ConfigParser | None = None,
    with_cfg: bool = False,
) -> str:
    cfg = cfg or configparser.ConfigParser()
    backend = _detect_ext_backend(meta)
    author_names = ", ".join(a.name for a in meta.authors if a.name)
    author_mails = ", ".join(a.email for a in meta.authors if a.email)
    maint_names = ", ".join(a.name for a in meta.maintainers if a.name)
    maint_mails = ", ".join(a.email for a in meta.maintainers if a.email)
    url = next(iter(meta.urls.values()), "") if meta.urls else ""
    packages_lines: list[str] = []
    if with_cfg and cfg.has_section("options.packages.find"):
        where = cfg.get("options.packages.find", "where", fallback="").strip()
        if where:
            packages_lines.append(f"        packages=find_packages(where={where!r}),")
            packages_lines.append(f"        package_dir={{'': {where!r}}},")
        else:
            packages_lines.append("        packages=find_packages(),")
    else:
        packages_lines.append("        packages=find_packages(),")
    entry_points: dict[str, list[str]] = {}
    if meta.scripts:
        entry_points["console_scripts"] = [f"{k} = {v}" for k, v in meta.scripts.items()]
    if meta.gui_scripts:
        entry_points["gui_scripts"] = [f"{k} = {v}" for k, v in meta.gui_scripts.items()]
    for sec, items in meta.entry_points.items():
        entry_points[sec] = [f"{k} = {v}" for k, v in items.items()]
    lines: list[str] = []
    lines.append(f"        name={meta.name!r},")
    lines.append(f"        version={meta.version!r},")
    lines.append(f"        description={meta.description!r},")
    lines.append("        long_description=long_description,")
    lines.append(f"        long_description_content_type={meta.readme_content_type!r},")
    if author_names:
        lines.append(f"        author={author_names!r},")
    if author_mails:
        lines.append(f"        author_email={author_mails!r},")
    if maint_names:
        lines.append(f"        maintainer={maint_names!r},")
    if maint_mails:
        lines.append(f"        maintainer_email={maint_mails!r},")
    if meta.license_text:
        lines.append(f"        license={meta.license_text!r},")
    elif meta.license_file:
        lines.append(f"        license_files=[{meta.license_file!r}],")
    if url:
        lines.append(f"        url={url!r},")
    if meta.keywords:
        lines.append(f"        keywords={meta.keywords!r},")
    if meta.classifiers:
        lines.append(f"        classifiers={meta.classifiers!r},")
    if meta.requires_python:
        lines.append(f"        python_requires={meta.requires_python!r},")
    if meta.dependencies:
        lines.append(f"        install_requires={meta.dependencies!r},")
    if meta.optional_dependencies:
        lines.append(f"        extras_require={meta.optional_dependencies!r},")
    if entry_points:
        lines.append(f"        entry_points={entry_points!r},")
    lines.append("        include_package_data=True,")
    lines.extend(packages_lines)
    if backend == "setuptools" and meta.tool.get("setuptools", {}).get("ext-modules"):
        lines.append("        ext_modules=ext_modules,")
    readme_file_repr = repr(meta.readme_file) if meta.readme_file else "''"
    header = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """
        Auto-generated from {root.name}/pyproject.toml.
        Detected extension backend: {backend}
        """
        from pathlib import Path
        from setuptools import Extension, find_packages, setup
        ROOT = Path(__file__).resolve().parent
        README_FILE = ROOT / {readme_file_repr}
        def read_text(relative_path):
            path = ROOT / relative_path
            return path.read_text(encoding="utf-8") if path.is_file() else ""
        long_description = read_text({readme_file_repr}) if {readme_file_repr} else {meta.readme_content!r}
        ''')
    ext_block = ""
    if backend == "setuptools" and meta.tool.get("setuptools", {}).get("ext-modules"):
        exts = []
        for ext in meta.tool["setuptools"]["ext-modules"]:
            exts.append(f"Extension({ext['name']!r}, sources={ext.get('sources', [])!r})")
        ext_block = "ext_modules = [\n    " + ",\n    ".join(exts) + "\n]\n\n"
    return header + ext_block + "setup(\n" + "\n".join(lines) + "\n)\n"


def _render_setup_py_runtime(meta: ProjectMeta) -> str:
    return textwrap.dedent('''\
        #!/usr/bin/env python3
        """
        Runtime setup.py shim generated by pydevkit.
        Reads pyproject.toml and dispatches to the configured build backend.
        """
        import importlib
        import sys
        from pathlib import Path
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        ROOT = Path(__file__).resolve().parent
        def _load_config():
            with (ROOT / "pyproject.toml").open("rb") as fh:
                return tomllib.load(fh)
        def _run_setuptools(cfg):
            from setuptools import setup
            setup()
        def _run_flit(cfg):
            flit = importlib.import_module("flit_core.buildapi")
            flit.build_wheel(str(ROOT / "dist"))
        def _run_poetry(cfg):
            pc = importlib.import_module("poetry.core.masonry.api")
            pc.build_wheel(str(ROOT / "dist"))
        def main():
            cfg = _load_config()
            backend = cfg["build-system"]["build-backend"]
            dispatch = {
                "setuptools.build_meta": _run_setuptools,
                "setuptools.build_meta:__legacy__": _run_setuptools,
                "flit_core.buildapi": _run_flit,
                "flit.buildapi": _run_flit,
                "poetry.core.masonry.api": _run_poetry,
                "poetry.masonry.api": _run_poetry,
            }
            handler = dispatch.get(backend)
            if handler is None:
                print(f"Unsupported build backend: {backend}", file=sys.stderr)
                return 1
            handler(cfg)
            return 0
        if __name__ == "__main__":
            raise SystemExit(main())
        ''')


def _detect_entry_points(project_dir: Path, package_name: str) -> list[dict[str, str]]:
    entry: list[dict[str, str]] = []
    pkg_main = project_dir / package_name / "__main__.py"
    pkg_cli = project_dir / package_name / "cli.py"
    root_main = project_dir / "__main__.py"
    root_cli = project_dir / "cli.py"

    def _guess_func(path: Path) -> str:
        text = _read_text(path)
        if re.search(r"def main\(", text) or "import click" in text:
            return "main"
        if re.search(r"def cli\(", text):
            return "cli"
        return "main"

    if pkg_main.exists():
        entry.append({"module": f"{package_name}.__main__", "function": "main", "script_name": package_name})
    if pkg_cli.exists():
        entry.append({
            "module": f"{package_name}.cli",
            "function": _guess_func(pkg_cli),
            "script_name": package_name if not pkg_main.exists() else f"{package_name}-cli",
        })
    if not entry and root_main.exists():
        entry.append({"module": "__main__", "function": "main", "script_name": package_name})
    if not entry and root_cli.exists():
        entry.append({"module": "cli", "function": _guess_func(root_cli), "script_name": package_name})
    return entry


def _parse_requirements(project_dir: Path) -> list[str]:
    for name in ("requirements.txt", "requirements.in", "Pipfile"):
        p = project_dir / name
        if not p.exists():
            continue
        deps: list[str] = []
        for line in _read_text(p).splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            if name == "Pipfile":
                if "=" in line and not line.startswith("["):
                    deps.append(line.split("=", 1)[0].strip())
            else:
                deps.append(line)
        return deps
    return []


def _render_setup_py_for_dir(project_dir: Path, package_name: str) -> str:
    entry_points = _detect_entry_points(project_dir, package_name)
    requirements = _parse_requirements(project_dir)
    ep_block = ""
    if entry_points:
        ep_block = "    entry_points={\n        'console_scripts': [\n"
        for ep in entry_points:
            ep_block += f"            '{ep['script_name']}={ep['module']}:{ep['function']}',\n"
        ep_block += "        ],\n    },\n"
    req_block = "    install_requires=[],\n"
    if requirements:
        req_block = "    install_requires=[\n"
        for r in requirements:
            req_block += f"        {r!r},\n"
        req_block += "    ],\n"
    readme_block = ""
    if (project_dir / "README.md").exists():
        readme_block = (
            "    long_description=open('README.md').read(),\n    long_description_content_type='text/markdown',\n"
        )
    return (
        "from setuptools import setup, find_packages\n\n"
        "setup(\n"
        f"    name={package_name!r},\n"
        "    version='0.1.0',\n"
        f"    description={package_name + ' - A Python project'!r},\n"
        "    author='Your Name',\n"
        "    author_email='your.email@example.com',\n"
        "    url='',\n"
        "    packages=find_packages(),\n"
        f"{req_block}{readme_block}{ep_block}"
        "    python_requires='>=3.6',\n"
        "    classifiers=[\n"
        "        'Development Status :: 3 - Alpha',\n"
        "        'Intended Audience :: Developers',\n"
        "        'Programming Language :: Python :: 3',\n"
        "    ],\n"
        ")\n"
    )


def _read_wheel_metadata(root: Path) -> dict[str, Any]:
    dist_info = next(root.glob("*.dist-info"), None)
    if dist_info is None:
        _fail("no .dist-info directory found")
    meta = Parser().parsestr((dist_info / "METADATA").read_text(encoding="utf-8"))
    return {
        "name": meta["Name"],
        "version": meta["Version"],
        "summary": meta.get("Summary", ""),
        "install_requires": meta.get_all("Requires-Dist") or [],
    }


def _read_wheel_entry_points(root: Path) -> dict[str, list[str]]:
    dist_info = next(root.glob("*.dist-info"), None)
    if dist_info is None:
        return {}
    ep_file = dist_info / "entry_points.txt"
    if not ep_file.exists():
        return {}
    groups: dict[str, list[str]] = {}
    section: str | None = None
    for line in ep_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            groups[section] = []
        elif section:
            groups[section].append(line)
    return groups


def _find_extensions(root: Path) -> list[str]:
    out: list[str] = []
    for path in root.rglob("*"):
        if path.suffix in BINARY_SUFFIXES:
            dotted = ".".join(path.relative_to(root).with_suffix("").parts)
            out.append(dotted)
    return out


def _render_setup_py_from_wheel(
    meta: dict[str, Any],
    ext_modules: list[str],
    entry_points: dict[str, list[str]],
) -> str:
    if ext_modules:
        lines = ",\n".join(f"    Extension({m!r}, sources=[{m.replace('.', '/')!r} + '.*'])" for m in ext_modules)
        ext_block = f"from setuptools import Extension\n\next_modules = [\n{lines}\n]\n"
    else:
        ext_block = "ext_modules = []\n"
    ep_block = ""
    if entry_points:
        rendered = "{\n"
        for group, items in entry_points.items():
            rendered += f"        {group!r}: [\n"
            for item in items:
                rendered += f"            {item!r},\n"
            rendered += "        ],\n"
        rendered += "    }"
        ep_block = f"    entry_points={rendered},\n"
    return (
        "from setuptools import setup, find_packages\n"
        f"{ext_block}\n"
        "setup(\n"
        f"    name={meta['name']!r},\n"
        f"    version={meta['version']!r},\n"
        f"    description={meta['summary']!r},\n"
        "    packages=find_packages() or ['.'],\n"
        f"    install_requires={meta['install_requires']!r},\n"
        "    ext_modules=ext_modules,\n"
        f"{ep_block}"
        ")\n"
    )


GITIGNORE_DEFAULT: str = (
    "__pycache__/\n*.py[cod]\n*.egg-info/\ndist/\nbuild/\n.venv/\nvenv/\nenv/\n"
    ".mypy_cache/\n.ruff_cache/\n.pytest_cache/\n.coverage\n"
)

GITIGNORE_DEV: str = textwrap.dedent("""\
    __pycache__/
    *.py[cod]
    *$py.class
    *.so
    .Python
    build/
    develop-eggs/
    dist/
    downloads/
    eggs/
    .eggs/
    lib/
    lib64/
    parts/
    sdist/
    var/
    wheels/
    *.egg-info/
    .installed.cfg
    *.egg
    *.manifest
    *.spec
    pip-log.txt
    pip-delete-this-directory.txt
    htmlcov/
    .tox/
    .nox/
    .coverage
    .coverage.*
    .cache
    nosetests.xml
    coverage.xml
    *.cover
    *.py,cover
    .hypothesis/
    .pytest_cache/
    *.mo
    *.pot
    .env
    .venv
    env/
    venv/
    ENV/
    env.bak/
    venv.bak/
    .idea/
    .vscode/
    *.swp
    *.swo
    *~
    .DS_Store
    Thumbs.db
    *.log
    logs/
    .env.local
    .env.*.local
    """)

PYPROJECT_SETUPTOOLS_SRC: str = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "{version}"
{description_line}authors = [{{name = "{author}"{email_part}}}]
requires-python = "{requires_python}"
dependencies = []
{urls_block}
[tool.setuptools.packages.find]
where = ["src"]
{scripts_block}"""

PYPROJECT_HATCHLING_TYPER: str = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
dynamic = ["version"]
description = "A standard library and CLI tool built with Typer."
readme = "README.md"
requires-python = ">=3.10"
authors = [
    {{ name = "{author}", email = "{email}" }}
]
classifiers = [
    "Programming Language :: Python :: 3",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
]
dependencies = [
    "typer>=0.12.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "black>=24.0.0",
    "flake8>=7.0.0",
]

[project.scripts]
{name} = "{module}.cli:app"
{name}-admin = "{module}.admin:app"

[tool.hatch.version]
path = "src/{module}/__init__.py"

[tool.black]
line-length = 88
target-version = ['py310']
"""

PYPROJECT_SETUPTOOLS_CFG: str = """\
[build-system]
requires = ["setuptools>=69.0", "wheel"]
build-backend = "setuptools.build_meta"

[project.scripts]
{name} = "{name}:main"
"""

SETUP_CFG_TEMPLATE: str = """\
[metadata]
name = {name}
version = {version}
{author_lines}

[options]
py_modules = {name}
python_requires = >=3.11

[options.entry_points]
console_scripts =
    {name} = {name}:main
"""

PRE_COMMIT_CONFIG: str = textwrap.dedent("""\
    repos:
      - repo: https://github.com/pre-commit/pre-commit-hooks
        rev: v4.5.0
        hooks:
          - id: trailing-whitespace
          - id: end-of-file-fixer
          - id: check-yaml
          - id: check-added-large-files
          - id: check-merge-conflict
          - id: debug-statements
          - id: check-ast
      - repo: https://github.com/astral-sh/ruff-pre-commit
        rev: v0.3.7
        hooks:
          - id: ruff
            args: [--fix, --exit-non-zero-on-fix]
      - repo: https://github.com/psf/black-pre-commit
        rev: 24.2.0
        hooks:
          - id: black
      - repo: https://github.com/pycqa/isort
        rev: 5.13.2
        hooks:
          - id: isort
            args: ["--profile", "black"]
    """)

DEV_PACKAGES: list[str] = [
    "ruff",
    "black",
    "isort",
    "mypy",
    "pytest",
    "pytest-cov",
    "pre-commit",
]

DEV_PYPROJECT: str = """\
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "0.1.0"
description = "Python project"
readme = "README.md"
requires-python = ">=3.11"
license = {{text = "MIT"}}
authors = [
    {{name = "Your Name", email = "your.email@example.com"}}
]

[project.optional-dependencies]
dev = [
    "pyright",
    "black",
    "isort",
    "mypy",
    "debugpy",
    "pytest",
    "pytest-cov",
    "pre-commit",
    "pynvim",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 120
target-version = "py312"
select = ["E", "F", "I", "N", "W", "UP"]
ignore = ["E501"]

[tool.ruff.isort]
profile = "black"

[tool.black]
line-length = 120
target-version = ["py312"]

[tool.isort]
profile = "black"
line_length = 120

[tool.mypy]
python_version = "3.12"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_functions = ["test_*"]
addopts = "-v --tb=short"
"""

NEW_SCRIPT_TEMPLATE: str = '''#!/usr/bin/env python3
"""Standalone script scaffold."""
from pathlib import Path
import sys


def process_file(path: Path) -> None:
    pass


def collect_files(args: list[str]) -> list[Path]:
    if not args:
        return list(Path.cwd().rglob("*.py"))
    out: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(p.rglob("*"))
    return out


def main() -> int:
    files = collect_files(sys.argv[1:])
    for f in files:
        process_file(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _scaffold_setuptools_src(cfg: InitConfig, target: Path) -> None:
    name = cfg.name
    module = _module_from_name(name)
    version = cfg.version
    user = _load_user_info()
    author = cfg.author or user.get("name", "Your Name")
    email = cfg.email or user.get("email", "")
    github = user.get("github_username", "")
    url = cfg.url or (f"https://github.com/{github}/{name}" if github else "")
    description_line = f'description = "{cfg.description}"\n' if cfg.description else ""
    email_part = f', email = "{email}"' if email else ""
    urls_block = f'\n[project.urls]\nHomepage = "{url}"\n' if url else ""
    scripts_block = f'\n[project.scripts]\n{name} = "{module}:main"\n' if cfg.simple_cli else ""
    (target / "pyproject.toml").write_text(
        PYPROJECT_SETUPTOOLS_SRC.format(
            name=name,
            version=version,
            description_line=description_line,
            author=author,
            email_part=email_part,
            requires_python=cfg.requires_python,
            urls_block=urls_block,
            scripts_block=scripts_block,
        ),
        encoding="utf-8",
    )
    (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (target / ".gitignore").write_text(GITIGNORE_DEFAULT, encoding="utf-8")
    (target / "LICENSE").write_text("", encoding="utf-8")
    pkg_dir = target / "src" / module
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    if cfg.simple_cli:
        (pkg_dir / "__main__.py").write_text(
            f'"""CLI entry point for {name}."""\n'
            "import sys\n\n\n"
            "def main() -> int:\n"
            f'    print("Hello from {name}!")\n'
            "    return 0\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n",
            encoding="utf-8",
        )
    tests = target / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / f"test_{module}.py").write_text(
        f'def test_version():\n    from {module} import __version__\n    assert __version__ == "{version}"\n',
        encoding="utf-8",
    )


def _scaffold_hatchling_typer(cfg: InitConfig, target: Path) -> None:
    name = cfg.name
    module = _module_from_name(name)
    version = cfg.version
    user = _load_user_info()
    author = cfg.author or user.get("name", "Your Name")
    email = cfg.email or user.get("email", "author@example.com")
    (target / "pyproject.toml").write_text(
        PYPROJECT_HATCHLING_TYPER.format(name=name, module=module, author=author, email=email),
        encoding="utf-8",
    )
    (target / "README.md").write_text(
        f"\nA cookiecutter-pypackage styled boilerplate library including "
        f"dual CLI entrypoints powered by Typer.\n"
        f"```bash\npip install .\n```\n"
        f'For development installations:\n```bash\npip install -e ".[dev]"\n```\n'
        f"```bash\n{name} hello --name Alice\n```\n"
        f"```bash\n{name}-admin setup\n```\n"
        f"- Run tests: `pytest`\n- Format code: `black .`\n",
        encoding="utf-8",
    )
    pkg_dir = target / "src" / module
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg_dir / "cli.py").write_text(
        "import typer\n"
        "from rich import print\n\n"
        f'app = typer.Typer(help="Main CLI for {name}")\n\n'
        "@app.command()\n"
        'def hello(name: str = typer.Argument("World", help="The name to greet")):\n'
        f'    print(f"[bold green]Hello[/bold green] [cyan]{{name}}[/cyan]! Welcome to {name}.")\n\n'
        "@app.command()\n"
        "def version():\n"
        f"    from {module} import __version__\n"
        f'    print(f"{name} version: [yellow]{{__version__}}[/yellow]")\n\n'
        'if __name__ == "__main__":\n'
        "    app()\n",
        encoding="utf-8",
    )
    (pkg_dir / "admin.py").write_text(
        "import typer\n"
        "from rich import print\n\n"
        f'app = typer.Typer(help="Administrative commands for {name}")\n\n'
        "@app.command()\n"
        "def setup():\n"
        '    print("[bold yellow]Initializing secure admin layout... Done.[/bold yellow]")\n\n'
        'if __name__ == "__main__":\n'
        "    app()\n",
        encoding="utf-8",
    )
    tests = target / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_cli.py").write_text(
        "from typer.testing import CliRunner\n"
        f"from {module}.cli import app\n\n"
        "runner = CliRunner()\n\n\n"
        "def test_hello_endpoint():\n"
        '    result = runner.invoke(app, ["hello", "Tester"])\n'
        "    assert result.exit_code == 0\n"
        '    assert "Hello Tester!" in result.stdout\n',
        encoding="utf-8",
    )


def _scaffold_setuptools_cfg(cfg: InitConfig, target: Path) -> None:
    name = cfg.name
    user = _load_user_info()
    author = cfg.author or user.get("name", "")
    email = cfg.email or user.get("email", "")
    github = user.get("github_username", "")
    url = cfg.url or (f"https://github.com/{github}/{name}" if github else "")
    author_lines = ""
    if author:
        author_lines += f"author = {author}\n"
    if email:
        author_lines += f"author_email = {email}\n"
    if url:
        author_lines += f"url = {url}\n"
    (target / "setup.py").write_text('__import__("setuptools").setup()\n', encoding="utf-8")
    (target / "setup.cfg").write_text(
        SETUP_CFG_TEMPLATE.format(name=name, version=cfg.version, author_lines=author_lines),
        encoding="utf-8",
    )
    (target / "pyproject.toml").write_text(
        PYPROJECT_SETUPTOOLS_CFG.format(name=name),
        encoding="utf-8",
    )
    (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (target / f"{name}.py").write_text(
        f'"""Main module for {name}."""\n\n\n'
        "def main() -> None:\n"
        f'    print("Hello from {name}!")\n\n\n'
        'if __name__ == "__main__":\n'
        "    main()\n",
        encoding="utf-8",
    )


@app.callback()
def _main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
) -> None:
    _init_logging(verbose)


@app.command("init")
def init_cmd(
    name: Annotated[str, typer.Argument(help="Project directory name")],
    style: Annotated[InitStyle, typer.Option("--style")] = InitStyle.setuptools_src,
    version: Annotated[str, typer.Option("--version")] = "0.1.0",
    description: Annotated[str, typer.Option("--description")] = "",
    author: Annotated[str | None, typer.Option("--author")] = None,
    email: Annotated[str | None, typer.Option("--email")] = None,
    url: Annotated[str | None, typer.Option("--url")] = None,
    requires_python: Annotated[str, typer.Option("--requires-python")] = ">=3.11",
    simple_cli: Annotated[bool, typer.Option("--simple-cli")] = False,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
) -> None:
    target = Path(name)
    if target.exists() and not force:
        _fail(f"directory '{target}' already exists (use --force)")
    target.mkdir(parents=True, exist_ok=True)
    cfg = InitConfig(
        name=name,
        version=version,
        description=description,
        author=author,
        email=email,
        url=url,
        requires_python=requires_python,
        simple_cli=simple_cli,
    )
    match style:
        case InitStyle.setuptools_src:
            _scaffold_setuptools_src(cfg, target)
        case InitStyle.hatchling_typer:
            _scaffold_hatchling_typer(cfg, target)
        case InitStyle.setuptools_cfg:
            _scaffold_setuptools_cfg(cfg, target)
    console.print(
        Panel.fit(
            f"[green]✓[/green] Project [bold]{name}[/bold] created at [cyan]{target}[/cyan]\n"
            f"Style: [yellow]{style.value}[/yellow]",
            title="init",
            border_style="green",
        )
    )


@app.command("dev")
def dev_cmd(
    path: Annotated[Path, typer.Argument()] = Path("."),
    install_hooks: Annotated[bool, typer.Option("--install-hooks", help="Install pre-commit hooks")] = False,
) -> None:
    project = path.expanduser().resolve()
    if not project.exists():
        project.mkdir(parents=True)
        logger.info("Created project directory: {}", project)
    layout: dict[str, list[str] | None] = {
        "src": None,
        "tests": ["__init__.py", "conftest.py"],
        "docs": None,
        "scripts": None,
        ".github/workflows": ["ci.yml"],
    }
    for rel, files in layout.items():
        d = project / rel
        d.mkdir(parents=True, exist_ok=True)
        if files:
            for f in files:
                (d / f).touch(exist_ok=True)
        else:
            (d / "__init__.py").touch(exist_ok=True)
    pyproject = project / "pyproject.toml"
    if pyproject.exists():
        logger.info("pyproject.toml already exists")
    else:
        pyproject.write_text(DEV_PYPROJECT.format(name=project.name), encoding="utf-8")
        logger.info("Created: pyproject.toml")
    (project / "requirements.txt").touch(exist_ok=True)
    (project / "requirements-dev.txt").write_text(
        "# Development dependencies\n" + "\n".join(DEV_PACKAGES) + "\n",
        encoding="utf-8",
    )
    gitignore = project / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_DEV, encoding="utf-8")
        logger.info("Created: .gitignore")
    if install_hooks:
        try:
            (project / ".pre-commit-config.yaml").write_text(PRE_COMMIT_CONFIG, encoding="utf-8")
            subprocess.run(["pre-commit", "install"], cwd=project, check=True)
            logger.info("Pre-commit hooks installed")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            logger.warning("pre-commit setup failed: {}", exc)
    console.print(
        Panel.fit(
            f"[green]✓[/green] Dev environment ready at [cyan]{project}[/cyan]\n\n"
            "Next steps:\n"
            f"  cd {project}\n"
            "  python -m venv .venv && source .venv/bin/activate\n"
            "  pip install -e '.[dev]'",
            title="dev",
            border_style="green",
        )
    )


@app.command("new-script")
def new_script_cmd(
    path: Annotated[Path, typer.Argument(help="Path to the new script")],
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
) -> None:
    if path.exists() and not force:
        _fail(f"{path} exists (use --force)")
    path.write_text(NEW_SCRIPT_TEMPLATE, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    console.print(f"[green]✓[/green] Created executable script: [cyan]{path}[/cyan]")


@setup_app.command("pyproject")
def setup_pyproject(
    path: Annotated[Path, typer.Argument(help="Path to pyproject.toml or its directory")] = Path("."),
    style: Annotated[SetupStyle, typer.Option("--style")] = SetupStyle.detailed,
    with_cfg: Annotated[bool, typer.Option("--with-cfg", help="Read setup.cfg (detailed only)")] = False,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
    preview: Annotated[bool, typer.Option("--preview", help="Show generated content")] = False,
) -> None:
    target = path.expanduser().resolve()
    pyproject_path = target if target.is_file() else target / "pyproject.toml"
    if not pyproject_path.is_file():
        _fail(f"pyproject.toml not found at {pyproject_path}")
    root = pyproject_path.parent
    data = _load_toml(pyproject_path)
    if "project" in data:
        meta = _extract_pep621_meta(data, root)
    elif "tool" in data and "poetry" in data["tool"]:
        meta = _extract_poetry_meta(data, root)
    else:
        _fail("pyproject.toml has neither [project] nor [tool.poetry]")
    if style == SetupStyle.runtime:
        content = _render_setup_py_runtime(meta)
    elif style == SetupStyle.detailed:
        cfg = _parse_setup_cfg(root / "setup.cfg") if with_cfg else None
        content = _render_setup_py_detailed(meta, root, cfg=cfg, with_cfg=with_cfg)
    else:
        content = _render_setup_py_simple(meta, root)
    if preview:
        console.print(Panel(content, title=f"setup.py ({style.value})", border_style="cyan"))
    out = root / "setup.py"
    if not _write_text(out, content, force=force):
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Created [cyan]{out}[/cyan]")


@setup_app.command("directory")
def setup_directory(
    path: Annotated[Path, typer.Argument(help="Project directory")] = Path("."),
    package: Annotated[str | None, typer.Option("--package", help="Package name")] = None,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
    preview: Annotated[bool, typer.Option("--preview")] = False,
) -> None:
    target = path.expanduser().resolve()
    if not target.is_dir():
        _fail(f"{target} is not a directory")
    pkg = package or target.name.replace("-", "_")
    content = _render_setup_py_for_dir(target, pkg)
    if preview:
        console.print(Panel(content, title="setup.py", border_style="cyan"))
    out = target / "setup.py"
    if not _write_text(out, content, force=force):
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Created [cyan]{out}[/cyan]")


@setup_app.command("wheel")
def setup_wheel(
    wheel: Annotated[Path, typer.Argument(help="Path to .whl file")],
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
) -> None:
    if not wheel.is_file() or wheel.suffix != ".whl":
        _fail(f"{wheel} is not a .whl file")
    tmp = Path(tempfile.mkdtemp(prefix="pydevkit-wheel-"))
    try:
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(tmp)
        meta = _read_wheel_metadata(tmp)
        entry_points = _read_wheel_entry_points(tmp)
        extensions = _find_extensions(tmp)
        out_dir = output_dir.resolve() if output_dir else Path("output") / meta["name"]
        if out_dir.exists() and not force:
            _fail(f"{out_dir} exists (use --force)")
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp, out_dir, dirs_exist_ok=True)
        (out_dir / "setup.py").write_text(
            _render_setup_py_from_wheel(meta, extensions, entry_points),
            encoding="utf-8",
        )
        (out_dir / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools>=61", "wheel"]\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        if entry_points:
            table = Table(title=f"Entry points ({meta['name']})", title_style="bold")
            table.add_column("Group")
            table.add_column("Entries")
            for group, items in entry_points.items():
                table.add_row(group, ", ".join(items))
            console.print(table)
        ext_status = "with binary extensions" if extensions else "pure Python"
        console.print(
            f"[green]✓[/green] setup.py for [bold]{meta['name']}[/bold] ({ext_status}) → [cyan]{out_dir}[/cyan]"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@cargo_app.command("generate")
def cargo_generate(
    input_file: Annotated[Path, typer.Option("-i", "--input")] = Path("Cargo.lock"),
    output: Annotated[Path, typer.Option("-o", "--output")] = Path("Cargo.toml"),
    root_name: Annotated[str | None, typer.Option("--root-name")] = None,
    root_version: Annotated[str, typer.Option("--root-version")] = "0.1.0",
    preview: Annotated[bool, typer.Option("--preview")] = False,
) -> None:
    src = input_file.expanduser()
    if not src.exists():
        _fail(f"{src} not found")
    logger.info("Parsing {}", src)
    data = _parse_cargo_lock(src.read_text(encoding="utf-8"))
    if not data["packages"]:
        _fail("no packages found in lock file")
    logger.info("Found {} packages (lock file v{})", len(data["packages"]), data["version"])
    if preview:
        table = Table(title=f"Cargo packages from {src.name}")
        table.add_column("Name")
        table.add_column("Version")
        table.add_column("Deps")
        for pkg in data["packages"]:
            table.add_row(pkg["name"], pkg["version"], str(len(pkg.get("dependencies", []))))
        console.print(table)
    rendered = _render_cargo_toml(data["packages"], root_name=root_name, root_version=root_version)
    if preview:
        console.print(Panel(rendered, title=f"Cargo.toml → {output}", border_style="cyan"))
    output.write_text(rendered, encoding="utf-8")
    console.print(f"[green]✓[/green] Generated [cyan]{output}[/cyan]")


@app.command("version")
def version_cmd() -> None:
    console.print("[bold]pydevkit[/bold] 1.0.0")


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
