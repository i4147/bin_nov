#!/data/data/com.termux/files/usr/bin/env python
"""Move, sanity-check, verify and strip the binaries of a directory.
Subcommands: ``sort`` collects the binaries into a subfolder, ``sanity`` runs the executables and moves the broken ones (missing libraries) to an error folder, ``verify-so`` loads the shared objects with ctypes, and ``strip``/``size``/``ext``/``exclude``/``retry`` strip them by different selection rules.
Usage: binarytools {sort,sanity,verify-so,strip,size,ext,exclude,retry} [options] Positional arguments: directory Root directory to scan (default: cwd) paths ...
Files or directories (default: cwd, *.so only) Subcommands: sort Move binary files into a subfolder sanity Test executables and move broken ones aside verify-so Verify .so files load via ctypes strip Batch-strip .so files size Strip by minimum size ext Strip by extension list exclude Strip, excluding patterns retry Strip with retry on failure Options: --strip-cmd Strip executable to invoke (default: strip) -v, --verbose Print per-file progress --no-verify Skip ctypes verification after stripping --dest Destination subfolder (default: binary) --recursive Descend into subdirectories --err-dir Folder for failed binaries (default: err) --report Report file path (default: ~/tmp/err) --timeout Per-probe timeout in seconds (default: 2.0) --log-file Log file path --symbols Also count symbols via nm ...
plus 4 more; run ``binarytools --help`` for the full list.
Examples: $ binarytools sanity ~/bin $ binarytools strip --recursive lib/ Command-line script: run ``python binarytools.py`` or the ``binarytools`` command.
The module is importable as well."""

import argparse
import concurrent.futures as _futures
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Iterator, Optional, Sequence

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger

MISSING_LIB_PATTERNS: tuple[str, ...] = (
    "error while loading shared libraries",
    "cannot open shared object file",
    "no such file",
    "not found",
    "failed to load",
)
PROBE_ARGS: tuple[str, ...] = ("--help", "-h", "--version", "-v", "--info")
_ANSI: dict[str, str] = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}


def cprint(msg: str, color: Optional[str] = None) -> None:
    if color and sys.stdout.isatty() and color in _ANSI:
        print(f"{_ANSI[color]}{msg}{_ANSI['reset']}")
    else:
        print(msg)


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}.{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def iter_files(root: Path, ext: Optional[Sequence[str]] = None, recursive: bool = False) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            base = Path(dirpath)
            for name in filenames:
                p = base / name
                if ext and not any(name.endswith(e) for e in ext):
                    continue
                yield p
    else:
        for p in root.iterdir():
            if not p.is_file():
                continue
            if ext and not any(p.name.endswith(e) for e in ext):
                continue
            yield p


def is_binary_file(path: Path, chunk: int = 1024) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(chunk)
    except OSError:
        return False
    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def is_elf_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def is_shebang_script(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def setup_logger(log_path: Path, verbose: bool = False) -> Any:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(log_path)
    if verbose:
        logger.add(sys.stderr, level="DEBUG")
    return logger


def verify_so_load(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "File does not exist"
    if not path.is_file():
        return False, "Not a regular file"
    try:
        ctypes.CDLL(str(path), use_errno=True)
        errno = ctypes.get_errno()
        return True, f"ok (errno={errno})" if errno else "ok"
    except OSError as e:
        return False, f"OSError: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def count_symbols(path: Path, timeout: float = 10.0) -> tuple[bool, int, str]:
    try:
        res = subprocess.run(["nm", str(path)], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, 0, "'nm' not found — install binutils for symbol analysis"
    except subprocess.TimeoutExpired:
        return False, 0, "nm timed out"
    except Exception as e:
        return False, 0, f"nm error: {e}"
    if res.returncode != 0:
        return False, 0, res.stderr.strip()[:200]
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return len(lines) > 0, len(lines), "ok"


def cmd_sort(args: argparse.Namespace) -> int:
    src = Path(args.directory).resolve()
    dst = Path(args.dest)
    if not dst.is_absolute():
        dst = src / dst
    dst.mkdir(parents=True, exist_ok=True)
    moved = 0
    scanned = 0
    for f in iter_files(src, recursive=args.recursive):
        if dst in f.parents or f.parent == dst:
            continue
        scanned += 1
        if is_binary_file(f):
            target = unique_path(dst / f.name)
            try:
                f.rename(target)
                moved += 1
                if args.verbose:
                    print(f"  → {f.name}")
            except OSError as e:
                cprint(f"  ! failed to move {f}: {e}", "red")
    print(f"Scanned {scanned} file(s); moved {moved} binary file(s) to {dst}")
    return 0


def _test_executable(path: Path, timeout: float) -> tuple[Path, Optional[str]]:
    for probe in PROBE_ARGS:
        try:
            res = subprocess.run([str(path), probe], capture_output=True, text=True, timeout=timeout)
            if res.stderr:
                low = res.stderr.lower()
                if any(p in low for p in MISSING_LIB_PATTERNS):
                    return path, res.stderr.strip()[:200]
            if res.returncode == 0:
                return path, None
        except subprocess.TimeoutExpired:
            return path, None
        except FileNotFoundError:
            return path, "File not found"
        except PermissionError:
            return path, "Permission denied"
        except OSError as e:
            if "exec format error" in str(e).lower():
                return path, "Exec format error (wrong architecture)"
            return path, str(e)
    try:
        res = subprocess.run([str(path)], capture_output=True, text=True, timeout=1.0)
        if res.stderr:
            low = res.stderr.lower()
            if any(p in low for p in MISSING_LIB_PATTERNS):
                return path, res.stderr.strip()[:200]
        return path, None
    except subprocess.TimeoutExpired:
        return path, None
    except Exception as e:
        return path, str(e)[:200]


def _collect_testable_executables(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in iter_files(root, recursive=True):
        if ".git" in p.parts or p.is_symlink():
            continue
        if not p.is_file():
            continue
        try:
            mode = p.stat().st_mode
        except OSError:
            continue
        if not (mode & 0o111):
            continue
        if is_shebang_script(p):
            continue
        if not is_elf_binary(p):
            continue
        out.append(p)
    return out


def cmd_sanity(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    err_dir = Path(args.err_dir)
    if not err_dir.is_absolute():
        err_dir = root / err_dir
    report = Path(args.report).expanduser()
    report.parent.mkdir(parents=True, exist_ok=True)
    candidates = _collect_testable_executables(root)
    if not candidates:
        msg = f"No executable binaries found in {root}"
        print(msg)
        report.write_text(msg + "\n", encoding="utf-8")
        return 0
    print(f"Found {len(candidates)} binaries to test")
    print("Testing binaries in parallel...")
    workers = MAX_WORKERS or (os.cpu_count() or 4)
    failures: list[tuple[Path, str]] = []
    with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_test_executable, p, args.timeout): p for p in candidates}
        for i, fut in enumerate(_futures.as_completed(futs), 1):
            src = futs[fut]
            try:
                path, err = fut.result()
            except Exception as e:
                path, err = src, f"Test exception: {str(e)[:100]}"
            if err:
                failures.append((path, err))
                print(f"  [{i}/{len(candidates)}] FAIL {path.name}")
            else:
                print(f"  [{i}/{len(candidates)}] OK   {path.name}")
    if failures:
        err_dir.mkdir(parents=True, exist_ok=True)
        for path, _ in failures:
            target = unique_path(err_dir / path.name)
            try:
                path.rename(target)
            except OSError as e:
                cprint(f"  ! could not move {path}: {e}", "red")
    lines = [
        "Binary Analysis Results",
        f"Directory: {root}",
        f"Total binaries tested: {len(candidates)}",
        f"Failed binaries: {len(failures)}",
        "=" * 40,
    ]
    if failures:
        for path, err in failures:
            lines += [f"Binary: {path}", f"Error: {err}", "-" * 40]
    else:
        lines.append("All binaries tested successfully!")
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "=" * 35)
    print(f"Failed:  {len(failures)}")
    print(f"Success: {len(candidates) - len(failures)}")
    if failures:
        for path, err in failures:
            print(f"  • {path.name}")
            print(f"    → {err[:100]}")
    else:
        print("\nAll binaries are working correctly!")
    print(f"Report written to: {report}")
    print("-" * 40)
    return 1 if failures else 0


def _collect_so_files(inputs: Sequence[str]) -> list[Path]:
    if not inputs:
        return list(iter_files(Path.cwd(), ext=[".so"]))
    out: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(iter_files(p, ext=[".so"], recursive=True))
        else:
            cprint(f"Warning: {p} does not exist", "yellow")
    return out


def cmd_verify_so(args: argparse.Namespace) -> int:
    log_path = Path(args.log_file).expanduser()
    logger = setup_logger(log_path, verbose=args.verbose)
    files = _collect_so_files(args.paths)
    if not files:
        cprint("No .so files found to verify", "yellow")
        return 0
    print(f"\nVerifying {len(files)} shared object file(s)...\n")
    valid = 0
    bad: list[Path] = []
    for so in files:
        ok, msg = verify_so_load(so)
        if ok:
            valid += 1
            note = ""
            if args.symbols:
                has, count, note = count_symbols(so, timeout=args.timeout)
                note = f" [{count} symbols]" if has else f" [{note}]"
            print(f"  ✓ {so}{note}")
            logger.debug(f"{so}: valid {note}")
        else:
            bad.append(so)
            print(f"  ✗ {so}: {msg}")
            logger.error(f"{so}: {msg}")
    print("\n" + "=" * 40)
    print("VERIFICATION SUMMARY")
    print("=" * 40)
    print(f"Total files checked: {len(files)}")
    print(f"✓ Valid files:       {valid}")
    print(f"✗ Files with errors: {len(bad)}")
    if bad:
        print("\n" + "=" * 40)
        print("FILES WITH ERRORS:")
        print("=" * 40)
        for b in bad:
            print(f"  ✗ {b}")
    print(f"Verification complete: {valid} valid, {len(bad)} errors")
    print(f"Log: {log_path}")
    return 1 if bad else 0


class SoStripper:
    def __init__(
        self,
        strip_cmd: str = "strip",
        verify_ctypes: bool = True,
        verbose: bool = False,
    ) -> None:
        self.strip_cmd = strip_cmd
        self.verify_ctypes = verify_ctypes
        self.verbose = verbose
        self.stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "verified": 0,
            "bytes_saved": 0,
        }

    def process_file(self, path: Path) -> dict:
        self.stats["total"] += 1
        result: dict = {"path": path, "success": False, "reason": ""}
        if not path.is_file():
            self.stats["skipped"] += 1
            result["reason"] = "not a regular file"
            return result
        try:
            before = path.stat().st_size
        except OSError as e:
            self.stats["failed"] += 1
            result["reason"] = str(e)
            return result
        backup = path.with_name(path.name + ".bak")
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            self.stats["failed"] += 1
            result["reason"] = f"backup failed: {e}"
            return result
        try:
            proc = subprocess.run(
                [self.strip_cmd, "--strip-unneeded", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                self._restore(backup, path)
                self.stats["failed"] += 1
                result["reason"] = (proc.stderr or "strip failed").strip()[:200]
                return result
        except FileNotFoundError:
            self._restore(backup, path)
            self.stats["failed"] += 1
            result["reason"] = f"'{self.strip_cmd}' not found (install binutils)"
            return result
        except subprocess.TimeoutExpired:
            self._restore(backup, path)
            self.stats["failed"] += 1
            result["reason"] = "strip timed out"
            return result
        except OSError as e:
            self._restore(backup, path)
            self.stats["failed"] += 1
            result["reason"] = str(e)
            return result
        if self.verify_ctypes:
            ok, msg = verify_so_load(path)
            if not ok:
                self._restore(backup, path)
                self.stats["failed"] += 1
                result["reason"] = f"ctypes verify failed: {msg}"
                return result
            self.stats["verified"] += 1
        try:
            after = path.stat().st_size
        except OSError:
            after = before
        backup.unlink(missing_ok=True)
        self.stats["success"] += 1
        self.stats["bytes_saved"] += max(0, before - after)
        result.update(success=True, before=before, after=after)
        if self.verbose:
            pct = (before - after) / before * 100 if before else 0.0
            print(f"  ✓ {path.name}  {before} → {after} bytes (-{pct:.1f}%)")
        return result

    @staticmethod
    def _restore(backup: Path, target: Path) -> None:
        try:
            if backup.exists():
                shutil.move(str(backup), str(target))
        except OSError:
            pass

    def strip_by_size(self, root: Path, min_mb: float) -> dict:
        print(f"\nStripping .so files larger than {min_mb} MB under {root} ...")
        threshold = int(min_mb * 1024 * 1024)
        candidates = [f for f in iter_files(root, ext=[".so"], recursive=True) if f.stat().st_size >= threshold]
        return self._run(candidates)

    def strip_by_extension(self, root: Path, extensions: Sequence[str]) -> dict:
        print(f"\nStripping .so files with extensions {list(extensions)} under {root} ...")
        seen: set[Path] = set()
        candidates: list[Path] = []
        for ext in extensions:
            for f in iter_files(root, ext=[ext], recursive=True):
                if f not in seen:
                    seen.add(f)
                    candidates.append(f)
        return self._run(candidates)

    def strip_by_exclude(self, root: Path, patterns: Sequence[str]) -> dict:
        print(f"\nStripping .so files under {root} (excluding {list(patterns)}) ...")
        candidates = [
            f for f in iter_files(root, ext=[".so"], recursive=True) if not any(pat in f.name for pat in patterns)
        ]
        return self._run(candidates)

    def strip_with_retry(self, root: Path, max_retries: int) -> dict:
        print(f"\nStripping with retry logic (max {max_retries} attempts) under {root} ...")
        candidates = list(iter_files(root, ext=[".so"], recursive=True))
        for path in candidates:
            for attempt in range(max_retries):
                res = self.process_file(path)
                if res["success"]:
                    break
                if attempt < max_retries - 1 and self.verbose:
                    print(f"  Retry {attempt + 1}/{max_retries - 1} for {path.name}")
                    time.sleep(1)
        self._report()
        return self.stats

    def _run(self, candidates: Sequence[Path]) -> dict:
        for path in candidates:
            self.process_file(path)
        self._report()
        return self.stats

    def _report(self) -> None:
        s = self.stats
        print("\n" + "=" * 40)
        print("STRIP SUMMARY")
        print("=" * 40)
        print(f"Total:   {s['total']}")
        print(f"Success: {s['success']}")
        print(f"Failed:  {s['failed']}")
        print(f"Skipped: {s['skipped']}")
        print(f"Verified via ctypes: {s['verified']}")
        print(f"Bytes saved: {s['bytes_saved']:,}")


def _strip_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to scan (default: cwd)",
    )
    p.add_argument(
        "--strip-cmd",
        default="strip",
        help="Strip executable to invoke (default: strip)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Print per-file progress")
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip ctypes verification after stripping",
    )


def cmd_strip(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    stripper = SoStripper(
        strip_cmd=args.strip_cmd,
        verify_ctypes=not args.no_verify,
        verbose=args.verbose,
    )
    if args.strip_mode == "size":
        stripper.strip_by_size(root, args.min_mb)
    elif args.strip_mode == "ext":
        stripper.strip_by_extension(root, args.extensions)
    elif args.strip_mode == "exclude":
        stripper.strip_by_exclude(root, args.patterns)
    elif args.strip_mode == "retry":
        stripper.strip_with_retry(root, args.max_retries)
    else:
        cprint("Unknown strip mode", "red")
        return 2
    return 1 if stripper.stats["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="binarytoolkit.py",
        description="Unified toolkit: move, sanity-check, verify and strip binaries.",
    )
    sub = parser.add_subparsers(dest="command", required=False)
    p_sort = sub.add_parser("sort", help="Move binary files into a subfolder")
    p_sort.add_argument("directory", nargs="?", default=".", help="Directory to scan (default: cwd)")
    p_sort.add_argument("--dest", default="binary", help="Destination subfolder (default: binary)")
    p_sort.add_argument("--recursive", action="store_true", help="Descend into subdirectories")
    p_sort.add_argument("-v", "--verbose", action="store_true")
    p_sort.set_defaults(func=cmd_sort)
    p_sanity = sub.add_parser("sanity", help="Test executables and move broken ones aside")
    p_sanity.add_argument("directory", nargs="?", default=".", help="Directory to scan (default: cwd)")
    p_sanity.add_argument("--err-dir", default="err", help="Folder for failed binaries (default: err)")
    p_sanity.add_argument(
        "--report",
        default=str(Path.home() / "tmp" / "err"),
        help="Report file path (default: ~/tmp/err)",
    )
    p_sanity.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Per-probe timeout in seconds (default: 2.0)",
    )
    p_sanity.set_defaults(func=cmd_sanity)
    p_ver = sub.add_parser("verify-so", help="Verify .so files load via ctypes")
    p_ver.add_argument("paths", nargs="*", help="Files or directories (default: cwd, *.so only)")
    p_ver.add_argument(
        "--log-file",
        default=str(Path.home() / "tmp" / "apps" / "soverify.log"),
        help="Log file path",
    )
    p_ver.add_argument("--symbols", action="store_true", help="Also count symbols via nm")
    p_ver.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="nm timeout in seconds (default: 10)",
    )
    p_ver.add_argument("-v", "--verbose", action="store_true")
    p_ver.set_defaults(func=cmd_verify_so)
    p_strip = sub.add_parser("strip", help="Batch-strip .so files")
    strip_sub = p_strip.add_subparsers(dest="strip_mode", required=True)
    p_size = strip_sub.add_parser("size", help="Strip by minimum size")
    p_size.add_argument("--min-mb", type=float, default=1.0, help="Minimum size in MB (default: 1.0)")
    _strip_common_args(p_size)
    p_ext = strip_sub.add_parser("ext", help="Strip by extension list")
    p_ext.add_argument(
        "--extensions",
        nargs="+",
        default=[".so", ".so.1", ".so.6"],
        help="Extensions to match",
    )
    _strip_common_args(p_ext)
    p_exc = strip_sub.add_parser("exclude", help="Strip, excluding patterns")
    p_exc.add_argument(
        "--patterns",
        nargs="+",
        default=["test", "debug", "profile"],
        help="Filename substrings to exclude",
    )
    _strip_common_args(p_exc)
    p_ret = strip_sub.add_parser("retry", help="Strip with retry on failure")
    p_ret.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum attempts per file (default: 3)",
    )
    _strip_common_args(p_ret)
    p_strip.set_defaults(func=cmd_strip)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        cprint("\nInterrupted by user", "yellow")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
