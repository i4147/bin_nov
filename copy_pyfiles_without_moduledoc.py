#!/data/data/com.termux/files/usr/bin/env python
"""Move the Python files that lack a module docstring out of the current directory.
Checks whether each top-level ``.py`` file starts with a docstring (scripts with a shebang count as undocumented) and moves the undocumented ones to ``~/tmp/notannotated``.
Usage: copy_pyfiles_without_moduledoc Examples: $ copy_pyfiles_without_moduledoc Run from the directory to inspect; files are moved, not copied, despite the name."""

from pathlib import Path
import shutil


def has_module_docstring(path: Path) -> bool:
    code = path.read_text(encoding="utf-8")
    if not code.startswith("#!"):
        s = code.lstrip()
        return s.startswith(('"""', "'''"))
    else:
        return False


def main() -> None:
    source_dir = Path()
    dest_dir = Path.home() / "tmp" / "notannotated"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for py_file in sorted(source_dir.glob("*.py"), reverse=True):
        if not py_file.is_file():
            continue
        if not has_module_docstring(py_file):
            dest = dest_dir / py_file.name
            shutil.move(py_file, dest)
            print(f"moved: {py_file} -> {dest}")


if __name__ == "__main__":
    main()
