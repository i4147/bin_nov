#!/data/data/com.termux/files/usr/bin/env python
"""Generate a ``setup.py`` for an existing package directory.
Finds the importable package inside the given folder (a directory with ``__init__.py`` or a single module file), scrapes ``__version__``/``__author__``/``__email__``/``__url__``/``__license__`` from ``__init__.py`` and writes a ready-to-run ``setup.py`` next to it that reads the README for the long description.
Usage: build_setup PACKAGE_FOLDER [VERSION] Examples: $ build_setup ./mypkg 1.4.7 Runs as a script (``python build_setup.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import re
import sys


DEFAULT_VERSION: str = "1.4.7"


def resolve_package(target: Path) -> tuple[str | None, Path | None, Path | None]:
    if (target / "__init__.py").is_file():
        return target.name, target, target.parent
    for entry in sorted(target.iterdir()):
        if entry.is_dir() and (entry / "__init__.py").is_file():
            return entry.name, entry, target
    for entry in sorted(target.iterdir()):
        if entry.is_file() and entry.suffix == ".py" and entry.name != "setup.py":
            return entry.stem, entry, target
    return None, None, None


def scrape_metadata(pkg_dir: Path, version: str) -> dict[str, str]:
    meta: dict[str, str] = {
        "version": version,
        "author": "",
        "author_email": "",
        "url": "",
        "license": "",
    }
    init: Path = pkg_dir / "__init__.py"
    if init.is_file():
        text: str = init.read_text(encoding="utf-8", errors="ignore")
        patterns: dict[str, str] = {
            "version": r"__version__\s*=\s*['\"]([^'\"]+)['\"]",
            "author": r"__author__\s*=\s*['\"]([^'\"]+)['\"]",
            "author_email": r"__email__\s*=\s*['\"]([^'\"]+)['\"]",
            "url": r"__url__\s*=\s*['\"]([^'\"]+)['\"]",
            "license": r"__license__\s*=\s*['\"]([^'\"]+)['\"]",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text)
            if match:
                meta[key] = match.group(1)
    return meta


def build_setup_py(pkg_name: str, meta: dict[str, str], out_dir: Path) -> str:
    readme_expr: str = "''"
    for readme in ("README.md", "README.rst", "README.txt", "README"):
        if (out_dir / readme).is_file():
            readme_expr = f"Path({readme!r}).read_text(encoding='utf-8') if Path({readme!r}).is_file() else ''"
            break
    lines: list[str] = [
        '"""Auto-generated setup.py."""',
        "import os",
        "from pathlib import Path",
        "from setuptools import setup, find_packages",
        "",
        "HERE = Path(__file__).resolve().parent",
        "os.chdir(HERE)",
        "",
        "setup(",
        f"    name={pkg_name!r},",
        f"    version={meta['version']!r},",
        f"    description={pkg_name!r},",
        f"    long_description={readme_expr},",
        "    long_description_content_type='text/markdown',",
        f"    author={meta['author']!r},",
        f"    author_email={meta['author_email']!r},",
        f"    url={meta['url']!r},",
        f"    license={meta['license']!r},",
        "    packages=find_packages(exclude=('tests', 'tests.*')),",
        "    include_package_data=True,",
        "    python_requires='>=3.7',",
        "    install_requires=[",
        "    ],",
        "    classifiers=[",
        "        'Programming Language :: Python :: 3',",
        "        'License :: OSI Approved :: MIT License',",
        "        'Operating System :: OS Independent',",
        "    ],",
        ")",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <package_folder> [version]", file=sys.stderr)
        sys.exit(1)
    target: Path = Path(sys.argv[1]).expanduser().resolve()
    version: str = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VERSION
    if not target.is_dir():
        print(f"Error: {target} is not a directory", file=sys.stderr)
        sys.exit(1)
    pkg_name, pkg_dir, out_dir = resolve_package(target)
    if pkg_name is None or pkg_dir is None or out_dir is None:
        print(f"Error: no importable package found at {target}", file=sys.stderr)
        sys.exit(1)
    meta: dict[str, str] = scrape_metadata(pkg_dir, version)
    setup_py: Path = out_dir / "setup.py"
    if setup_py.exists():
        print(f"Warning: {setup_py} already exists, overwriting.", file=sys.stderr)
    setup_py.write_text(build_setup_py(pkg_name, meta, out_dir), encoding="utf-8")
    print(f"Wrote {setup_py} (package={pkg_name!r}, version={meta['version']!r})")


if __name__ == "__main__":
    main()
