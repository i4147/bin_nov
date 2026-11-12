#!/data/data/com.termux/files/usr/bin/env python
"""Image and media optimisation toolkit.
Subcommands: ``auto-enhance``/``pil-enhance`` improve the photos in place, ``downscale``/``resize`` shrink them by a factor, ``upscale`` enlarges the small ones, ``strip-exif`` removes the metadata, ``optimize-png`` calls optipng/oxipng and ``embed-optimize`` compresses the base64 assets inside CSS/HTML/JS.
Usage: imgtools {auto-enhance,downscale,embed-optimize,optimize-png,pil-enhance,resize,strip-exif,upscale} [options] Positional arguments: inputs ...
Files or folders.
Defaults to recursive '.'.
scale_factor Scale factor (0 < f <= 1.0).
paths ...
Files or directories.
Default: current directory.
Subcommands: auto-enhance Google-Photos-style in-place image enhancement downscale Downscale images in-place by a scale factor embed-optimize Optimize base64 resources inside CSS/HTML/JS optimize-png Optimize PNG files with optipng or oxipng pil-enhance PIL ImageEnhance in-place enhancement resize PIL in-place downscale by scale factor strip-exif Strip EXIF data from images upscale Upscale small images by width-based factors Options: -v, --verbose Print per-image details.
--parallel Enable multiprocessing.
--root Root directory to scan.
Default: current directory.
--dry-run List files without processing.
--timeout External command timeout in seconds.
--png-command PNG optimizer command.
--jpg-command JPEG optimizer command.
--webp-to-jpg-command WebP-to-JPEG command.
--svg-command SVG optimizer command.
--js-command JS optimizer command.
...
plus 16 more; run ``imgtools --help`` for the full list.
Examples: $ imgtools resize 0.5 photos/ $ imgtools strip-exif shots/ Command-line script: run ``python imgtools.py`` or the ``imgtools`` command.
The module is importable as well."""

import argparse
import base64
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Sequence

from dh import MAX_WORKERS

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None
DEFAULT_IMAGE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
DEFAULT_PNG_EXTS: set[str] = {".png"}
DEFAULT_EMBED_EXTS: set[str] = {".css", ".html", ".htm", ".js"}
DEFAULT_RESIZE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
DEFAULT_UPSCALE_EXTS: set[str] = {".webp", ".jpg", ".jpeg", ".png"}
EMBED_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
}
EMBED_EXT_KIND: dict[str, str] = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".webp": "webp",
    ".svg": "svg",
    ".css": "css",
    ".js": "js",
}
EMBED_KIND_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "svg": "image/svg+xml",
    "css": "text/css",
    "js": "application/javascript",
}
EMBED_DATA_RE = re.compile(
    r"data:(?P<mime>image/(?:png|jpe?g|webp|svg\+xml)|text/css|(?:application|text)/javascript);base64,(?P<data>[A-Za-z0-9+/=]+)"
)


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            with contextlib.suppress(OSError):
                total += f.stat().st_size
    return total


def parse_extensions(value: str | Sequence[str]) -> set[str]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return {p if p.startswith(".") else f".{p}" for p in parts if p}


def iter_files(
    paths: Sequence[Path],
    extensions: set[str],
    recursive: bool = True,
) -> list[Path]:
    exts = {e.lower() for e in extensions}
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in exts:
                found.append(p)
        elif p.is_dir():
            iterator = p.rglob("*") if recursive else p.glob("*")
            for f in iterator:
                if f.is_file() and f.suffix.lower() in exts:
                    found.append(f)
        else:
            print(f"[WARNING] Skipping invalid path: {p}")
    seen: set[Path] = set()
    out: list[Path] = []
    for f in found:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            out.append(f)
    return out


def run_command(
    cmd: list[str],
    timeout: int | None = None,
    capture: bool = True,
) -> tuple[int, str, str]:
    res = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    return res.returncode, res.stdout or "", res.stderr or ""


def parallel_map(
    func: Callable[[Any], Any],
    items: Sequence[Any],
    workers: int | None = None,
    use_threads: bool = False,
    desc: str = "Processing",
) -> list[Any]:
    if not items:
        return []
    workers = workers or os.cpu_count() or 1
    Executor = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
    results: list[Any] = []
    with Executor(max_workers=workers) as executor:
        futures = [executor.submit(func, item) for item in items]
        iterator: Iterable[Any] = as_completed(futures)
        if tqdm is not None:
            iterator = tqdm(iterator, total=len(futures), desc=desc, unit="item", ncols=80)
        for fut in iterator:
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(exc)
    return results


def _import_cv2():
    try:
        import cv2
        import numpy as np

        return cv2, np
    except ImportError as exc:
        msg = "opencv-python and numpy are required for this command"
        raise SystemExit(msg) from exc


def _import_pil():
    try:
        from PIL import Image, ImageEnhance

        return Image, ImageEnhance
    except ImportError as exc:
        msg = "Pillow is required for this command"
        raise SystemExit(msg) from exc


def _auto_enhance_one(task: tuple[Path, bool]) -> bool:
    path, verbose = task
    cv2, np = _import_cv2()
    try:
        img = cv2.imread(str(path))
        if img is None:
            print(f"[ERROR] Could not read: {path}")
            return False
        denoised = cv2.fastNlMeansDenoisingColored(img, None, 3, 3, 7, 21)
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
        l_chan, a_chan, b_chan = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_eq = clahe.apply(l_chan)
        merged = cv2.merge((l_eq, a_chan, b_chan))
        bgr = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h_chan, s_chan, v_chan = cv2.split(hsv)
        s_chan = np.clip(s_chan * 1.1, 0, 255).astype(np.uint8)
        hsv2 = cv2.merge((h_chan, s_chan, v_chan))
        bgr2 = cv2.cvtColor(hsv2, cv2.COLOR_HSV2BGR)
        blur = cv2.GaussianBlur(bgr2, (0, 0), 2.0)
        sharp = cv2.addWeighted(bgr2, 1.5, blur, -0.5, 0)
        cv2.imwrite(str(path), sharp)
        if verbose:
            print(f"[SUCCESS] Enhanced and replaced: {path.name}")
        return True
    except Exception as exc:
        print(f"[FAILED] Error processing {path.name}: {exc}")
        return False


def cmd_auto_enhance(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path()]
    files = iter_files(inputs, DEFAULT_IMAGE_EXTS, recursive=True)
    if not files:
        print("[INFO] No supported images found to enhance. Exiting.")
        return 0
    print(f"\n[START] Found {len(files)} target images.")
    print("[WARNING] Images will be ENHANCED IN-PLACE (originals will be overwritten)!")
    tasks = [(f, args.verbose) for f in files]
    if args.parallel:
        workers = MAX_WORKERS or os.cpu_count() or 1
        print(f"[SYSTEM] Utilizing {workers} parallel CPU threads.")
        results = parallel_map(_auto_enhance_one, tasks, workers=workers, desc="Enhancing")
    else:
        print("[SYSTEM] Processing sequentially (default mode)...")
        results = []
        for i, task in enumerate(tasks, 1):
            path, verbose = task
            if verbose:
                print(f"[{i}/{len(tasks)}] {path.name}")
            results.append(_auto_enhance_one(task))
    ok = sum(1 for r in results if r is True)
    print(f"[FINISHED] Done. Success: {ok}/{len(files)}")
    return 0


def _downscale_one(task: tuple[Path, float]) -> tuple[Path, bool, str]:
    path, scale = task
    cv2, _ = _import_cv2()
    try:
        img = cv2.imread(str(path))
        if img is None:
            return path, False, "Failed to read image"
        h, w = img.shape[:2]
        new_w = int(w * scale)
        new_h = int(h * scale)
        if new_w < 1 or new_h < 1:
            return path, False, f"New size too small ({new_w}x{new_h})"
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        ok = cv2.imwrite(str(path), resized)
        if not ok:
            return path, False, "Failed to write image"
        return path, True, f"{w}x{h} -> {new_w}x{new_h}"
    except Exception as exc:
        return path, False, f"Error: {exc}"


def cmd_downscale(args: argparse.Namespace) -> int:
    if not 0 < args.scale_factor <= 1.0:
        print("[ERROR] Scale factor must be between 0 (exclusive) and 1.0 (inclusive)")
        return 1
    if args.scale_factor == 1.0:
        print("[WARN] Scale factor is 1.0 - no downscaling will occur")
    root = Path(args.root)
    print("-" * 40)
    print("IMAGE DOWNSCALER")
    print("-" * 40)
    print(f"[INIT] Root directory: {root.resolve()}")
    print(f"[INIT] Scale factor: {args.scale_factor} (new size = original x {args.scale_factor})")
    print(f"[INIT] CPU cores available: {os.cpu_count()}")
    exts = DEFAULT_IMAGE_EXTS | {".gif"}
    files = iter_files([root], exts, recursive=True)
    print(f"[SCAN] Found {len(files)} image file(s)")
    if not files:
        print("[WARN] No images to process!")
        return 0
    workers = MAX_WORKERS or os.cpu_count() or 1
    print(f"\n[PROCESS] Downscaling {len(files)} image(s) with {workers} process(es)...")
    tasks = [(f, args.scale_factor) for f in files]
    results = parallel_map(_downscale_one, tasks, workers=workers, desc="Downscaling")
    ok = 0
    fail = 0
    print("\n[RESULTS]")
    print("-" * 40)
    for result in results:
        if isinstance(result, Exception):
            fail += 1
            print(f"✗ FAIL  unexpected error: {result}")
            continue
        path, success, message = result
        if success:
            ok += 1
            print(f"✓ OK    {path.name:<50} {message}")
        else:
            fail += 1
            print(f"✗ FAIL  {path.name:<50} {message}")
    print("-" * 40)
    print(f"[SUMMARY] Successful: {ok} | Failed: {fail} | Total: {len(files)}")
    print("\n" + "=" * 40)
    print("PROCESS COMPLETE - Images updated in-place")
    return 0


def _run_embed_tool(cmd: list[str], name: str, timeout: int) -> bool:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            print(f"{name} failed (rc={res.returncode}): {res.stderr.strip()[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"{name} timed out after {timeout}s")
        return False
    except FileNotFoundError:
        print(f"{name}: '{cmd[0]}' not found in PATH")
        return False
    except Exception as exc:
        print(f"{name}: {exc}")
        return False


def _optimize_embedded_resource(
    mime: str,
    data: bytes,
    args: argparse.Namespace,
) -> tuple[bytes | None, str | None]:
    ext = EMBED_MIME_EXT.get(mime)
    if ext is None:
        print(f"[WARNING] Unsupported MIME type: {mime}")
        return None, None
    kind = EMBED_EXT_KIND.get(ext)
    if kind is None:
        return None, None
    fd, tmp_name = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    src = Path(tmp_name)
    src.write_bytes(data)
    temp_paths: list[Path] = [src]
    result_path = src
    result_mime = mime
    try:
        if kind == "png":
            if not _run_embed_tool([args.png_command, str(src)], "pngq", args.timeout):
                return None, None
        elif kind == "jpg":
            if not _run_embed_tool([args.jpg_command, str(src)], "jpegoptim", args.timeout):
                return None, None
        elif kind == "webp":
            jpg = src.with_suffix(".jpg")
            if not _run_embed_tool(
                [args.webp_to_jpg_command, str(src), str(jpg)],
                "to_jpg",
                args.timeout,
            ):
                return None, None
            if not jpg.exists():
                print(f"to_jpg did not produce output: {jpg}")
                return None, None
            temp_paths.append(jpg)
            if not _run_embed_tool([args.jpg_command, str(jpg)], "jpegoptim", args.timeout):
                return None, None
            result_path = jpg
            result_mime = "image/jpeg"
        elif kind == "svg":
            if not _run_embed_tool(
                [args.svg_command, "-i", str(src), "-o", str(src)],
                "svgo",
                args.timeout,
            ):
                return None, None
        elif kind == "css":
            if not _run_embed_tool([args.css_command, str(src)], "ccss", args.timeout):
                return None, None
        elif kind == "js" and not _run_embed_tool([args.js_command, str(src)], "ter_ser", args.timeout):
            return None, None
        if not result_path.exists():
            print(f"Result file missing after optimization: {result_path}")
            return None, None
        return result_path.read_bytes(), result_mime
    finally:
        for f in temp_paths:
            with contextlib.suppress(OSError):
                f.unlink(missing_ok=True)


def _process_embed_file(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "file": path,
        "original_size": 0,
        "new_size": 0,
        "resources_found": 0,
        "resources_optimized": 0,
        "space_freed": 0,
        "error": None,
    }
    try:
        raw = path.read_bytes()
        original_size = len(raw)
        stats["original_size"] = original_size
        text = raw.decode("utf-8", errors="replace")
        matches = list(EMBED_DATA_RE.finditer(text))
        stats["resources_found"] = len(matches)
        if not matches:
            return stats
        parts: list[str] = []
        last = 0
        optimized = 0
        for match in matches:
            parts.append(text[last : match.start()])
            mime = match.group("mime")
            b64 = match.group("data")
            try:
                data = base64.b64decode(b64)
            except Exception as exc:
                print(f"[WARNING] Base64 decode failed in {path.name}: {exc}")
                parts.append(match.group(0))
                last = match.end()
                continue
            is_webp = EMBED_MIME_EXT.get(mime) == ".webp"
            new_data, new_mime = _optimize_embedded_resource(mime, data, args)
            if new_data is not None and (is_webp or len(new_data) < len(data)):
                new_b64 = base64.b64encode(new_data).decode("ascii")
                parts.append(f"data:{new_mime};base64,{new_b64}")
                optimized += 1
            else:
                parts.append(match.group(0))
                if new_data is not None and not is_webp:
                    print(f"[DEBUG] No size improvement for {mime} in {path.name} ({len(new_data)} >= {len(data)})")
            last = match.end()
        parts.append(text[last:])
        if optimized > 0:
            new_text = "".join(parts)
            new_bytes = new_text.encode("utf-8")
            path.write_bytes(new_bytes)
            stats["new_size"] = len(new_bytes)
            stats["resources_optimized"] = optimized
            stats["space_freed"] = original_size - len(new_bytes)
        else:
            stats["new_size"] = original_size
    except Exception as exc:
        print(f"[ERROR] Error processing {path}: {exc}")
        stats["error"] = str(exc)
    return stats


def _process_embed_file_task(task: tuple[Path, argparse.Namespace]) -> dict[str, Any]:
    path, args = task
    return _process_embed_file(path, args)


def collect_embed_files(paths: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_file() and p.suffix.lower() in DEFAULT_EMBED_EXTS:
            found.append(p)
        elif p.is_dir():
            for ext in DEFAULT_EMBED_EXTS:
                found.extend(p.rglob(f"*{ext}"))
                found.extend(p.rglob(f"*{ext.upper()}"))
        else:
            print(f"[WARNING] Path not found or unsupported: {p}")
    seen: set[Path] = set()
    out: list[Path] = []
    for f in found:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            out.append(f)
    return out


def _print_embed_result(stats: dict[str, Any]) -> None:
    name = stats["file"].name
    if stats["error"]:
        print(f"  ✗ {name} — ERROR: {stats['error']}")
        return
    found = stats["resources_found"]
    optimized = stats["resources_optimized"]
    freed = stats["space_freed"]
    if found == 0:
        print(f"  · {name} — no embedded resources")
    elif optimized == 0:
        print(f"  · {name} — {found} resource(s), none optimized")
    else:
        print(f"  ✓ {name} — {optimized}/{found} optimized, freed {human_size(freed)}")


def cmd_embed_optimize(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    files = collect_embed_files(inputs)
    if not files:
        print("No CSS/HTML/JS files found.")
        return 0
    print(f"Found {len(files)} file(s) to process.\n")
    if args.dry_run:
        for f in files:
            print(f"  {f}")
        return 0
    tasks = [(f, args) for f in files]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_process_embed_file_task, task) for task in tasks]
        iterator: Iterable[Any] = as_completed(futures)
        if tqdm is not None:
            iterator = tqdm(iterator, total=len(futures), desc="Embed", unit="file", ncols=80)
        for fut in iterator:
            stats = fut.result()
            results.append(stats)
            _print_embed_result(stats)
    errors = sum(1 for s in results if s["error"])
    found = sum(s["resources_found"] for s in results)
    optimized = sum(s["resources_optimized"] for s in results)
    freed = sum(s["space_freed"] for s in results)
    print("\n" + "=" * 40)
    print("Summary")
    print("-" * 40)
    print(f"  Files processed      : {len(results)}")
    print(f"  Errors               : {errors}")
    print(f"  Resources found      : {found}")
    print(f"  Resources optimized  : {optimized}")
    print(f"  Total space freed    : {human_size(freed)}")
    print("=" * 40)
    return 0


def _optimize_png_one(
    task: tuple[Path, str, str, str, bool],
) -> tuple[Path, bool, str | int]:
    path, tool, optipng_args, oxipng_args, show_output = task
    try:
        original_size = path.stat().st_size
        if tool == "optipng":
            cmd = ["optipng", *optipng_args.split(), str(path)]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError:
                return path, False, "optipng not found"
            if res.returncode != 0:
                return path, False, f"optipng rc={res.returncode}: {res.stderr[:200]}"
            output = (res.stdout or "") + (res.stderr or "")
            if "skipping" in output.lower():
                return path, False, "skipped"
            if show_output:
                print(output.strip())
        else:
            cmd = ["oxipng", *oxipng_args.split(), str(path)]
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                return path, False, "oxipng not found"
            except subprocess.CalledProcessError:
                return path, False, "oxipng failed"
        new_size = path.stat().st_size
        return path, True, original_size - new_size
    except Exception as exc:
        return path, False, str(exc)


def cmd_optimize_png(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    files = iter_files(inputs, DEFAULT_PNG_EXTS, recursive=args.recursive)
    if not files:
        print("No PNG files found.")
        return 0
    print(f"Found {len(files)} PNG files to optimize.")
    tasks = [(f, args.tool, args.optipng_args, args.oxipng_args, args.show_output) for f in files]
    workers = MAX_WORKERS or os.cpu_count() or 1
    results = parallel_map(_optimize_png_one, tasks, workers=workers, desc="Optimizing PNGs")
    ok = 0
    fail = 0
    total_freed = 0
    for result in results:
        if isinstance(result, Exception):
            fail += 1
            print(f"✗ FAIL  unexpected error: {result}")
            continue
        path, success, info = result
        if success:
            ok += 1
            freed = int(info)
            total_freed += freed
            print(f"✅ {path.name} — freed {human_size(freed)}")
        else:
            fail += 1
            print(f"✗ {path.name} — {info}")
    print(f"\nOptimization complete. Success: {ok}/{len(files)} files.")
    print(f"Total space freed: {total_freed / 1048576:.2f} MB")
    return 0


def _pil_enhance_one(
    task: tuple[Path, float, float, float, float],
) -> bool:
    path, contrast, brightness, sharpness, color = task
    Image, ImageEnhance = _import_pil()
    try:
        with Image.open(path) as img:
            img = ImageEnhance.Contrast(img).enhance(contrast)
            img = ImageEnhance.Brightness(img).enhance(brightness)
            img = ImageEnhance.Sharpness(img).enhance(sharpness)
            img = ImageEnhance.Color(img).enhance(color)
            img.save(path)
        print(f"Enhanced: {path.name}")
        return True
    except Exception as exc:
        print(f"Error enhancing {path.name}: {exc}")
        return False


def cmd_pil_enhance(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    exts = parse_extensions(args.extensions)
    files = iter_files(inputs, exts, recursive=args.recursive)
    if not files:
        print("No image files found.")
        return 0
    print(f"Found {len(files)} image file(s) to process...")
    tasks = [(f, args.contrast, args.brightness, args.sharpness, args.color) for f in files]
    workers = MAX_WORKERS or os.cpu_count() or 1
    parallel_map(_pil_enhance_one, tasks, workers=workers, desc="Enhancing")
    print("All images processed!")
    return 0


def _resize_one(task: tuple[Path, float, int]) -> bool:
    path, scale, quality = task
    Image, _ = _import_pil()
    try:
        with Image.open(path) as img:
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            resized.save(path, optimize=True, quality=quality)
        print(f"Reduced: {path} ({img.width}x{img.height} -> {new_w}x{new_h})")
        return True
    except Exception as exc:
        print(f"Error processing {path}: {exc}")
        return False


def cmd_resize(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    exts = parse_extensions(args.extensions)
    files = iter_files(inputs, exts, recursive=args.recursive)
    if not files:
        print("No image files found in current directory.")
        return 0
    print(f"Found {len(files)} image file(s) to process...")
    tasks = [(f, args.scale, args.quality) for f in files]
    workers = MAX_WORKERS or os.cpu_count() or 1
    parallel_map(_resize_one, tasks, workers=workers, desc="Resizing")
    print("All images processed!")
    return 0


def _strip_exif_one(task: tuple[Path, bool, bool]) -> dict[str, Any]:
    path, backup, verbose = task
    Image, _ = _import_pil()
    stats: dict[str, Any] = {
        "path": path,
        "success": False,
        "original_size": 0,
        "new_size": 0,
        "message": "",
        "backup_created": False,
    }
    try:
        original_size = path.stat().st_size
        stats["original_size"] = original_size
        if backup:
            backup_path = path.with_suffix(path.suffix + ".backup")
            backup_path.write_bytes(path.read_bytes())
            stats["backup_created"] = True
            if verbose:
                print(f"📋 Backup: {backup_path.name}")
        with Image.open(path) as img:
            clean = Image.new(img.mode, img.size)
            clean.putdata(list(img.getdata()))
            buf = io.BytesIO()
            save_kwargs: dict[str, Any] = {}
            if img.format == "JPEG":
                save_kwargs.update(quality=95, optimize=True)
            elif img.format == "PNG":
                save_kwargs.update(optimize=True)
            try:
                clean.save(buf, format=img.format, exif=None, **save_kwargs)
            except TypeError:
                clean.save(buf, format=img.format, **save_kwargs)
            new_bytes = buf.getvalue()
            path.write_bytes(new_bytes)
            stats["new_size"] = len(new_bytes)
            stats["success"] = True
            delta = len(new_bytes) - original_size
            pct = delta / original_size * 100 if original_size else 0.0
            stats["message"] = f"Stripped EXIF: {delta:+.0f}B ({pct:+.1f}%)"
            if verbose:
                print(f"✅ {path.name}")
                print(f"   {human_size(original_size)} -> {human_size(len(new_bytes))} ({pct:+.1f}%)")
    except Exception as exc:
        stats["success"] = False
        stats["message"] = f"Error: {exc}"
        if verbose:
            print(f"❌ {path.name}: {exc}")
    return stats


def cmd_strip_exif(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.paths] if args.paths else [Path()]
    extensions = parse_extensions(args.extensions)
    recursive = not args.no_recursive
    files = iter_files(paths, extensions, recursive=recursive)
    if not files:
        print("ℹ️  No image files found.")
        return 0
    size_before: dict[Path, int] = {}
    if not args.no_size_report:
        parents = {f.parent for f in files}
        for parent in parents:
            size_before[parent] = dir_size(parent)
    print(f"📸 Found {len(files)} image file(s)")
    print(f"🔧 Using {MAX_WORKERS} parallel worker(s)")
    print(f"💾 Backup: {'Yes' if args.backup else 'No'}")
    print(f"📁 Recursive: {'Yes' if recursive else 'No'}")
    print("-" * 40)
    tasks = [(f, args.backup, args.verbose) for f in files]
    results = parallel_map(_strip_exif_one, tasks, workers=MAX_WORKERS, desc="Stripping EXIF")
    ok = 0
    fail = 0
    total_original = 0
    total_new = 0
    for i, result in enumerate(results, 1):
        if isinstance(result, Exception):
            fail += 1
            print(f"❌ unexpected error: {result}")
            continue
        total_original += result["original_size"]
        total_new += result["new_size"]
        if result["success"]:
            ok += 1
            if not args.verbose:
                print(f"  [{i}/{len(files)}] ✅ {result['path'].name}")
        else:
            fail += 1
            if not args.verbose:
                print(f"  [{i}/{len(files)}] ❌ {result['path'].name}: {result['message']}")
    print("-" * 40)
    delta = total_new - total_original
    print("📊 Summary:")
    print(f"   Total files: {len(files)}")
    print(f"   ✅ Successful: {ok}")
    print(f"   ❌ Failed: {fail}")
    print(f"   📦 Original size: {human_size(total_original)}")
    print(f"   📦 New size: {human_size(total_new)}")
    if total_original > 0:
        print(f"   💰 Change: {human_size(delta)} ({delta / total_original * 100:+.1f}%)")
    else:
        print(f"   💰 Change: {human_size(delta)} (N/A)")
    if not args.no_size_report and size_before:
        print("📁 Folder size changes:")
        for parent in sorted(size_before):
            before = size_before[parent]
            after = dir_size(parent)
            change = after - before
            if change != 0:
                pct = change / before * 100 if before > 0 else 0.0
                print(f"   {parent}:")
                print(f"      {human_size(before)} -> {human_size(after)} ({pct:+.1f}%)")
    backups = [r for r in results if isinstance(r, dict) and r.get("backup_created")]
    if backups:
        print(f"💾 Backups created for {len(backups)} file(s)")
    return 0


def _upscale_one(path: Path) -> bool:
    cv2, _ = _import_cv2()
    try:
        img = cv2.imread(str(path))
        if img is None or not img.any():
            return False
        h, w = img.shape[:2]
        factor = 0
        if 1 < w < 200:
            factor = 8
        elif 200 <= w <= 500:
            factor = 6
        elif 500 <= w <= 1000:
            factor = 4
        elif 1000 <= w <= 2000:
            factor = 2
        elif w > 2000:
            return False
        if factor == 0:
            return False
        print(f"[✓] {path.name}: {h}x{w} -> {h * factor}x{w * factor}")
        resized = cv2.resize(
            img,
            (w * factor, h * factor),
            interpolation=cv2.INTER_LANCZOS4,
        )
        sharp = cv2.addWeighted(resized, 1.5, resized, -0.5, 0)
        cv2.imwrite(str(path), sharp)
        return True
    except Exception:
        return False


def cmd_upscale(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    exts = parse_extensions(args.extensions)
    files = iter_files(inputs, exts, recursive=args.recursive)
    if not files:
        print("No image files found.")
        return 0
    for i, f in enumerate(files, 1):
        print(f"{i}/{len(files)}")
        _upscale_one(f)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imgtool.py",
        description="Merged image/media optimization toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("auto-enhance", help="Google-Photos-style in-place image enhancement")
    p.add_argument("inputs", nargs="*", help="Files or folders. Defaults to recursive '.'.")
    p.add_argument("-v", "--verbose", action="store_true", help="Print per-image details.")
    p.add_argument("--parallel", action="store_true", help="Enable multiprocessing.")
    p.set_defaults(func=cmd_auto_enhance)
    p = sub.add_parser("downscale", help="Downscale images in-place by a scale factor")
    p.add_argument(
        "scale_factor",
        nargs="?",
        type=float,
        default=0.5,
        help="Scale factor (0 < f <= 1.0).",
    )
    p.add_argument(
        "--root",
        default=".",
        help="Root directory to scan. Default: current directory.",
    )
    p.set_defaults(func=cmd_downscale)
    p = sub.add_parser("embed-optimize", help="Optimize base64 resources inside CSS/HTML/JS")
    p.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Files or directories. Default: current directory.",
    )
    p.add_argument("--dry-run", action="store_true", help="List files without processing.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    p.add_argument("--timeout", type=int, default=300, help="External command timeout in seconds.")
    p.add_argument("--png-command", default="pngq", help="PNG optimizer command.")
    p.add_argument("--jpg-command", default="jpegoptim", help="JPEG optimizer command.")
    p.add_argument("--webp-to-jpg-command", default="to_jpg", help="WebP-to-JPEG command.")
    p.add_argument("--svg-command", default="svgo", help="SVG optimizer command.")
    p.add_argument("--js-command", default="ter_ser", help="JS optimizer command.")
    p.add_argument("--css-command", default="ccss", help="CSS optimizer command.")
    p.set_defaults(func=cmd_embed_optimize)
    p = sub.add_parser("optimize-png", help="Optimize PNG files with optipng or oxipng")
    p.add_argument("inputs", nargs="*", help="Files or directories. Default: current directory.")
    p.add_argument(
        "--tool",
        choices=["optipng", "oxipng"],
        default="optipng",
        help="PNG optimizer tool.",
    )
    p.add_argument("--optipng-args", default="-o7", help="Arguments for optipng.")
    p.add_argument(
        "--oxipng-args",
        default="-o max --quiet --strip safe --force",
        help="Arguments for oxipng.",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        default=True,
        help="Recurse into directories.",
    )
    p.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse.")
    p.add_argument("--show-output", action="store_true", help="Show tool output.")
    p.set_defaults(func=cmd_optimize_png)
    p = sub.add_parser("pil-enhance", help="PIL ImageEnhance in-place enhancement")
    p.add_argument("inputs", nargs="*", help="Files or directories. Default: current directory.")
    p.add_argument("--contrast", type=float, default=1.1, help="Contrast factor.")
    p.add_argument("--brightness", type=float, default=1.1, help="Brightness factor.")
    p.add_argument("--sharpness", type=float, default=1.1, help="Sharpness factor.")
    p.add_argument("--color", type=float, default=1.1, help="Color factor.")
    p.add_argument("--extensions", default=".jpg,.png,.webp", help="Comma-separated extensions.")
    p.add_argument(
        "--recursive",
        action="store_true",
        default=True,
        help="Recurse into directories.",
    )
    p.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse.")
    p.set_defaults(func=cmd_pil_enhance)
    p = sub.add_parser("resize", help="PIL in-place downscale by scale factor")
    p.add_argument("inputs", nargs="*", help="Files or directories. Default: current directory.")
    p.add_argument("--scale", type=float, default=0.75, help="Scale factor. Default: 0.75.")
    p.add_argument("--quality", type=int, default=85, help="JPEG quality. Default: 85.")
    p.add_argument(
        "--extensions",
        default=".jpg,.jpeg,.png,.webp,.bmp,.tiff",
        help="Comma-separated extensions.",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        default=False,
        help="Recurse into directories.",
    )
    p.set_defaults(func=cmd_resize)
    p = sub.add_parser("strip-exif", help="Strip EXIF data from images")
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories. Default: current directory.",
    )
    p.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="Create .backup files before stripping.",
    )
    p.add_argument("--no-recursive", action="store_true", help="Do not process subdirectories.")
    p.add_argument(
        "--extensions",
        nargs="+",
        default=sorted(DEFAULT_IMAGE_EXTS),
        help="Extensions to process.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Show detailed output.")
    p.add_argument("--no-size-report", action="store_true", help="Skip folder size change report.")
    p.set_defaults(func=cmd_strip_exif)
    p = sub.add_parser("upscale", help="Upscale small images by width-based factors")
    p.add_argument("inputs", nargs="*", help="Files or directories. Default: current directory.")
    p.add_argument(
        "--extensions",
        default=".webp,.jpg,.jpeg,.png",
        help="Comma-separated extensions.",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        default=True,
        help="Recurse into directories.",
    )
    p.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse.")
    p.set_defaults(func=cmd_upscale)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
