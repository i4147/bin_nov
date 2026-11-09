#!/data/data/com.termux/files/usr/bin/env python
"""Point the shebangs of Python scripts at the current interpreter path.
Rewrites ``#!...python`` lines (``env``, ``/usr/bin``, Homebrew or Termux prefixes) to ``#!$PREFIX/bin/python3.14`` and restores the executable bit, skipping files whose shebang already matches.
Usage: fix_shebang_opt Examples: $ fix_shebang_opt Runs as a script (``python fix_shebang_opt.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import stat


HOME = Path.home()
cwd = Path.cwd()
PREFIX = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
NEW_SHEBANG = f"#!{PREFIX}/bin/python3.14"
OLD_PREFIXES = (
    str(HOME / ".local/bin"),
    str(PREFIX / "bin"),
    "/usr/bin",
    "/usr/local/bin",
    "/usr/bin/env",
    "/opt/homebrew/bin",
    "/usr/local/opt",
)


def looks_like_python_interpreter(interp: str) -> bool:
    name = Path(interp).name
    return "python" in name


def should_rewrite(interp: str) -> bool:
    if not looks_like_python_interpreter(interp):
        return False
    return interp.strip() != str(PREFIX / "bin/python3.14")


def parse_shebang(first_line: str):
    if not first_line.startswith("#!"):
        return None
    body = first_line[2:].strip()
    if not body:
        return None
    parts = body.split()
    if not parts:
        return None
    exe = parts[0]
    if Path(exe).name == "env" and len(parts) >= 2:
        return parts[1]
    return exe


def rewrite_file(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except (OSError, PermissionError) as e:
        print(f"  ! cannot read {path}: {e}")
        return False
    if b"\x00" in data[:1024]:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    first_newline = text.find("\n")
    if first_newline == -1:
        first_line, rest = text, ""
    else:
        first_line, rest = text[:first_newline], text[first_newline:]
    interp = parse_shebang(first_line)
    if interp is None:
        return False
    if not should_rewrite(interp):
        return False
    new_content = NEW_SHEBANG + rest
    try:
        mode = path.stat().st_mode
        tmp = path.with_suffix(path.suffix + ".shebang.tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.chmod(tmp, stat.S_IMODE(mode))
        tmp.replace(path)
    except OSError as e:
        print(f"  ! cannot write {path}: {e}")
        return False
    print(f"  ✓ {path.name}:  {interp}  →  {PREFIX}/bin/python3.14")
    return True


def main():
    if not cwd.is_dir():
        print(f"Directory not found: {cwd}")
        return
    print(f"Scanning {cwd} …")
    changed = 0
    scanned = 0
    for path in sorted(cwd.iterdir()):
        if not path.is_file():
            continue
        if not os.access(path, os.X_OK):
            continue
        scanned += 1
        if rewrite_file(path):
            changed += 1
    print()
    print(f"Scanned {scanned} executable file(s); rewrote {changed} shebang(s).")


if __name__ == "__main__":
    main()
