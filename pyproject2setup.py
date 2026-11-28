#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations
import multiprocessing as mp
from pathlib import Path
import pprint
import re
import sys
import tomllib
from typing import Any, Callable

import libcst as cst
from loguru import logger


NATIVE_BACKENDS = ("maturin", "mesonpy", "scikit_build_core", "meson_python")
DEFAULT_BACKEND = "setuptools"  # PEP 517 fallback when build-backend is absent
POOL_WORKERS = 4
PERSON_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>$")


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------
def backend_name(data: dict[str, Any]) -> str:
    """Return the top-level module of build-backend, e.g. 'poetry' for 'poetry.core.masonry.api'."""
    raw = str(data.get("build-system", {}).get("build-backend", "")).strip()
    if not raw:
        return DEFAULT_BACKEND
    return raw.split(".", 1)[0].lower().replace("-", "_")


def skip_reason(data: dict[str, Any]) -> str | None:
    project = data.get("project", {})
    raw_backend = str(data.get("build-system", {}).get("build-backend", "")).lower()
    backend = backend_name(data)

    if project.get("dynamic"):
        return "project uses dynamic metadata; resolve it before conversion"

    if any(marker in raw_backend for marker in NATIVE_BACKENDS):
        return f"native-build backend detected: {raw_backend}"

    if backend not in ADAPTERS:
        return f"unsupported build backend: {backend}"

    setuptools_config = data.get("tool", {}).get("setuptools", {})
    if backend == "setuptools" and setuptools_config.get("ext-modules"):
        return "setuptools ext-modules are configured"

    return None


# ---------------------------------------------------------------------------
# Backend adapters: each returns a PEP 621-style [project] dict
# ---------------------------------------------------------------------------
def _pep621(data: dict[str, Any]) -> dict[str, Any] | None:
    """setuptools, hatchling, flit_core and pdm already use [project]."""
    return data.get("project")


def _parse_person(entry: str) -> dict[str, str]:
    match = PERSON_RE.match(entry.strip())
    if match:
        return {"name": match["name"], "email": match["email"]}
    return {"name": entry.strip()}


def _numbers(version: str) -> list[int]:
    return ([int(part) for part in version.split(".")] + [0, 0, 0])[:3]


def _bump(nums: list[int], idx: int) -> str:
    upper = [0, 0, 0]
    upper[idx] = nums[idx] + 1
    return ".".join(map(str, upper))


def _caret(version: str) -> str:
    """Poetry ^: compatible with the first non-zero component."""
    nums = _numbers(version)
    width = len(version.split("."))
    idx = next((i for i, n in enumerate(nums[:width]) if n), width - 1)
    return f">={version},<{_bump(nums, idx)}"


def _tilde(version: str) -> str:
    """Poetry ~: bump the minor component if given, else the major."""
    nums = _numbers(version)
    idx = 1 if len(version.split(".")) > 1 else 0
    return f">={version},<{_bump(nums, idx)}"


def _poetry_clause(part: str) -> str:
    if part.startswith("^"):
        return _caret(part[1:])
    if part.startswith("~") and not part.startswith("~="):
        return _tilde(part[1:])
    if part[0].isdigit():  # bare version means exact match in Poetry
        return f"=={part}"
    return part  # ==, !=, >=, <=, >, <, ~= pass through unchanged


def _poetry_specifier(spec: str) -> str:
    if "||" in spec:
        msg = f"OR constraint {spec!r} cannot be expressed in PEP 508"
        raise ValueError(msg)
    clauses = [
        _poetry_clause(part.strip())
        for part in spec.split(",")
        if part.strip() not in ("", "*")
    ]
    return ",".join(clauses)


def _poetry_requirement(name: str, spec: str | dict[str, Any]) -> str:
    if isinstance(spec, dict):
        if any(key in spec for key in ("git", "path", "url")):
            msg = f"dependency {name!r} uses a non-index source; convert manually"
            raise ValueError(msg)
        extras = spec.get("extras")
        if extras:
            name = f"{name}[{','.join(extras)}]"
        spec = spec.get("version", "*")
    return f"{name}{_poetry_specifier(spec)}"


def _poetry_project(data: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy [tool.poetry] metadata into a PEP 621 [project] dict."""
    if "project" in data:  # Poetry 2.x can use [project] directly
        return data["project"]

    poetry = data.get("tool", {}).get("poetry")
    if not isinstance(poetry, dict):
        msg = "missing [tool.poetry] table"
        raise ValueError(msg)

    project: dict[str, Any] = {
        key: poetry[key]
        for key in (
            "name",
            "version",
            "description",
            "keywords",
            "classifiers",
            "readme",
            "license",
        )
        if key in poetry
    }

    if authors := poetry.get("authors"):
        project["authors"] = [_parse_person(entry) for entry in authors]

    url_keys = (
        ("homepage", "Homepage"),
        ("repository", "Repository"),
        ("documentation", "Documentation"),
    )
    urls = {label: poetry[key] for key, label in url_keys if key in poetry}
    if urls:
        project["urls"] = urls

    deps = dict(poetry.get("dependencies", {}))
    python_spec = deps.pop("python", None)
    if python_spec:
        project["requires-python"] = _poetry_specifier(python_spec)

    optional = {
        name
        for name, spec in deps.items()
        if isinstance(spec, dict) and spec.get("optional")
    }
    project["dependencies"] = [
        _poetry_requirement(name, spec)
        for name, spec in deps.items()
        if name not in optional
    ]

    extras = {
        extra: [
            _poetry_requirement(name, deps[name])
            for name in members
            if name in optional
        ]
        for extra, members in poetry.get("extras", {}).items()
    }
    if extras:
        project["optional-dependencies"] = extras

    if scripts := poetry.get("scripts"):
        project["scripts"] = scripts
    if plugins := poetry.get("plugins"):
        project["entry-points"] = plugins

    return project


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "setuptools": _pep621,
    "hatchling": _pep621,
    "flit_core": _pep621,
    "pdm": _pep621,
    "poetry": _poetry_project,
}


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------
def detect_where(data: dict[str, Any], root: Path) -> str:
    """Return the directory that contains importable packages ('.' or 'src' etc.)."""
    tool = data.get("tool", {})

    poetry_pkgs = tool.get("poetry", {}).get("packages", [])
    if poetry_pkgs and "from" in poetry_pkgs[0]:
        return poetry_pkgs[0]["from"]

    hatch_pkgs = (
        tool.get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    if hatch_pkgs:
        return Path(hatch_pkgs[0]).parent.as_posix()

    if pdm_dir := tool.get("pdm", {}).get("build", {}).get("package-dir"):
        return pdm_dir

    return "src" if (root / "src").is_dir() else "."


# ---------------------------------------------------------------------------
# setup.py generation
# ---------------------------------------------------------------------------
def readme_metadata(
    project: dict[str, Any], root: Path
) -> tuple[str | None, str | None]:
    readme = project.get("readme")
    if readme is None:
        return None, None

    if isinstance(readme, str):
        path = root / readme
        return path.read_text(encoding="utf-8"), None

    if isinstance(readme, dict):
        content_type = readme.get("content-type")
        if "text" in readme:
            return readme["text"], content_type
        if "file" in readme:
            path = root / readme["file"]
            return path.read_text(encoding="utf-8"), content_type

    msg = "Unsupported [project].readme format"
    raise ValueError(msg)


def make_setup_kwargs(
    project: dict[str, Any], root: Path, where: str
) -> dict[str, Any]:
    # Standard PEP 621 fields mapped to setuptools setup() arguments.
    field_map = {
        "name": "name",
        "version": "version",
        "description": "description",
        "requires-python": "python_requires",
        "keywords": "keywords",
        "classifiers": "classifiers",
        "dependencies": "install_requires",
        "optional-dependencies": "extras_require",
    }
    kwargs = {
        setup_key: project[toml_key]
        for toml_key, setup_key in field_map.items()
        if toml_key in project and toml_key != "optional-dependencies"
    }

    if "optional-dependencies" in project:
        kwargs["extras_require"] = project["optional-dependencies"]

    for key in ("authors", "maintainers"):
        people = project.get(key, [])
        if people:
            # setup() has single-value author/maintainer fields.
            person = people[0]
            prefix = key[:-1]
            if "name" in person:
                kwargs[prefix] = person["name"]
            if "email" in person:
                kwargs[f"{prefix}_email"] = person["email"]

    if "urls" in project:
        urls = project["urls"]
        kwargs["project_urls"] = urls
        if homepage := urls.get("Homepage"):
            kwargs["url"] = homepage

    if "license" in project:
        license_value = project["license"]
        if isinstance(license_value, str):
            kwargs["license"] = license_value
        elif isinstance(license_value, dict):
            if "text" in license_value:
                kwargs["license"] = license_value["text"]
            elif "file" in license_value:
                kwargs["license_files"] = [license_value["file"]]

    if "license-files" in project:
        kwargs["license_files"] = project["license-files"]

    readme, content_type = readme_metadata(project, root)
    if readme is not None:
        kwargs["long_description"] = readme
        if content_type:
            kwargs["long_description_content_type"] = content_type

    entry_points: dict[str, Any] = {}
    if project.get("scripts"):
        entry_points["console_scripts"] = project["scripts"]
    if project.get("gui-scripts"):
        entry_points["gui_scripts"] = project["gui-scripts"]
    if project.get("entry-points"):
        for group, entries in project["entry-points"].items():
            entry_points[group] = entries
    if entry_points:
        kwargs["entry_points"] = entry_points

    # Placeholder replaced by a real find_packages() call in render_setup().
    kwargs["packages"] = "find:"
    if where != ".":
        kwargs["package_dir"] = {"": where}

    return kwargs


class _PackagesRewriter(cst.CSTTransformer):
    """Swap the 'find:' placeholder for a live find_packages() call."""

    def __init__(self, replacement: cst.BaseExpression) -> None:
        super().__init__()
        self._replacement = replacement

    def leave_DictElement(
        self, original_node: cst.DictElement, updated_node: cst.DictElement
    ) -> cst.DictElement:
        key = updated_node.key
        if isinstance(key, cst.SimpleString) and key.evaluated_value == "packages":
            return updated_node.with_changes(value=self._replacement)
        return updated_node


def render_setup(kwargs: dict[str, Any], where: str) -> str:
    call = "find_packages()" if where == "." else f"find_packages(where={where!r})"
    source = (
        "from setuptools import find_packages, setup\n\n"
        f"setup(**{pprint.pformat(kwargs, sort_dicts=False, width=88)})\n"
    )
    module = cst.parse_module(source).visit(
        _PackagesRewriter(cst.parse_expression(call))
    )
    return module.code


# ---------------------------------------------------------------------------
# Conversion and parallel driver
# ---------------------------------------------------------------------------
def convert(toml_path: Path) -> int:
    if not toml_path.is_file():
        logger.error("File not found: {}", toml_path)
        return 2

    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))

        if reason := skip_reason(data):
            logger.warning("Skipping {}: {}", toml_path, reason)
            return 0

        project = ADAPTERS[backend_name(data)](data)
        if not isinstance(project, dict) or not project.get("name"):
            logger.warning("Skipping {}: no static project name found", toml_path)
            return 0

        root = toml_path.parent
        where = detect_where(data, root)
        kwargs = make_setup_kwargs(project, root, where)

        output = root / "setup.py"
        output.write_text(render_setup(kwargs, where), encoding="utf-8")
        logger.success("Wrote {}", output)
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError, cst.ParserSyntaxError) as exc:
        logger.error("Conversion failed for {}: {}", toml_path, exc)
        return 1


def _worker(path_str: str) -> tuple[str, int]:
    return path_str, convert(Path(path_str))


def main(argv: list[str]) -> int:
    targets = [str(Path(arg).resolve()) for arg in argv] or [
        str(Path("pyproject.toml").resolve())
    ]
    failures = 0
    with mp.Pool(processes=POOL_WORKERS) as pool:
        for path_str, code in pool.imap_unordered(_worker, targets):
            logger.debug("Finished {} (exit {})", path_str, code)
            if code:
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
