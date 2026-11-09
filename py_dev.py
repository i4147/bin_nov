#!/data/data/com.termux/files/usr/bin/env python
"""Scaffold a new Python project with linting, tests and CI.
Creates the package layout, ``pyproject.toml``, ``.gitignore``, a pre-commit configuration (ruff, black, isort, mypy, the standard hooks), a pytest test and a GitHub Actions workflow, then installs the development requirements and initialises the virtual environment.
The project directory can be passed as the first argument, otherwise the prompts ask for it.
Usage: py_dev [PROJECT_NAME] Examples: $ py_dev mylib Runs as a script (``python py_dev.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import subprocess
import sys


class PythonDevSetup:
    DEV_PACKAGES = [
        "pre-commit",
    ]
    PRE_COMMIT_CONFIG = """repos:
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
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.8.0
    hooks:
      - id: mypy
        additional_dependencies: [types-requests, types-python-dateutil]
        args: [--ignore-missing-imports, --disallow-untyped-defs]
        exclude: ^tests/
"""
    PYTHON_VERSION = "3.12.12"
    GITIGNORE_TEMPLATE = """# Byte-compiled / optimized / DLL files
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
"""

    def __init__(self, project_path: Path | None = None) -> None:
        self.project_path = project_path or Path.cwd()
        self.venv_path = self.project_path / ".venv"
        self.is_windows = sys.platform == "win32"

    def check_python_version(self) -> bool:
        try:
            result = subprocess.run([sys.executable, "--version"], capture_output=True, text=True)
            version_str = result.stdout.strip()
            print(f"Found Python: {version_str}")
            version = version_str.split()[-1]
            major, minor, _ = version.split(".")
            if int(major) >= 3 and int(minor) >= 11:
                return True
            else:
                print(f"Warning: Python {major}.{minor} detected. Recommended: Python 3.11+")
                return True
        except Exception as e:
            print(f"Error checking Python version: {e}")
            return False

    def create_project_structure(self) -> None:
        structure = {
            "src": [],
            "tests": ["__init__.py", "conftest.py"],
            "docs": [],
            "scripts": [],
            ".github/workflows": ["ci.yml"],
        }
        print("\nCreating project structure...")
        for dir_path, files in structure.items():
            full_path = self.project_path / dir_path
            full_path.mkdir(parents=True, exist_ok=True)
            for file in files:
                (full_path / file).touch(exist_ok=True)
                print(f"  Created: {dir_path}/{file}")
            if not files:
                (full_path / "__init__.py").touch(exist_ok=True)
                print(f"  Created: {dir_path}/__init__.py")
        main_file = self.project_path / "src" / "main.py"
        if not main_file.exists():
            main_file.write_text(self._get_main_template())
            print(f"  Created: src/main.py")
        test_file = self.project_path / "tests" / "test_main.py"
        if not test_file.exists():
            test_file.write_text(self._get_test_template())
            print(f"  Created: tests/test_main.py")
        ci_file = self.project_path / ".github" / "workflows" / "ci.yml"
        if not ci_file.exists():
            ci_file.write_text(self._get_ci_template())
            print(f"  Created: .github/workflows/ci.yml")

    def _get_main_template(self) -> str:
        return '''"""Main module for the project."""
def main() -> None:
    print("Hello, Python!")
if __name__ == "__main__":
    main()
'''

    def _get_test_template(self) -> str:
        return '''"""Tests for the main module."""
import pytest
from src.main import main
def test_main(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    captured = capsys.readouterr()
    assert captured.out.strip() == "Hello, Python!"
'''

    def _get_ci_template(self) -> str:
        return """name: Python CI
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install ruff black isort mypy pytest pytest-cov
      - name: Lint with ruff
        run: ruff check .
      - name: Format check with black
        run: black --check .
      - name: Import sort check
        run: isort --check-only .
      - name: Type check with mypy
        run: mypy src/
      - name: Test with pytest
        run: |
          pip install -e .
          pytest --cov=src --cov-report=xml tests/
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml
"""

    def _get_python_path(self) -> str:
        if self.venv_path.exists():
            if self.is_windows:
                return str(self.venv_path / "Scripts" / "python")
            else:
                return str(self.venv_path / "bin" / "python")
        return sys.executable

    def setup_pre_commit(self) -> bool:
        print("\nSetting up pre-commit hooks...")
        try:
            pre_commit_file = self.project_path / ".pre-commit-config.yaml"
            pre_commit_file.write_text(self.PRE_COMMIT_CONFIG)
            print("  Created: .pre-commit-config.yaml")
            python_version_file = self.project_path / ".python-version"
            python_version_file.write_text(self.PYTHON_VERSION)
            print(f"  Created: .python-version ({self.PYTHON_VERSION})")
            gitignore_file = self.project_path / ".gitignore"
            if not gitignore_file.exists():
                gitignore_file.write_text(self.GITIGNORE_TEMPLATE)
                print("  Created: .gitignore")
            subprocess.run(["pre-commit", "install"], cwd=self.project_path, check=True)
            print("  Pre-commit hooks installed")
            subprocess.run(["pre-commit", "run", "--all-files"], cwd=self.project_path)
            return True
        except Exception as e:
            print(f"  Error setting up pre-commit: {e}")
            return False

    def create_pyproject_toml(self) -> bool:
        print("\nCreating pyproject.toml...")
        pyproject_path = self.project_path / "pyproject.toml"
        if pyproject_path.exists():
            print("  pyproject.toml already exists")
            return True
        content = f"""[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"
[project]
name = "{self.project_path.name}"
version = "1.4.7"
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
[tool.coverage.run]
source = ["src"]
[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
]
"""
        pyproject_path.write_text(content)
        print("  Created: pyproject.toml")
        return True

    def create_requirements_files(self) -> bool:
        print("\nCreating requirements files...")
        req_path = self.project_path / "requirements.txt"
        if not req_path.exists():
            req_path.write_text("# Add your production dependencies here\n")
            print("  Created: requirements.txt")
        req_dev_path = self.project_path / "requirements-dev.txt"
        if not req_dev_path.exists():
            dev_deps = "\n".join(self.DEV_PACKAGES)
            req_dev_path.write_text(f"# Development dependencies\n{dev_deps}\n")
            print("  Created: requirements-dev.txt")
        return True

    def generate_neovim_config_info(self) -> bool:
        print("\nCreating Neovim configuration info...")
        neovim_file = self.project_path / "NEOVIM.md"
        if not neovim_file.exists():
            content = f"""# Neovim Configuration for Python Development
This project uses **LazyVim** with a custom Python configuration.
1. **Install Neovim** (v0.9+ recommended):
   Linux/macOS:
   brew install neovim  (macOS)
   sudo apt install neovim  (Ubuntu/Debian)
   Windows:
   choco install neovim
2. **Install LazyVim**:
   mv ~/.config/nvim ~/.config/nvim.bak
   git clone https://github.com/LazyVim/starter ~/.config/nvim
   rm -rf ~/.config/nvim/.git
3. **Add Python Configuration**:
   Create ~/.config/nvim/lua/plugins/python.lua with the configuration.
4. **Install Python LSP Tools**:
   pip install pyright black isort debugpy pynvim
| Key | Action |
|-----|--------|
| <leader>rp | Run current Python file |
| <leader>rv | Run Python file (vertical split) |
| <leader>rh | Run Python file (horizontal split) |
| <leader>pi | Open Python REPL |
| <leader>dc | Debug: Continue |
| <leader>db | Debug: Toggle breakpoint |
| <leader>dr | Debug: Open REPL |
| <leader>di | Debug: Toggle UI |
| <leader>tr | Run tests |
| <leader>tf | Run tests for current file |
| <leader>tt | Run all tests |
| <leader>td | Debug tests |
| <leader>cv | Create virtualenv |
The configuration automatically detects and uses virtual environments.
To create a virtual environment:
python -m venv .venv
source .venv/bin/activate
The Python configuration includes:
- LSP: Pyright + Ruff LSP
- Formatting: Black + isort (via Conform.nvim)
- Linting: Ruff (via nvim-lint)
- Debugging: debugpy with DAP UI
- Testing: neotest with pytest
- Snippets: Python docstring and code snippets
- Virtualenv: Automatic venv detection and creation
LSP not working?
:LspInfo
:Mason
Formatting not working?
:ConformInfo
Debugging not working?
:DapInfo
"""
            neovim_file.write_text(content)
            print("  Created: NEOVIM.md")
        return True

    def run(self) -> bool:
        print("=" * 40)
        print("  Python Development Environment Setup")
        print("=" * 40)
        if not self.check_python_version():
            return False
        self.create_project_structure()
        if not self.create_pyproject_toml():
            return False
        if not self.create_requirements_files():
            return False
        if not self.setup_pre_commit():
            print("  Warning: Pre-commit setup failed, continuing...")
        if not self.generate_neovim_config_info():
            print("  Warning: Neovim info generation failed, continuing...")
        print("\n" + "=" * 40)
        print("  Setup Complete!")
        print("=" * 40)
        print(f"\nProject: {self.project_path}")
        print(f"Python: {self._get_python_path()}")
        print(f"Venv: {self.venv_path}")
        print("\nNext steps:")
        print("  1. Activate virtual environment:")
        if self.is_windows:
            print(f"     .\\{self.venv_path.name}\\Scripts\\activate")
        else:
            print(f"     source {self.venv_path.name}/bin/activate")
        print("  2. Start coding!")
        print("  3. Use pre-commit run --all-files to check everything")
        return True


def main() -> None:
    project_name = None
    if len(sys.argv) > 1:
        project_name = sys.argv[1]
    if project_name:
        project_path = Path(project_name)
        if not project_path.exists():
            project_path.mkdir(parents=True)
            print(f"Created project directory: {project_path}")
    else:
        print("Python Development Environment Setup")
        print("-" * 40)
        project_name = input("Enter project name (or press Enter for current directory): ").strip()
        if project_name:
            project_path = Path(project_name)
            if not project_path.exists():
                project_path.mkdir(parents=True)
                print(f"Created project directory: {project_path}")
        else:
            project_path = Path.cwd()
            print(f"Using current directory: {project_path}")
    setup = PythonDevSetup(project_path)
    success = setup.run()
    if not success:
        print("\nSetup failed. Check the error messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
