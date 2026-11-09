#!/data/data/com.termux/files/usr/bin/env python
"""Convert images to a target format.
Chooses the backend (OpenCV, scikit-image, Pillow or ImageMagick), converts each image to ``--to`` (jpg by default) at the given quality, keeps or replaces the sources (``-k``/``-f``) and runs the conversion in parallel with ``--dry-run``/``--list-targets`` support.
Usage: imgco [paths ...] [--to jpg|png|webp] [-q QUALITY] [-r] Positional arguments: paths ...
Files or directories (default: current dir) Options: -b, --backend -o, --to Output format/extension (default: jpg) -q, --quality Quality 1-100 where applicable (default 95) -r, --recursive Recurse into directories -k, --keep Keep original files -f, --force Overwrite existing targets / convert already-matching -e, --ext Extra source extension to include (can repeat) -v, --verbose Print per-file results --dry-run List files that would be processed and exit --list-targets Print available target extensions for selected/auto backend and exit ...
plus 1 more; run ``imgco --help`` for the full list.
Examples: $ imgco shots/ --to webp -q 80 -r Command-line script: run ``python imgco.py`` or the ``imgco`` command.
The module is importable as well."""

import argparse
import concurrent.futures
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Optional, Sequence

from dh import MAX_WORKERS


cv2 = None
np = None
ski_io = None
ski_color = None
Image = None
_HAS: dict[str, bool] = {
    "cv2": False,
    "skimage": False,
    "pillow": False,
    "imagemagick": False,
}
try:
    import cv2 as _cv2
    import numpy as _np

    cv2 = _cv2
    np = _np
    _HAS["cv2"] = True
except ImportError:
    pass
try:
    import numpy as _np
    from skimage import (
        color as _ski_color,
        io as _ski_io,
    )

    ski_io = _ski_io
    ski_color = _ski_color
    if np is None:
        np = _np
    _HAS["skimage"] = True
except ImportError:
    pass
try:
    from PIL import Image as _Image

    Image = _Image
    _HAS["pillow"] = True
except ImportError:
    pass
if shutil.which("convert") or shutil.which("magick"):
    _HAS["imagemagick"] = True
TARGET_EXTS: dict[str, set[str]] = {
    "cv2": {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"},
    "skimage": {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"},
    "pillow": {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".gif"},
    "imagemagick": {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".gif", ".avif", ".heic"},
}
PRIORITY: tuple[str, str, str, str] = ("cv2", "skimage", "pillow", "imagemagick")


def detect_backend(preferred: Optional[str] = None) -> str:
    if preferred:
        preferred = preferred.lower()
        if preferred not in _HAS:
            return "none"
        if _HAS[preferred]:
            return preferred
        return "none"
    for name in PRIORITY:
        if _HAS[name]:
            return name
    return "none"


def human_size(nbytes: int) -> str:
    if nbytes < 0:
        nbytes = -nbytes
        sign = "-"
    else:
        sign = ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(nbytes)
    for u in units:
        if size < 1024.0 or u == units[-1]:
            return f"{sign}{size:.1f}{u}" if u != "B" else f"{sign}{int(size)}{u}"
        size /= 1024.0
    return f"{sign}{size:.1f}TB"


def get_dir_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += get_dir_size(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        pass
    return total


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def collect_images(
    paths: Sequence[Path],
    exts: Sequence[str],
    recursive: bool,
) -> list[Path]:
    found: list[Path] = []
    ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts}
    for p in paths:
        p = p.resolve()
        if p.is_file():
            if p.suffix.lower() in ext_set:
                found.append(p)
        elif p.is_dir():
            if recursive:
                for root, _, files in os.walk(p, followlinks=False):
                    for name in files:
                        fp = Path(root) / name
                        if fp.suffix.lower() in ext_set:
                            found.append(fp)
            else:
                try:
                    with os.scandir(p) as it:
                        for entry in it:
                            if entry.is_file(follow_symlinks=False):
                                fp = Path(entry.path)
                                if fp.suffix.lower() in ext_set:
                                    found.append(fp)
                except OSError:
                    pass
    return found


def _normalize_to(to: str) -> str:
    to = to.lower().lstrip(".")
    if to in ("jpg", "jpeg"):
        return ".jpg"
    if to in ("tif", "tiff"):
        return ".tiff"
    return f".{to}"


def process_cv2(src: Path, dst: Path, quality: int, to_ext: str) -> bool:
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        return False
    if img.ndim == 2:
        final = img
    elif img.shape[2] == 4:
        b, g, r, a = cv2.split(img)
        alpha = a.astype(np.float32) / 255.0
        white = 255.0
        b = (b.astype(np.float32) * alpha + white * (1.0 - alpha)).astype(np.uint8)
        g = (g.astype(np.float32) * alpha + white * (1.0 - alpha)).astype(np.uint8)
        r = (r.astype(np.float32) * alpha + white * (1.0 - alpha)).astype(np.uint8)
        final = cv2.merge((b, g, r))
    else:
        final = img
    params: list[int] = []
    if to_ext in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    elif to_ext == ".png":
        params = [int(cv2.IMWRITE_PNG_COMPRESSION), max(0, min(9, (100 - quality) // 10))]
    elif to_ext == ".webp":
        params = [int(cv2.IMWRITE_WEBP_QUALITY), quality]
    return bool(cv2.imwrite(str(dst), final, params))


def process_skimage(src: Path, dst: Path, quality: int, to_ext: str) -> bool:
    img = ski_io.imread(str(src))
    if img is None:
        return False
    if img.ndim == 2:
        rgb = img
    elif img.shape[-1] == 4:
        rgb = ski_color.rgba2rgb(img)
        rgb = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    else:
        rgb = img
    kwargs = {}
    if to_ext in (".jpg", ".jpeg"):
        kwargs["quality"] = quality
    ski_io.imsave(str(dst), rgb, **kwargs)
    return True


def process_pillow(src: Path, dst: Path, quality: int, to_ext: str) -> bool:
    with Image.open(src) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            if to_ext in (".png", ".webp", ".gif", ".tiff", ".tif"):
                final = img.convert("RGBA") if img.mode != "RGBA" else img
            else:
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                final = background
        elif to_ext in (".jpg", ".jpeg") and img.mode != "RGB":
            final = img.convert("RGB")
        else:
            final = img
        save_kwargs = {}
        if to_ext in (".jpg", ".jpeg"):
            save_kwargs = {"quality": quality, "optimize": True}
        elif to_ext == ".webp":
            save_kwargs = {"quality": quality, "method": 4}
        elif to_ext == ".png":
            save_kwargs = {"optimize": True}
        fmt = {
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".png": "PNG",
            ".bmp": "BMP",
            ".tiff": "TIFF",
            ".tif": "TIFF",
            ".webp": "WEBP",
            ".gif": "GIF",
        }.get(to_ext, to_ext[1:].upper())
        final.save(dst, fmt, **save_kwargs)
    return True


def process_imagemagick(src: Path, dst: Path, quality: int, to_ext: str) -> bool:
    cmd = shutil.which("magick") or shutil.which("convert")
    if not cmd:
        return False
    try:
        args = [
            cmd,
            str(src),
            "-background",
            "white",
            "-alpha",
            "remove",
            "-alpha",
            "off",
            "-quality",
            str(quality),
            str(dst),
        ]
        subprocess.run(args, check=True, capture_output=True, timeout=120)
        return dst.is_file()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def process_file(
    path: str | Path,
    backend: str,
    to_ext: str = ".jpg",
    quality: int = 95,
    keep: bool = False,
    force: bool = False,
) -> tuple[str, Optional[int], Optional[int]]:
    path = Path(path)
    if not path.is_file():
        return (f"skip: {path.name} (not a file)", None, None)
    if path.suffix.lower() == to_ext and not force:
        return (f"skip: {path.name} (already {to_ext})", None, None)
    before = path.stat().st_size
    out = path.with_suffix(to_ext)
    if out.exists() and not force:
        out = unique_path(out)
    success = False
    try:
        if backend == "cv2":
            success = process_cv2(path, out, quality, to_ext)
        elif backend == "skimage":
            success = process_skimage(path, out, quality, to_ext)
        elif backend == "pillow":
            success = process_pillow(path, out, quality, to_ext)
        elif backend == "imagemagick":
            success = process_imagemagick(path, out, quality, to_ext)
        else:
            return (f"error: {path.name} (no backend available)", None, None)
    except Exception as exc:
        return (f"error: {path.name} ({exc})", None, None)
    if not success or not out.is_file():
        if out.exists():
            with contextlib.suppress(OSError):
                out.unlink()
        return (f"error: {path.name} (encode failed)", None, None)
    after = out.stat().st_size
    if not keep:
        with contextlib.suppress(OSError):
            path.unlink()
    return (f"ok: {path.name} → {out.name} ({human_size(before)} → {human_size(after)})", before, after)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    installed = [n for n in PRIORITY if _HAS[n]]
    installed_str = " ".join(installed) or "(none)"
    p = argparse.ArgumentParser(
        description="Convert images to a target format (default: jpg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"installed backends: {installed_str}",
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories (default: current dir)",
    )
    p.add_argument(
        "-b",
        "--backend",
        choices=list(PRIORITY),
        default=None,
        metavar="NAME",
        help=f"force backend (default: auto). installed: {installed_str}",
    )
    p.add_argument(
        "-o",
        "--to",
        default="jpg",
        metavar="EXT",
        help="output format/extension (default: jpg)",
    )
    p.add_argument(
        "-q",
        "--quality",
        type=int,
        default=95,
        metavar="Q",
        help="quality 1-100 where applicable (default 95)",
    )
    p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="recurse into directories",
    )
    p.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="keep original files",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite existing targets / convert already-matching",
    )
    p.add_argument(
        "-e",
        "--ext",
        action="append",
        default=None,
        metavar="EXT",
        help="extra source extension to include (can repeat)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print per-file results",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="list files that would be processed and exit",
    )
    p.add_argument(
        "--list-targets",
        action="store_true",
        help="print available target extensions for selected/auto backend and exit",
    )
    p.add_argument(
        "--list-backends",
        action="store_true",
        help="print installed backends and exit",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_backends:
        print("installed backends:")
        for name in PRIORITY:
            status = "yes" if _HAS[name] else "no"
            print(f"  {name}: {status}")
        return 0
    backend = detect_backend(args.backend)
    if backend == "none":
        if args.backend:
            print(f"error: backend '{args.backend}' is not installed", file=sys.stderr)
        else:
            print("error: no image backend available (need cv2 / skimage / pillow / imagemagick)", file=sys.stderr)
        return 2
    available_targets = TARGET_EXTS.get(backend, set())
    if args.list_targets:
        print(f"backend: {backend}")
        print("available targets:", " ".join(sorted(available_targets)) or "(none)")
        return 0
    to_ext = _normalize_to(args.to)
    if to_ext not in available_targets:
        print(
            f"error: target '{to_ext}' not supported by backend '{backend}'. "
            f"available: {' '.join(sorted(available_targets))}",
            file=sys.stderr,
        )
        return 2
    quality = max(1, min(100, args.quality))
    jobs = MAX_WORKERS
    if jobs == 0:
        jobs = os.cpu_count() or 1
    jobs = max(1, jobs)
    default_exts = [".webp", ".bmp", ".png", ".tiff", ".tif", ".jpeg", ".jpg", ".gif"]
    if args.ext:
        default_exts.extend(args.ext)
    exts = list(dict.fromkeys(e.lower() if e.startswith(".") else f".{e.lower()}" for e in default_exts))
    search_paths = args.paths or [Path.cwd()]
    files = collect_images(search_paths, exts, args.recursive)
    files = [f for f in files if f.suffix.lower() != to_ext or args.force]
    if not files:
        print("no images found")
        return 0
    if args.dry_run:
        for f in files:
            print(f)
        print(f"\n{len(files)} file(s) would be processed → {to_ext} (backend={backend})")
        return 0
    cwd = Path.cwd()
    size_before = get_dir_size(cwd) if not args.paths else 0
    results: list[tuple[str, Optional[int], Optional[int]]] = []
    worker: Callable = lambda p: process_file(
        p, backend=backend, to_ext=to_ext, quality=quality, keep=args.keep, force=args.force
    )
    if jobs == 1:
        for f in files:
            res = worker(f)
            results.append(res)
            if args.verbose:
                print(res[0])
    else:
        py_ver = sys.version_info
        if py_ver >= (3, 14) and hasattr(concurrent.futures, "InterpreterPoolExecutor"):
            Executor = concurrent.futures.InterpreterPoolExecutor
        else:
            Executor = concurrent.futures.ProcessPoolExecutor
        with Executor(max_workers=jobs) as ex:
            futs = {ex.submit(worker, f): f for f in files}
            for fut in concurrent.futures.as_completed(futs):
                res = fut.result()
                results.append(res)
                if args.verbose:
                    print(res[0])
    ok = sum(1 for r in results if r[0].startswith("ok:"))
    err = sum(1 for r in results if r[0].startswith("error:"))
    skip = sum(1 for r in results if r[0].startswith("skip:"))
    total_saved = 0
    for msg, b, a in results:
        if b is not None and a is not None:
            total_saved += b - a
    if not args.verbose:
        for msg, _, _ in results:
            if msg.startswith("error:"):
                print(msg, file=sys.stderr)
    print(f"backend={backend}  to={to_ext}  processed={ok}  errors={err}  skipped={skip}")
    if total_saved:
        print(f"space change: {human_size(total_saved)}")
    if not args.paths:
        size_after = get_dir_size(cwd)
        freed = size_before - size_after
        if freed:
            print(f"cwd space freed: {human_size(freed)}")
    return 1 if err else 0


if __name__ == "__main__":
    raise SystemExit(main())
