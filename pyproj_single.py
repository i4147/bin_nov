#!/data/data/com.termux/files/usr/bin/env python
"""
Initialize a minimal single-module Python project with one CLI entry point.

Usage:
    python init_python_project.py myproject --entry myproject python init_python_project.py myproject --entry mycli --author "Your Name" --email you@example.com

"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from textwrap import dedent

from dh import atomic_write

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_identifier(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not value or value[0].isdigit():
        value = "_" + value
    return value


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content, encoding="utf-8")
    print(f"Created {path}")


def init_project(
    project_name: str,
    entry_point: str,
    author: str,
    email: str,
    description: str,
    python_version: str,
) -> Path:
    if not PROJECT_NAME_RE.fullmatch(project_name):
        msg = "Project name may contain letters, numbers, dots, underscores, and hyphens only."
        raise ValueError(msg)

    module_name = safe_identifier(entry_point)
    project_dir = Path(project_name).resolve()

    if project_dir.exists() and any(project_dir.iterdir()):
        msg = f"Directory is not empty: {project_dir}"
        raise FileExistsError(msg)

    project_dir.mkdir(parents=True, exist_ok=True)

    pyproject = f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{project_name}"
version = "0.1.0"
description = "{description}"
readme = "README.md"
requires-python = ">={python_version}"
license = {{ text = "MIT" }}
authors = [{{ name = "{author}", email = "{email}" }}]
dependencies = []

[project.scripts]
{entry_point} = "{module_name}:main"

[tool.setuptools]
py-modules = ["{module_name}"]
"""

    module = dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"{description}\"\"\"

        from __future__ import annotations

        import argparse
        import sys

        __version__ = "0.1.0"


        def main(argv: list[str] | None = None) -> int:
            parser = argparse.ArgumentParser(
                prog="{entry_point}",
                description="{description}",
            )
            parser.add_argument(
                "--version",
                action="version",
                version=f"%(prog)s {{__version__}}",
            )
            parser.add_argument(
                "name",
                nargs="?",
                default="world",
                help="Name to greet.",
            )

            args = parser.parse_args(argv)
            print(f"Hello, {{args.name}}!")
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
    """)

    readme = dedent(f"""\
        # {project_name}

        {description}

        ## Installation

        ```bash
        pip install .
        ```

        ## Usage

        ```bash
        {entry_point} --help
        {entry_point} world
        ```

        ## Development

        Run the module directly:

        ```bash
        python {module_name}.py --help
        ```
    """)

    gitignore = dedent("""\
        __pycache__/
        *.py[cod]
        *.egg-info/
        .eggs/
        build/
        dist/
        .venv/
        venv/
        .pytest_cache/
        .mypy_cache/
        .ruff_cache/
        .coverage
        htmlcov/
    """)

    tests = dedent(f"""\
        import {module_name}


        def test_main_returns_zero(capsys):
            assert {module_name}.main(["world"]) == 0
            captured = capsys.readouterr()
            assert captured.out == "Hello, world!\\n"
    """)

    write_file(project_dir / "pyproject.toml", pyproject)
    write_file(project_dir / f"{module_name}.py", module)
    write_file(project_dir / "README.md", readme)
    write_file(project_dir / ".gitignore", gitignore)
    write_file(project_dir / "tests" / "test_module.py", tests)

    return project_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a minimal single-module Python CLI project."
    )
    parser.add_argument(
        "project_name", help="Distribution/project name, e.g. my-project"
    )
    parser.add_argument(
        "--entry",
        default=None,
        help="CLI command and Python module name (default: derived from project name)",
    )
    parser.add_argument("--author", default="Your Name")
    parser.add_argument("--email", default="you@example.com")
    parser.add_argument(
        "--description",
        default="A minimal Python command-line application.",
    )
    parser.add_argument("--python", default="3.8", help="Minimum Python version")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entry_point = args.entry or safe_identifier(args.project_name)

    try:
        project_dir = init_project(
            project_name=args.project_name,
            entry_point=entry_point,
            author=args.author,
            email=args.email,
            description=args.description,
            python_version=args.python,
        )
    except (ValueError, FileExistsError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("\nProject initialized:")
    print(f"  cd {project_dir}")
    print("  python -m venv .venv")
    print("  source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate")
    print("  pip install -e .")
    print(f"  {entry_point} --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
