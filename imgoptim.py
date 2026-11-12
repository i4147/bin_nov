#!/data/data/com.termux/files/usr/bin/env python
"""Optimize JPEG, PNG and SVG files in place with the external tools that are installed.
Runs jpegoptim (or mogrify), pngquant/optipng and svgo over every matching file below the root, validating that each result still decodes before replacing the original.
``--dry-run`` reports the savings without writing, ``--only`` restricts the file kinds and the remaining options are passed through to the tools; work happens in a process pool.
Usage: imgoptim [options] [paths ...] Options: -n, --dry-run -v, --verbose Forward tool stdout/stderr (drops the tools' -q flag).
--root --no-recursive --only --timeout --jpg-quality --progressive --normal --jpegoptim ...
plus 12 more; run ``imgoptim --help`` for the full list.
Examples: $ imgoptim images/ --dry-run $ imgoptim images/ --only jpg --jpg-quality 82 Command-line script: run ``python imgoptim.py`` or the ``imgoptim`` command.
The module is importable as well."""

import argparse
import contextlib
from dataclasses import dataclass
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Final
import xml.etree.ElementTree as ET

from dh import MAX_WORKERS, walker

GREEN: str = "\033[32m"
DIM: str = "\033[2m"
RESET: str = "\033[0m"
ALL_EXTS: list[str] = [".jpg", ".jpeg", ".png", ".svg"]
KIND_BY_EXT: dict[str, str] = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".svg": "svg"}
JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"
PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
DEFAULT_TIMEOUT: Final[float] = 300.0
PNGQUANT_NOOP: set[int] = {98, 99}


@dataclass(slots=True)
class Result:
    path: Path
    before: int
    after: int
    status: str
    kind: str
    detail: str = ""
    stdout: str = ""
    stderr: str = ""

    @property
    def saved(self) -> int:
        return max(self.before - self.after, 0)


def kind_of(p: Path) -> str:
    return KIND_BY_EXT.get(p.suffix.lower(), "")


def _run(cmd: str, timeout: float, label: str):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "", "", f"{label} timed out after {timeout:g}s"
    except FileNotFoundError:
        return None, "", "", f"{label} not found: {cmd[0]!r}77"
    except OSError as e:
        return None, "", "", f"{label} failed: {e}"
    return proc, proc.stdout or "", proc.stderr or "", None


def _validate_jpeg(p: Path) -> str | None:
    try:
        with p.open("rb") as f:
            if f.read(len(JPEG_MAGIC)) != JPEG_MAGIC:
                return "output is not a JPEG"
    except OSError as e:
        return f"cannot read output: {e}"
    return None


def _validate_png(p: Path) -> str | None:
    try:
        with p.open("rb") as f:
            if f.read(len(PNG_MAGIC)) != PNG_MAGIC:
                return "output is not a PNG"
    except OSError as e:
        return f"cannot read output: {e}"
    return None


def _validate_svg(p: Path) -> str | None:
    try:
        ET.parse(p)
    except ET.ParseError as e:
        return f"output is not well-formed XML: {e}"
    except OSError as e:
        return f"cannot read output: {e}"
    return None


def optimize_jpg(path: Path, before: int, opts: dict, dry_run: bool) -> Result:
    q = opts["jpg_quality"]
    flags: list[str] = []
    if not opts["verbose"]:
        flags.append("-q")
    if opts["jpg_progressive"] and not opts["jpg_normal"]:
        flags.append("--all-progressive")
    elif opts["jpg_normal"] and not opts["jpg_progressive"]:
        flags.append("--all-normal")
    flags.extend(opts["jpegoptim_extra"])
    if dry_run:
        tmpdir = tempfile.mkdtemp()
        try:
            cmd = [opts["jpegoptim"], f"-m{q}", "--strip-all", *flags, "-d", tmpdir, str(path)]
            proc, out, err, e = _run(cmd, opts["timeout"], "jpegoptim")
            if e is not None:
                return Result(path, before, before, "error", "jpg", e, out, err)
            if proc.returncode != 0:
                return Result(path, before, before, "error", "jpg", (err or out).strip()[:200], out, err)
            out_path = Path(tmpdir) / path.name
            if not out_path.exists():
                return Result(path, before, before, "unchanged", "jpg", "", out, err)
            verr = _validate_jpeg(out_path)
            if verr is not None:
                return Result(path, before, before, "error", "jpg", verr, out, err)
            after = out_path.stat().st_size
            if after >= before:
                return Result(path, before, after, "unchanged", "jpg", "", out, err)
            return Result(path, before, after, "optimized", "jpg", "", out, err)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=path.suffix, dir=path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        shutil.copy2(path, tmp_path)
        cmd = [opts["jpegoptim"], f"-m{q}", "--strip-all", *flags, "-o", str(tmp_path)]
        proc, out, err, e = _run(cmd, opts["timeout"], "jpegoptim")
        if e is not None:
            return Result(path, before, before, "error", "jpg", e, out, err)
        if proc.returncode != 0:
            return Result(path, before, before, "error", "jpg", (err or out).strip()[:200], out, err)
        verr = _validate_jpeg(tmp_path)
        if verr is not None:
            return Result(path, before, before, "error", "jpg", verr, out, err)
        after = tmp_path.stat().st_size
        if after >= before:
            return Result(path, before, after, "unchanged", "jpg", "", out, err)
        try:
            shutil.copymode(path, tmp_path)
            os.replace(tmp_path, path)
        except OSError as e:
            return Result(path, before, after, "error", "jpg", f"failed to replace: {e}", out, err)
        tmp_path = None
        return Result(path, before, after, "optimized", "jpg", "", out, err)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def optimize_png(path: Path, before: int, opts: dict, dry_run: bool) -> Result:
    tmp_out: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=path.suffix, dir=path.parent, delete=False) as tmp:
            tmp_out = Path(tmp.name)
        cmd = [opts["pngquant"], "--force", "--strip", f"--quality={opts['png_quality']}"]
        if opts["png_speed"] is not None:
            cmd.extend(["--speed", str(opts["png_speed"])])
        if opts["png_posterize"] is not None:
            cmd.extend(["--posterize", str(opts["png_posterize"])])
        cmd.extend(opts["pngquant_extra"])
        cmd.extend(["--output", str(tmp_out), "--", str(path)])
        proc, out, err, e = _run(cmd, opts["timeout"], "pngquant")
        if e is not None:
            return Result(path, before, before, "error", "png", e, out, err)
        if proc.returncode in PNGQUANT_NOOP:
            return Result(path, before, before, "unchanged", "png", "", out, err)
        if proc.returncode != 0:
            return Result(path, before, before, "error", "png", f"exit {proc.returncode}", out, err)
        verr = _validate_png(tmp_out)
        if verr is not None:
            return Result(path, before, before, "error", "png", verr, out, err)
        after = tmp_out.stat().st_size
        if after >= before:
            return Result(path, before, after, "unchanged", "png", "", out, err)
        if not dry_run:
            try:
                shutil.copymode(path, tmp_out)
                os.replace(tmp_out, path)
            except OSError as e:
                return Result(path, before, after, "error", "png", f"failed to replace: {e}", out, err)
            tmp_out = None
        return Result(path, before, after, "optimized", "png", "", out, err)
    finally:
        if tmp_out is not None:
            with contextlib.suppress(OSError):
                tmp_out.unlink(missing_ok=True)


def optimize_svg(path: Path, before: int, opts: dict, dry_run: bool) -> Result:
    tmp_out: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=path.suffix, dir=path.parent, delete=False) as tmp:
            tmp_out = Path(tmp.name)
        cmd = [opts["svgo"]]
        if not opts["verbose"]:
            cmd.append("-q")
        if opts["svg_multipass"]:
            cmd.append("--multipass")
        if opts["svg_pretty"]:
            cmd.append("--pretty")
        if opts["svg_precision"] is not None:
            cmd.extend(["--precision", str(opts["svg_precision"])])
        if opts["svg_config"]:
            cmd.extend(["--config", opts["svg_config"]])
        cmd.extend(opts["svgo_extra"])
        cmd.extend(["-i", str(path), "-o", str(tmp_out)])
        proc, out, err, e = _run(cmd, opts["timeout"], "svgo")
        if e is not None:
            return Result(path, before, before, "error", "svg", e, out, err)
        if proc.returncode != 0:
            return Result(path, before, before, "error", "svg", (err or out).strip()[:200], out, err)
        verr = _validate_svg(tmp_out)
        if verr is not None:
            return Result(path, before, before, "error", "svg", verr, out, err)
        after = tmp_out.stat().st_size
        if after >= before:
            return Result(path, before, after, "unchanged", "svg", "", out, err)
        if not dry_run:
            try:
                shutil.copymode(path, tmp_out)
                os.replace(tmp_out, path)
            except OSError as e:
                return Result(path, before, after, "error", "svg", f"failed to replace: {e}", out, err)
            tmp_out = None
        return Result(path, before, after, "optimized", "svg", "", out, err)
    finally:
        if tmp_out is not None:
            with contextlib.suppress(OSError):
                tmp_out.unlink(missing_ok=True)


def optimize_one(args: tuple[Path, dict, bool]) -> Result:
    path, opts, dry_run = args
    kind = kind_of(path)
    try:
        before = path.stat().st_size
    except OSError as e:
        return Result(path, 0, 0, "error", kind, str(e))
    try:
        if kind == "jpg":
            return optimize_jpg(path, before, opts, dry_run)
        if kind == "png":
            return optimize_png(path, before, opts, dry_run)
        if kind == "svg":
            return optimize_svg(path, before, opts, dry_run)
        return Result(path, before, before, "unchanged", kind, "unknown extension")
    except Exception as e:
        return Result(path, 0, 0, "error", kind, f"{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="Forward tool stdout/stderr (drops the tools' -q flag)."
    )
    ap.add_argument("--root", default=".")
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--only", nargs="+", choices=["jpg", "png", "svg"], default=None)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    ap.add_argument("--jpg-quality", type=int, default=70)
    ap.add_argument("--progressive", action="store_true")
    ap.add_argument("--normal", action="store_true")
    ap.add_argument("--jpegoptim", default="jpegoptim", metavar="PATH")
    ap.add_argument("--jpegoptim-arg", action="append", default=[], metavar="ARG")
    ap.add_argument("--png-quality", default="65-80")
    ap.add_argument("--speed", type=int, choices=range(1, 12), default=None, metavar="1-11")
    ap.add_argument("--posterize", type=int, choices=range(5), default=None, metavar="0-4")
    ap.add_argument("--pngquant", default="pngquant", metavar="PATH")
    ap.add_argument("--pngquant-arg", action="append", default=[], metavar="ARG")
    ap.add_argument("--svg-precision", type=int, default=None)
    ap.add_argument("--svg-config", default=None)
    ap.add_argument("--no-svg-multipass", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--svgo", default="svgo", metavar="PATH")
    ap.add_argument("--svgo-arg", action="append", default=[], metavar="ARG")
    args = ap.parse_args()
    if args.progressive and args.normal:
        print("error: --progressive and --normal are mutually exclusive", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    files = list(walker(root, recursive=not args.no_recursive, exts=ALL_EXTS))
    if args.only:
        only_set = set(args.only)
        files = [p for p in files if kind_of(p) in only_set]
    if not files:
        print(f"  no image.")
        return 0
    tool_by_kind = {"jpg": args.jpegoptim, "png": args.pngquant, "svg": args.svgo}
    kinds_present = {kind_of(p) for p in files}
    missing: set[str] = set()
    for k in kinds_present:
        if shutil.which(tool_by_kind[k]) is None:
            missing.add(k)
            print(f"Warning: {tool_by_kind[k]!r} not found — skipping all {k} files", file=sys.stderr)
    files = [p for p in files if kind_of(p) not in missing]
    if not files:
        print("No processable files (all required tools missing).")
        return 1
    print(f"Root: {root}")
    print(f"Files: {len(files)} | jobs: {MAX_WORKERS} | dry-run: {args.dry_run}")
    print(
        f"jpg-quality: {args.jpg_quality} | png-quality: {args.png_quality} | "
        f"svg-multipass: {not args.no_svg_multipass}"
    )
    print("-" * 60)
    opts = {
        "jpg_quality": args.jpg_quality,
        "jpg_progressive": args.progressive,
        "jpg_normal": args.normal,
        "jpegoptim": args.jpegoptim,
        "jpegoptim_extra": tuple(args.jpegoptim_arg),
        "png_quality": args.png_quality,
        "png_speed": args.speed,
        "png_posterize": args.posterize,
        "pngquant": args.pngquant,
        "pngquant_extra": tuple(args.pngquant_arg),
        "svg_precision": args.svg_precision,
        "svg_config": args.svg_config,
        "svg_multipass": not args.no_svg_multipass,
        "svg_pretty": args.pretty,
        "svgo": args.svgo,
        "svgo_extra": tuple(args.svgo_arg),
        "timeout": args.timeout,
        "verbose": args.verbose,
    }
    tasks = [(p, opts, args.dry_run) for p in files]
    total_saved = 0
    counts = {"optimized": 0, "unchanged": 0, "error": 0}
    by_kind: dict[str, int] = {}
    with mp.Pool(processes=MAX_WORKERS) as pool:
        for r in pool.imap_unordered(optimize_one, tasks):
            if args.verbose and (r.stdout or r.stderr):
                if r.stdout:
                    sys.stdout.write(r.stdout)
                    if not r.stdout.endswith("\n"):
                        sys.stdout.write("\n")
                if r.stderr:
                    sys.stderr.write(r.stderr)
                    if not r.stderr.endswith("\n"):
                        sys.stderr.write("\n")
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.status == "optimized":
                total_saved += r.saved
                by_kind[r.kind] = by_kind.get(r.kind, 0) + r.saved
                print(f"{r.path.name} {GREEN}{r.saved}{RESET}")
            elif r.status == "unchanged":
                print(f"{r.path.name} {DIM}unchanged{RESET}")
            else:
                print(f"{r.path.name} {DIM}error: {r.detail}{RESET}", file=sys.stderr)
    print("-" * 60)
    label = "Would free" if args.dry_run else "Space freed"
    print(f"Optimized: {counts['optimized']} | Unchanged: {counts['unchanged']} | Errors: {counts['error']}")
    print(f"{label}: {GREEN}{total_saved:,} bytes{RESET} ({total_saved / 1024 / 1024:.2f} MiB)")
    for k in ("jpg", "png", "svg"):
        if k in by_kind:
            print(f"  {k}: {GREEN}{by_kind[k]:,} bytes{RESET}")
    if args.dry_run:
        print("(dry run — no files were modified)")
    return 0 if counts["error"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
