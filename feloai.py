#!/data/data/com.termux/files/usr/bin/env python
"""Ask the felo superagent for a script and save the reply as a dated log file.
Runs ``felo-sa.mjs`` with the arguments given on the command line, writes a header with the prompt and a footer with the elapsed time around the returned text and stores everything under ``~/tmp/apps`` as a timestamped Markdown file.
Usage: feloai PROMPT Examples: $ feloai 'write a python script that renames files' Runs as a script (``python feloai.py``) in the directory it should act on; the module can also be imported."""

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys


LOG_DIR = Path.home() / "tmp" / "apps"
FSA: str = "/data/data/com.termux/files/home/bashbin/felo-sa.mjs"


def create_log_file():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_file = LOG_DIR / f"felo_{timestamp}.txt"
    counter = 1
    while log_file.exists():
        log_file = LOG_DIR / f"felo_{timestamp}_{counter}.txt"
        counter += 1
    return log_file


def write_log_header(log_file: Path, cmd_args: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("================================\n")
        f.write(f"Started: {timestamp}\n")
        f.write(f"Working directory: {os.getcwd()}\n")
        f.write(f"Arguments: {cmd_args!r}\n")
        f.write("--- Output ---\n")


def write_log_footer(log_file: Path, exit_code: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n--- End ---\n")
        f.write(f"Finished: {timestamp}\n")
        f.write(f"Exit code: {exit_code}\n")
        f.write("================================\n")


def main() -> int:
    log_file = create_log_file()
    cmd_args = sys.argv[1:]
    if not cmd_args:
        print("Usage: felo <query>", file=sys.stderr)
        return 2
    query = " ".join(cmd_args)
    cmd = [
        "node",
        FSA,
        "--query",
        query,
        "--accept-language",
        "en",
        "--timeout",
        "300",
    ]
    process = None
    exit_code = 1
    write_log_header(log_file, cmd_args)
    try:
        with open(log_file, "a", encoding="utf-8") as log_f:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if process.stdout is not None:
                for line in process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log_f.write(line)
                    log_f.flush()
            exit_code = process.wait()
    except KeyboardInterrupt:
        exit_code = 130
        print("\nInterrupted by user", file=sys.stderr)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
    except Exception as e:
        error_msg = f"Error running command: {e}\n"
        sys.stderr.write(error_msg)
        with open(log_file, "a", encoding="utf-8") as log_f:
            log_f.write(error_msg)
    finally:
        write_log_footer(log_file, exit_code)
        print(f"Log saved to: {log_file}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
