#!/data/data/com.termux/files/usr/bin/env python
"""Create extension-less symlinks for the scripts in the personal bin folders.
For ``~/bashbin`` (``.sh``) and ``~/bin`` plus ``~/.local/bin`` (``.py``) it adds a symlink without the extension pointing at the script, so the tools can be called by name.
Existing non-symlink entries with the same name are replaced.
Usage: csym Examples: $ csym Runs as a script (``python csym.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path


BASHBIN: Path = Path.home() / "bashbin"
BIN: Path = Path.home() / "bin"
LOBIN: Path = Path.home() / ".local" / "bin"


def process_dir(cwd: Path, ext: str) -> None:
    for path in cwd.glob(f"*.{ext}"):
        path = path.resolve()
        symlink_path = path.with_name(path.stem)
        if symlink_path.exists() and not symlink_path.is_symlink():
            symlink_path.unlink()
            symlink_path.symlink_to(path.name)
            print(f"Created: {symlink_path.name} -> {path.name}")
            continue
        if symlink_path.exists() and symlink_path.is_symlink():
            continue
        if not symlink_path.exists() or not symlink_path.is_symlink():
            symlink_path.symlink_to(path.name)
            print(f"Created: {symlink_path.name} -> {path.name}")


if __name__ == "__main__":
    process_dir(BASHBIN, "sh")
    process_dir(BIN, "py")
    process_dir(LOBIN, "py")
