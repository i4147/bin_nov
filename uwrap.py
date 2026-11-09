#!/data/data/com.termux/files/usr/bin/env python
"""Universal command wrapper with logging, colors, and clipboard support."""

import argparse
import datetime as dt
import glob
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any


LOG_DIR = Path.home() / "tmp" / "log" / "apps"
CLIPBOARD_MAX_BYTES: int = 1 * 1024 * 1024
COLORS: dict[str, str] = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def color(text: str, color_name: str = "reset", bold: bool = False) -> str:
    if not supports_color():
        return text
    prefix = COLORS.get(color_name, COLORS["reset"])
    if bold:
        prefix += COLORS["bold"]
    return f"{prefix}{text}{COLORS['reset']}"


def expand_glob_args(args: list[str]) -> list[str]:
    expanded = []
    for arg in args:
        if not any(ch in arg for ch in "*?["):
            expanded.append(arg)
            continue
        matches = glob.glob(arg)
        if matches:
            expanded.extend(sorted(matches))
        else:
            expanded.append(arg)
    return expanded


def create_log_file(name: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    milliseconds = int(time.time() * 1000) % 1000
    log_file = LOG_DIR / f"{name}_{timestamp}_{milliseconds:03d}.log"
    counter = 1
    while log_file.exists():
        log_file = LOG_DIR / f"{name}_{timestamp}_{milliseconds:03d}_{counter}.log"
        counter += 1
    return log_file


def write_log_header(log_file: Path, command: list[str], cwd: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{'=' * 50}\n")
        f.write(f"Command: {shlex.join(command)}\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Cwd: {cwd}\n")
        f.write(f"{'=' * 50}\n\n")


def write_log_footer(log_file: Path, exit_code: int) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 50}\n")
        f.write(f"Exit Code: {exit_code}\n")
        f.write(f"Completed: {timestamp}\n")
        f.write(f"{'=' * 50}\n")


def copy_to_clipboard(data: str) -> bool:
    try:
        proc = subprocess.run(
            ["termux-clipboard-set"],
            input=data,
            text=True,
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def parse_args(argv: list[str]) -> tuple[str, list[str], argparse.Namespace]:
    parser = argparse.ArgumentParser(
        description="Universal command wrapper with logging, colors, and clipboard",
        add_help=False,
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--no-log", action="store_true", help="Disable logging")
    parser.add_argument("--timestamp", action="store_true", help="Prefix output with timestamps")
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Disable clipboard copying (enabled by default)",
    )
    parser.add_argument("--help", action="store_true", help="Show this help message")
    known, rest = parser.parse_known_args(argv)
    if known.help:
        parser.print_help()
        raise SystemExit(0)
    if not rest:
        parser.print_usage(sys.stderr)
        msg = "error: provide a command to wrap, e.g. wrapper.py ls -la *.py"
        raise SystemExit(msg)
    return rest[0], rest[1:], known


def main() -> None:
    name, command_args, opts = parse_args(sys.argv[1:])
    command_args = expand_glob_args(command_args)
    command = [name, *command_args]
    log_file = None
    if not opts.no_log:
        log_file = create_log_file(name)
        write_log_header(log_file, command, os.getcwd())
    exit_code = 1
    output_buffer = [] if not opts.no_clipboard else None
    output_size = 0
    try:
        with open(log_file, "a", encoding="utf-8") if log_file else nullcontext() as log_f:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in process.stdout:
                if line.startswith(("Error:", "error:", "WARNING:", "warning:")):
                    output = color(line, "yellow")
                elif line.startswith(("Fatal:", "fatal:", "Traceback")):
                    output = color(line, "red", bold=True)
                else:
                    output = line
                if opts.timestamp:
                    ts = dt.datetime.now().strftime("%H:%M:%S")
                    output = color(f"[{ts}] ", "gray") + output
                sys.stdout.write(output)
                sys.stdout.flush()
                if log_f:
                    log_f.write(line)
                    log_f.flush()
                if output_buffer is not None:
                    output_buffer.append(line)
                    output_size += len(line.encode("utf-8"))
                    if output_size > CLIPBOARD_MAX_BYTES:
                        output_buffer = None
                        print(
                            color(
                                f"\n[clipboard] Output exceeds {CLIPBOARD_MAX_BYTES // 1024}KB limit, skipping copy",
                                "yellow",
                            ),
                            file=sys.stderr,
                        )
            process.wait()
            exit_code = process.returncode
    except KeyboardInterrupt:
        exit_code = 130
        print(color("\nInterrupted by user", "red", bold=True), file=sys.stderr)
    except FileNotFoundError:
        exit_code = 127
        msg = color(f"Error: command '{name}' not found", "red", bold=True)
        print(msg, file=sys.stderr)
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"Error: command '{name}' not found\n")
    except Exception as exc:
        exit_code = 1
        error_msg = color(f"Error running command: {exc}", "red", bold=True)
        print(error_msg, file=sys.stderr)
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"Error: {exc}\n")
    if output_buffer is not None:
        clipboard_text = "".join(output_buffer)
        copy_to_clipboard(clipboard_text)
    if log_file:
        write_log_footer(log_file, exit_code)
        print(color(f"Log saved to: {log_file}", "cyan"), file=sys.stderr)
    raise SystemExit(exit_code)


class nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> bool:
        return False


if __name__ == "__main__":
    main()
