#!/data/data/com.termux/files/usr/bin/env python
"""Normalize the shebang of every Python file below the current directory.
Rewrites ``#!...python2/3`` lines to ``#!/data/data/com.termux/files/usr/bin/env python`` and processes the files in a worker pool, reporting how many shebangs were changed.
Usage: fixshebang [paths ...] Examples: $ fixshebang Runs as a script (``python fixshebang.py``) in the directory it should act on; the module can also be imported."""

from multiprocessing import Pool
from pathlib import Path
import re
from typing import Any, Final


SHEBANG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#!.*python[23]?(?:\.\d+)?(?:[ \t]+.*)?$", re.MULTILINE)
PY_SHEBANG: Final[str] = "#!/data/data/com.termux/files/usr/bin/env python"


def find_python_files(directory: Path) -> list[Path]:
    from dh import get_pyfiles

    return get_pyfiles(directory)


def process_file(path: Path, root_dir: Path) -> tuple[Path, bool, str | None, str, str]:
    rel_path = str(path.relative_to(root_dir))
    if path.is_symlink():
        return (path, False, "Symlink skipped", rel_path, "skipped")
    try:
        content = path.read_text(encoding="utf-8")
        new_shebang = PY_SHEBANG
        has_shebang = content.startswith("#!")
        if not has_shebang:
            path.write_text(f"{new_shebang}\n{content}", encoding="utf-8")
            return (path, True, None, rel_path, "added")
        first_line = content.split("\n", 1)[0]
        if "python" not in first_line.lower():
            return (path, False, "Not a Python shebang", rel_path, "skipped")
        if first_line.strip() == new_shebang:
            return (path, False, "Already correct", rel_path, "unchanged")
        lines = content.split("\n")
        lines[0] = new_shebang
        path.write_text("\n".join(lines), encoding="utf-8")
        return (path, True, None, rel_path, "updated")
    except Exception as e:
        return (path, False, str(e), rel_path, "error")


def _process_file_star(
    args: tuple[Path, Path],
) -> tuple[Path, bool, str | None, str, str]:
    return process_file(*args)


def main() -> int:
    current_dir = Path.cwd()
    print(f"📁 Scanning directory: {current_dir}")
    print("-" * 40)
    python_files = list(find_python_files(current_dir))
    if not python_files:
        print("No Python files found.")
        return 0
    print(f"Found {len(python_files)} Python files to check.")
    print("-" * 40)
    updated_files: list[tuple[Path, str]] = []
    added_shebang_files: list[tuple[Path, str]] = []
    errors: list[tuple[str, str]] = []
    skipped_count = 0
    already_correct_count = 0
    not_python_count = 0
    with Pool(processes=8) as pool:
        async_results: list[Any] = []
        for path in python_files:
            async_results.append(pool.apply_async(_process_file_star, ((path, current_dir),)))
        for async_result in async_results:
            path, was_changed, error, rel_path, action_type = async_result.get()
            if error:
                if "Symlink" in error:
                    skipped_count += 1
                elif "Not a Python shebang" in error:
                    not_python_count += 1
                elif "Already correct" in error:
                    already_correct_count += 1
                else:
                    errors.append((rel_path, error))
            elif was_changed and action_type == "updated":
                updated_files.append((path, rel_path))
            elif was_changed and action_type == "added":
                added_shebang_files.append((path, rel_path))
            else:
                skipped_count += 1
    if updated_files:
        print(f"\n✅ Updated existing shebangs in {len(updated_files)} files:")
        print("-" * 40)
        for path, rel_path in updated_files:
            file_info = rel_path
            if "." not in Path(rel_path).name:
                file_info += " (no extension)"
            print(f"  ✏️  {file_info}")
    if added_shebang_files:
        print(f"\n➕ Added new shebang to {len(added_shebang_files)} files:")
        print("-" * 40)
        for path, rel_path in added_shebang_files:
            file_info = rel_path
            if "." not in Path(rel_path).name:
                file_info += " (no extension)"
            print(f"  ➕ {file_info}")
    if not updated_files and not added_shebang_files:
        print("\n✅ No files needed updating.")
    print("\n" + "=" * 40)
    print("📊 Summary:")
    print(f"  ✏️  Updated existing shebangs: {len(updated_files)} files")
    print(f"  ➕ Added new shebangs: {len(added_shebang_files)} files")
    print(f"  ⏭️  Skipped (symlinks): {skipped_count} files")
    print(f"  ⏭️  Not Python shebang: {not_python_count} files")
    print(f"  ⏭️  Already correct: {already_correct_count} files")
    print(f"  📝 Total processed: {len(python_files)} files")
    if errors:
        print(f"  ❌ Errors: {len(errors)} files")
    if errors:
        print("\n❌ Errors:")
        for rel_path, error in errors:
            print(f"  - {rel_path}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
