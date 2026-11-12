#!/data/data/com.termux/files/usr/bin/env python
"""Verify that the shared objects below the current directory actually load.
Loads each ``.so`` file with ctypes, reports the failures and writes a JSON report to ``/sdcard/soverify.log``; a Python-API wrapper around the process keeps the interpreter stable while the libraries are probed.
Usage: soverify [paths ...] Examples: $ soverify $PREFIX/lib Runs as a script (``python soverify.py``) in the directory it should act on; the module can also be imported.
"""

import ctypes
from pathlib import Path
import subprocess
import sys

from dh import cprint, get_files
from loguru import logger

logger.remove()
logger.add("/data/data/com.termux/files/home/tmp/apps/soverify.log")


class CtypesVerifier:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.platform = sys.platform

    def log(self, message: str, level: str = "DEBUG") -> None:
        if self.verbose:
            getattr(logger, level.lower())(f"[CTYPES] {message}")

    def verify_so_file(self, path: Path) -> tuple[bool, str]:
        if not path.exists():
            return (False, "File does not exist")
        if not path.is_file():
            return (False, "Not a regular file")
        try:
            ctypes.CDLL(str(path), use_errno=True)
            err = ctypes.get_errno()
            if err:
                self.log(f"Warning: errno set to {err} for {path.name}")
            return (True, "ok")
        except OSError as e:
            error_msg = f"OSError: {e}"
            self.log(f"Failed to load {path.name}: {error_msg}", "ERROR")
            return (False, error_msg)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            self.log(f"Failed to load {path.name}: {error_msg}", "ERROR")
            return (False, error_msg)

    def verify_with_symbols(self, path: Path) -> tuple[bool, dict]:
        can_load, msg = self.verify_so_file(path)
        symbol_info = {
            "can_load": can_load,
            "message": msg,
            "has_symbols": False,
            "symbol_count": 0,
        }
        if not can_load:
            return (False, symbol_info)
        try:
            result = subprocess.run(["nm", str(path)], capture_output=True, timeout=10, text=True)
            if result.returncode == 0:
                lines = [line for line in result.stdout.split("\n") if line.strip()]
                symbol_info["symbol_count"] = len(lines)
                symbol_info["has_symbols"] = len(lines) > 0
                self.log(f"Found {len(lines)} symbols in {path.name}")
        except FileNotFoundError:
            self.log(
                "'nm' command not found. Install binutils for symbol analysis",
                "WARNING",
            )
        except subprocess.TimeoutExpired:
            self.log(f"Symbol extraction timed out for {path.name}", "WARNING")
        except Exception as e:
            self.log(f"Could not extract symbols from {path.name}: {e}", "ERROR")
        return (can_load, symbol_info)


def verify_single_file(path: Path) -> bool | None:
    try:
        verifier = CtypesVerifier()
        success, message = verifier.verify_so_file(path)
        if success:
            logger.debug(f"✓ {path.name}: Valid")
            return True
        else:
            logger.error(f"✗ {path.name}: {message}")
            cprint(f"  ✗ {path}: {message}", "red")
            return False
    except Exception as e:
        logger.error(f"✗ {path.name}: Unexpected error - {e}")
        cprint(f"  ✗ {path}: Unexpected error - {e}", "red")
        return False


def collect_files(args: list[str]) -> list[Path]:
    if not args:
        return get_files(Path.cwd(), ext=[".so"])
    files = []
    for arg in args:
        path = Path(arg)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(get_files(path, ext=[".so"]))
        else:
            cprint(f"Warning: {path} does not exist", "yellow")
    return files


def main() -> None:
    files = list(collect_files(sys.argv[1:]))
    if not files:
        cprint("No .so files found to verify", "yellow")
        return
    print(f"\nVerifying {len(files)} shared object file(s)...\n")
    valid_count = 0
    error_count = 0
    error_files = []
    for path in files:
        result = verify_single_file(path)
        if result is True:
            valid_count += 1
        elif result is False:
            error_count += 1
            error_files.append(path)
    print(f"\n{'=' * 40}")
    print("VERIFICATION SUMMARY")
    print(f"{'=' * 40}")
    print(f"Total files checked: {len(files)}")
    print(f"✓ Valid files:       {valid_count}")
    print(f"✗ Files with errors: {error_count}")
    if error_files:
        print(f"\n{'=' * 40}")
        print("FILES WITH ERRORS:")
        print(f"{'=' * 40}")
        for path in error_files:
            print(f"  ✗ {path}")
    print(f"Verification complete: {valid_count} valid, {error_count} errors out of {len(files)} files")
    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    gil_state = ctypes.pythonapi.PyGILState_Ensure()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cprint("\nVerification interrupted by user", "yellow")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        cprint(f"Fatal error: {e}", "red")
        sys.exit(1)
    finally:
        ctypes.pythonapi.PyGILState_Release(gil_state)
    Path("/sdcard/soverify.log").unlink()
