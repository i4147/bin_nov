#!/data/data/com.termux/files/usr/bin/env python
import sys
from pathlib import Path


def normalize_chmod(target_dir: str):
    root = Path(target_dir)
    if not root.exists():
        print(f"Error: Path '{target_dir}' does not exist.")
        return

    for path in root.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(0o755)
            elif path.is_file():
                # Keep executable bits if already set, otherwise default to 644
                current_mode = path.stat().st_mode
                if current_mode & 0o111:
                    path.chmod(0o755)
                else:
                    path.chmod(0o644)
        except PermissionError as e:
            print(f"Skipped {path}: {e}")

    # Apply permissions to the root target itself
    if root.is_dir():
        root.chmod(0o755)
    elif root.is_file():
        root.chmod(0o644)
    print(f"Permissions normalized for: {target_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python normalize_mods.py <path_to_mods_folder>")
        sys.exit(1)
    normalize_chmod(sys.argv[1])
