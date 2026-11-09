#!/data/data/com.termux/files/usr/bin/env python
"""Run ``ruff`` over the Python files below the current directory.
Includes the extension-less files whose first line looks like a Python script, skips binaries and caches, and invokes ``ruff check`` (optionally with the arguments given on the command line).
Usage: pyruf [ruff arguments ...] Examples: $ pyruf --fix Runs as a script (``python pyruf.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import subprocess
import sys


def is_extensionless_python(file_path: Path) -> bool:
    if file_path.is_file() and not file_path.suffix:
        try:
            with open(file_path, "rb") as f:
                first_line = f.readline()
                return b"python" in first_line
        except (IOError, UnicodeDecodeError):
            return False
    return False


def main():
    args = sys.argv[1:]
    if not args or any(arg in args for arg in ("-h", "--help", "-V", "--version")):
        sys.exit(subprocess.run(["ruff"] + args).returncode)
    subcommand = args[0]
    remaining_args = args[1:]
    targets = []
    flags = []
    for arg in remaining_args:
        if arg.startswith("-"):
            flags.append(arg)
        else:
            targets.append(arg)
    if not targets:
        targets = ["."]
    extensionless_files = []
    native_targets = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            native_targets.append(str(path))
            for p in path.rglob("*"):
                if not any(part.startswith(".") for part in p.parts) and is_extensionless_python(p):
                    extensionless_files.append(p)
        elif path.is_file():
            if is_extensionless_python(path):
                extensionless_files.append(path)
            else:
                native_targets.append(str(path))
    exit_code = 0
    for file_path in extensionless_files:
        try:
            content = file_path.read_bytes()
            cmd = ["ruff", subcommand, "--stdin-filename", str(file_path)] + flags + ["-"]
            result = subprocess.run(cmd, input=content, capture_output=True)
            if subcommand == "format" and "--check" not in flags and "--diff" not in flags:
                if result.returncode == 0 and result.stdout:
                    file_path.write_bytes(result.stdout)
            else:
                if result.stdout:
                    sys.stdout.buffer.write(result.stdout)
                if result.stderr:
                    sys.stderr.buffer.write(result.stderr)
            if result.returncode != 0:
                exit_code = result.returncode
        except Exception as e:
            print(f"Error processing {file_path}: {e}", file=sys.stderr)
            exit_code = 1
    if native_targets:
        native_cmd = ["ruff", subcommand] + flags + native_targets
        native_result = subprocess.run(native_cmd)
        if native_result.returncode != 0:
            exit_code = native_result.returncode
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
