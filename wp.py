#!/data/data/com.termux/files/usr/bin/env python
"""
Run `wheel pack` on one or more directories and remove each on success.

Usage:
    script.py DIR [DIR ...] script.py -a [OPTIONS] Features -------- * Safety: --dry-run, --keep, --no-verify, --trash, --fail-fast, --max-delete * Selection: --skip-hidden, --include, --exclude, --min-size, --max-size, --skip-symlinks (all in -a mode) * Sorting: --sort {size,name,mtime}, --reverse (in -a mode) * Output: -v/-q, --log, --report (JSON) * Robust: --state-file (resume), --skip-existing, Ctrl-C friendly * Passthru: --dest, --build-number, --wheel-arg

"""

from __future__ import annotations
import argparse
import contextlib
from dataclasses import asdict, dataclass
import fnmatch
import glob
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Optional
import zipfile

from dh import fsz, runcmd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SIZE_SUFFIXES: dict[str, int] = {
    "tb": 1024**4,
    "gb": 1024**3,
    "mb": 1024**2,
    "kb": 1024,
    "t": 1024**4,
    "g": 1024**3,
    "m": 1024**2,
    "k": 1024,
    "b": 1,
}


def parse_size(text: str) -> int:
    """Parse a human-readable size ('1.5G', '200MB', '1024') into bytes."""
    s = text.strip().lower()
    if not s:
        msg = f"invalid size: {text!r}"
        raise argparse.ArgumentTypeError(msg)
    for suffix, mult in _SIZE_SUFFIXES.items():
        if s.endswith(suffix) and len(s) > len(suffix):
            try:
                return int(float(s[: -len(suffix)]) * mult)
            except ValueError:
                msg = f"invalid size: {text!r}"
                raise argparse.ArgumentTypeError(msg)
    try:
        return int(s)
    except ValueError:
        msg = f"invalid size: {text!r}"
        raise argparse.ArgumentTypeError(msg)


def dir_size(path: str) -> int:
    """Recursive byte count of regular files under *path* (no symlink following)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, name))
    return total


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def matches_filters(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    return not any(fnmatch.fnmatch(name, pat) for pat in exclude)


def snapshot_wheels(dest: str) -> set[str]:
    return set(glob.glob(os.path.join(dest, "*.whl")))


def verify_wheel(path: str) -> Optional[str]:
    """Return an error string if *path* is not a valid wheel, else None."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return f"corrupt member in wheel: {bad}"
    except (OSError, zipfile.BadZipFile) as exc:
        return f"invalid wheel: {exc}"
    return None


def move_to_trash(path: str, trash_dir: str) -> str:
    """Move *path* into *trash_dir*, avoiding collisions. Return final path."""
    os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(os.path.abspath(path))
    target = os.path.join(trash_dir, base)
    n = 1
    while Path(target).exists():
        target = os.path.join(trash_dir, f"{base}.{n}")
        n += 1
    shutil.move(path, target)
    return target


def build_pack_cmd(directory: str, args: argparse.Namespace) -> list[str]:
    cmd = ["wheel", "pack", directory]
    if args.dest:
        cmd += ["-d", args.dest]
    if args.build_number is not None:
        cmd += ["--build-number", str(args.build_number)]
    cmd += list(args.wheel_arg)
    return cmd


# ---------------------------------------------------------------------------
# resume state
# ---------------------------------------------------------------------------


def load_state(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("completed", []))
    except (OSError, ValueError):
        return set()


def save_state(path: str, completed: set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"completed": sorted(completed)}, f, indent=2)
    except OSError as exc:
        print(f"warning: cannot write state file {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# per-directory result
# ---------------------------------------------------------------------------


@dataclass
class DirResult:
    directory: str
    success: bool
    skipped: bool = False
    reason: str = ""
    wheel: Optional[str] = None
    size: Optional[int] = None
    duration: float = 0.0
    removed: bool = False


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


def process_dir(
    directory: str,
    size: int,
    args: argparse.Namespace,
    trash_dir: Optional[str],
    state: set[str],
) -> DirResult:
    t0 = time.monotonic()
    res = DirResult(directory=directory, size=size, success=False)

    # state-file resume
    if directory in state:
        res.skipped = True
        res.success = True
        res.reason = "already completed (state file)"
        return res

    base = os.path.basename(os.path.abspath(directory))
    dest = args.dest or "."

    # skip-existing
    if args.skip_existing:
        existing = glob.glob(os.path.join(dest, f"{base}-*.whl"))
        if existing:
            res.skipped = True
            res.success = True
            res.reason = f"wheel already exists: {os.path.basename(existing[0])}"
            return res

    # dry-run short-circuit
    if args.dry_run:
        res.success = True
        res.reason = "dry-run"
        res.wheel = f"(would create *.whl in {dest})"
        return res

    before = snapshot_wheels(dest)

    if not args.quiet:
        print(f"packing {directory} ({fsz(size)})", file=sys.stderr)

    result, txt, err = runcmd(build_pack_cmd(directory, args))

    if txt and args.verbose:
        print(txt, end="")
    if err:
        print(err, end="", file=sys.stderr)

    if result != 0:
        res.reason = f"wheel pack failed (exit {result})"
        return res

    # identify produced wheel(s)
    new = sorted(snapshot_wheels(dest) - before)
    if new:
        res.wheel = new[-1]

    # verify
    if not args.no_verify:
        if not new:
            res.reason = "no wheel produced"
            return res
        for whl in new:
            err_msg = verify_wheel(whl)
            if err_msg:
                res.reason = err_msg
                return res

    # keep
    if args.keep:
        res.success = True
        res.reason = "kept (--keep)"
        return res

    # remove (trash or rmtree)
    try:
        if trash_dir:
            final = move_to_trash(directory, trash_dir)
            res.reason = f"moved to {final}"
        else:
            shutil.rmtree(directory)
            res.reason = "removed"
        res.removed = True
        res.success = True
    except OSError as exc:
        res.reason = f"removal failed: {exc}"

    res.duration = time.monotonic() - t0
    return res


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pack and remove directories with `wheel pack`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ---- selection ----
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="process every top-level directory in '.' one by one",
    )
    parser.add_argument("dirs", nargs="*", metavar="DIR", help="directories to pack (relative to '.')")
    parser.add_argument("--skip-hidden", action="store_true", help="in -a mode, skip dotfiles/dot-dirs")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude dirs matching GLOB (repeatable; -a mode)",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="only include dirs matching GLOB (repeatable; -a mode)",
    )
    parser.add_argument(
        "--min-size",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="skip dirs smaller than SIZE (e.g. 100M; -a mode)",
    )
    parser.add_argument(
        "--max-size",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="skip dirs larger than SIZE (-a mode)",
    )
    parser.add_argument("--skip-symlinks", action="store_true", help="skip top-level symlinks (-a mode)")

    # ---- sorting ----
    parser.add_argument(
        "--sort",
        choices=["size", "name", "mtime"],
        default="size",
        help="sort key for -a mode (default: size)",
    )
    parser.add_argument(
        "-r",
        "--reverse",
        action="store_true",
        help="reverse default order (default: size desc, name asc, mtime desc)",
    )

    # ---- behavior ----
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would happen; do nothing",
    )
    parser.add_argument("--keep", action="store_true", help="pack but do not remove directories")
    parser.add_argument("--no-verify", action="store_true", help="skip verifying the produced wheel")
    parser.add_argument(
        "--trash",
        nargs="?",
        const=os.path.expanduser("~/.local/share/Trash/files"),
        default=None,
        metavar="DIR",
        help="move dirs to DIR instead of deleting (default: XDG trash)",
    )
    parser.add_argument("--fail-fast", action="store_true", help="stop at first failure")
    parser.add_argument(
        "--max-delete",
        type=int,
        default=None,
        metavar="N",
        help="refuse to remove more than N dirs in one run",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip dirs whose wheel already exists in dest",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        metavar="FILE",
        help="resume-able state file (JSON)",
    )
    parser.add_argument("-d", "--dest", default=None, metavar="DIR", help="pass -d DIR to `wheel pack`")
    parser.add_argument(
        "--build-number",
        type=int,
        default=None,
        metavar="N",
        help="pass --build-number N to `wheel pack`",
    )
    parser.add_argument(
        "--wheel-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra arg passed to `wheel pack` (repeatable)",
    )

    # ---- output ----
    parser.add_argument("-v", "--verbose", action="store_true", help="show wheel pack stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--log", default=None, metavar="FILE", help="append per-dir log lines to FILE")
    parser.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="write JSON summary report to FILE",
    )

    args = parser.parse_args()

    # ---- mutual exclusion / basic sanity ----
    if args.all and args.dirs:
        parser.error("-a/--all cannot be combined with explicit directories")
    if not args.all and not args.dirs:
        parser.error("at least one DIR is required (or use -a)")

    # ---- build candidate list ----
    if args.all:
        entries: list[str] = []
        for name in os.listdir("."):
            full = os.path.join(".", name)
            if not Path(full).is_dir():
                continue
            if args.skip_symlinks and Path(full).is_symlink():
                continue
            if args.skip_hidden and is_hidden(name):
                continue
            if not matches_filters(name, args.include, args.exclude):
                continue
            entries.append(name)

        if not entries:
            print("no candidate directories found", file=sys.stderr)
            return 1

        # compute size and mtime once per dir
        sized: list[tuple[str, int, float]] = []
        for name in entries:
            try:
                mtime = os.stat(name).st_mtime
            except OSError:
                mtime = 0.0
            sized.append((name, dir_size(name), mtime))

        if args.min_size is not None:
            sized = [t for t in sized if t[1] >= args.min_size]
        if args.max_size is not None:
            sized = [t for t in sized if t[1] <= args.max_size]

        if not sized:
            print("no candidate directories after size filters", file=sys.stderr)
            return 1

        default_reverse = {"size": True, "name": False, "mtime": True}
        reverse = default_reverse[args.sort] != args.reverse

        if args.sort == "size":
            sized.sort(key=lambda t: t[1], reverse=reverse)
        elif args.sort == "name":
            sized.sort(key=lambda t: t[0], reverse=reverse)
        else:  # mtime
            sized.sort(key=lambda t: t[2], reverse=reverse)

        dirs = [name for name, _sz, _mt in sized]
        sizes = {name: size for name, size, _mt in sized}

        if not args.quiet:
            order = ", ".join(f"{n} ({fsz(sizes[n])})" for n in dirs)
            direction = "desc" if reverse else "asc"
            print(
                f"processing order (sort={args.sort}, {direction}): {order}",
                file=sys.stderr,
            )
    else:
        dirs = list(args.dirs)
        sizes = {d: dir_size(d) for d in dirs}

    # ---- safety: max-delete ----
    if not args.dry_run and args.max_delete is not None and len(dirs) > args.max_delete:
        print(
            f"refusing: {len(dirs)} directories selected, exceeds --max-delete {args.max_delete}",
            file=sys.stderr,
        )
        return 1

    # ---- state ----
    state = load_state(args.state_file) if args.state_file else set()

    # ---- log file ----
    log_fh = None
    if args.log:
        try:
            log_fh = open(args.log, "a", encoding="utf-8")
        except OSError as exc:
            print(f"warning: cannot open log {args.log}: {exc}", file=sys.stderr)

    results: list[DirResult] = []
    failures = 0
    interrupted = False

    try:
        for i, directory in enumerate(dirs, 1):
            if not args.quiet:
                print(f"[{i}/{len(dirs)}]", file=sys.stderr, end=" ", flush=True)

            res = process_dir(directory, sizes.get(directory, 0), args, args.trash, state)
            results.append(res)

            if res.skipped:
                if not args.quiet:
                    print(f"{directory}: skipped ({res.reason})", file=sys.stderr)
            elif res.success:
                if not args.quiet:
                    print(f"{directory}: ok ({res.reason})", file=sys.stderr)
                if args.state_file:
                    state.add(directory)
                    save_state(args.state_file, state)
            else:
                failures += 1
                print(f"{directory}: FAILED ({res.reason})", file=sys.stderr)

            if log_fh is not None:
                status = "skip" if res.skipped else ("ok" if res.success else "fail")
                log_fh.write(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{status}\t{directory}\t{res.size or 0}\t{res.reason}\n"
                )
                log_fh.flush()

            if not res.success and args.fail_fast:
                print("stopping due to --fail-fast", file=sys.stderr)
                break
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted", file=sys.stderr)
    finally:
        if log_fh is not None:
            log_fh.close()

    # ---- summary ----
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    succeeded = sum(1 for r in results if r.success and not r.skipped)
    removed = sum(1 for r in results if r.removed)
    bytes_freed = sum(r.size or 0 for r in results if r.removed)

    if not args.quiet:
        print(
            f"summary: {total} processed, {succeeded} ok, {skipped} skipped, "
            f"{failures} failed, {removed} removed ({fsz(bytes_freed)} freed)",
            file=sys.stderr,
        )

    # ---- report ----
    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "summary": {
                            "total": total,
                            "succeeded": succeeded,
                            "skipped": skipped,
                            "failed": failures,
                            "removed": removed,
                            "bytes_freed": bytes_freed,
                            "interrupted": interrupted,
                        },
                        "results": [asdict(r) for r in results],
                    },
                    f,
                    indent=2,
                )
        except OSError as exc:
            print(f"warning: cannot write report {args.report}: {exc}", file=sys.stderr)

    if interrupted:
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
