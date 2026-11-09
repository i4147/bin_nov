#!/data/data/com.termux/files/usr/bin/env python
"""Wrap the ``gh`` CLI so every invocation is logged.
Finds the real ``gh`` binary, appends the command, the arguments, the exit status and the duration to a dated log file under ``~/tmp/log/apps`` and passes the output through unchanged.
Usage: wrapper_gh [gh arguments ...] Examples: $ wrapper_gh pr list Meant to shadow ``gh`` on ``PATH``; keep the real binary reachable at the path it probes."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


LOG_DIR = Path.home() / "tmp" / "log" / "apps"
REAL_GH: str = "/data/data/com.termux/files/usr/bin/gh"


def find_real_gh() -> Any | None:
    if (
        Path(REAL_GH).is_file()
        and os.access(REAL_GH, os.X_OK)
        and (Path(REAL_GH).resolve() != Path(__file__).resolve())
    ):
        return REAL_GH
    path_dirs = os.environ.get("PATH", "").split(":")
    script_path = Path(__file__).resolve()
    for path_dir in path_dirs:
        candidate = Path(path_dir).joinpath("gh")
        if Path(candidate).is_file() and os.access(candidate, os.X_OK) and (Path(candidate).resolve() != script_path):
            return candidate
    return None


def create_log_file():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    milliseconds = int(time.time() * 400) % 1000
    log_file = LOG_DIR / f"gh_{timestamp}_{milliseconds:03d}.log"
    counter = 1
    while log_file.exists():
        log_file = LOG_DIR / f"gh_{timestamp}_{milliseconds:03d}_{counter}.log"
        counter += 1
    return log_file


def write_log_header(log_file: Path, command_args: Any) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    cwd = os.getcwd()
    with open(log_file, "a") as f:
        f.write("=== GH Command Log ===\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Command: gh {' '.join(command_args)}\n")
        f.write("================================\n\n")


def write_log_footer(log_file: Path, exit_code: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(log_file, "a") as f:
        f.write("\n================================\n")
        f.write(f"Exit Code: {exit_code}\n")
        f.write(f"Completed: {timestamp}\n")
        f.write("================================\n")


def main() -> None:
    real_gh = find_real_gh()
    if not real_gh:
        print("Error: Could not find the real gh binary", file=sys.stderr)
        print("Please install gh first: pkg install gh", file=sys.stderr)
        sys.exit(1)
    log_file = create_log_file()
    command_args = sys.argv[1:]
    write_log_header(log_file, command_args)
    command = [real_gh] + command_args
    try:
        with open(log_file, "a") as log_f:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log_f.write(line)
                log_f.flush()
            process.wait()
            exit_code = process.returncode
    except KeyboardInterrupt:
        exit_code = 130
        print("\nInterrupted by user", file=sys.stderr)
    except Exception as e:
        exit_code = 1
        error_msg = f"Error running command: {e}\n"
        sys.stderr.write(error_msg)
        with open(log_file, "a") as log_f:
            log_f.write(error_msg)
    write_log_footer(log_file, exit_code)
    print(f"Log saved to: {log_file}", file=sys.stderr)
    sys.exit(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
