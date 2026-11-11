#!/data/data/com.termux/files/usr/bin/env python
import random
import sys
from pathlib import Path


def show_random_cheat():
    cheat_dir = Path.expanduser(Path("~/.cheats"))

    if not cheat_dir.exists() or not cheat_dir.is_dir():
        print(
            f"Error: The directory '{cheat_dir}' does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    cheat_files = [f for f in cheat_dir.rglob("*") if f.is_file()]

    selected_cheat = random.choice(cheat_files)

    try:
        lines = selected_cheat.read_text(encoding="utf-8").splitlines()

        #        clean_lines = [line for line in lines if not line.lstrip().startswith("#")]
        content = "\n".join(lines).strip()

        print(content)
    except Exception as e:
        sys.exit(1)


if __name__ == "__main__":
    show_random_cheat()
