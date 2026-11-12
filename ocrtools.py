#!/data/data/com.termux/files/usr/bin/env python
"""OCR and image pre-processing toolkit.
Subcommands: ``enhance``/``prepare`` binarise and deskew the images for Tesseract, ``grid-variants``/``grid-search`` try the image variants and the oem/psm settings, ``ocr`` extracts the text (plus a JSON report and CSV with ``-j``) and ``translate`` OCRs and translates the result.
Usage: ocrtools {enhance,prepare,grid-variants,grid-search,ocr,translate} [options] [paths ...] Positional arguments: paths ...
Image files or directories (default: cwd).
image input_path Subcommands: enhance Binarise/enhance images, saving alongside originals (image2text.py).
prepare Prepare images for Tesseract OCR, in-place (ocr_prepare.py).
grid-variants Tesseract grid over 5 image variants (ocrgrid.py).
grid-search Tesseract grid over oem/psm on preprocessed images (ocrgrid2.py).
ocr Extract text from image(s) (pyocr.py / ruimg.py).
translate OCR a text/image file and translate it to English (transocr.py).
Options: --backend --variants Output suffix set.
'auto' = pil for one file, both for many (matches image2text.py).
-r, --recursive Process subdirectories recursively.
-v, --verbose Enable DEBUG logging (requires loguru).
-o, --out --psm --oem --dpi --lang Tesseract language(s) (default: eng).
-l, --lang Tesseract language(s), e.g.
'eng' or 'rus+eng'.
Default: tesseract default (pyocr.py behaviour).
...
plus 3 more; run ``ocrtools --help`` for the full list.
Examples: $ ocrtools ocr scans/ -l eng -j $ ocrtools grid-search page.png Command-line script: run ``python ocrtools.py`` or the ``ocrtools`` command.
The module is importable as well."""

import argparse
import csv
from datetime import datetime
from itertools import product
import json
from multiprocessing import Pool, cpu_count
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Sequence

from dh import MAX_WORKERS
from loguru import logger

try:
    import cv2

    HAS_CV2 = True
except ImportError:
    cv2 = None
    HAS_CV2 = False
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False
try:
    from PIL import Image, ImageEnhance, ImageFilter

    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False
try:
    from skimage import (
        color as skcolor,
        filters as skfilters,
        io as skiio,
    )
    from skimage.filters import threshold_local
    from skimage.util import img_as_ubyte

    HAS_SKIMAGE = True
except ImportError:
    HAS_SKIMAGE = False
try:
    import pytesseract

    HAS_TESS = True
except ImportError:
    pytesseract = None
    HAS_TESS = False
try:
    from loguru import logger

    HAS_LOGURU = True
except ImportError:
    HAS_LOGURU = False

    class _FallbackLogger:
        def debug(self, m: Any, *a: Any, **k: Any) -> None:
            print(f"[DEBUG] {m}")

        def info(self, m: Any, *a: Any, **k: Any) -> None:
            print(f"[INFO] {m}")

        def warning(self, m: Any, *a: Any, **k: Any) -> None:
            print(f"[WARN] {m}", file=sys.stderr)

        def error(self, m: Any, *a: Any, **k: Any) -> None:
            print(f"[ERROR] {m}", file=sys.stderr)

        def remove(self) -> None:
            pass

        def add(self, *a: Any, **k: Any) -> None:
            pass

    logger = _FallbackLogger()
IMAGE_EXT: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tiff",
    ".tif",
    ".bmp",
    ".gif",
    ".webp",
}
TEXT_EXT: set[str] = {".txt", ".md", ".csv", ".json", ".py"}
PHOTO_EXT: set[str] = {".jpg", ".jpeg", ".png"}
DEFAULT_GRID_VARIANTS_PSM: list[int] = [3, 4, 6, 11]
DEFAULT_GRID_VARIANTS_OEM: list[int] = [1, 3]
DEFAULT_GRID_VARIANTS_DPI: list[int] = [150, 300]
DEFAULT_GRID_SEARCH_OEM: list[int] = [0, 1, 2, 3]
DEFAULT_GRID_SEARCH_PSM: list[int] = [3, 4, 6, 11, 12, 13]
DEFAULT_TRANSLATE_CHUNK: int = 32768


class AppError(RuntimeError):
    pass


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _pmap(fn: Callable[[Any], Any], items: Sequence[Any], workers: int | None = None) -> list[Any]:
    items = list(items)
    if not items:
        return []
    desired = workers or cpu_count() or 1
    n = max(1, min(desired, len(items)))
    if n <= 1 or len(items) == 1:
        return [fn(i) for i in items]
    with Pool(processes=n) as pool:
        return pool.map(fn, items)


def find_images(
    paths: Iterable[Path],
    *,
    recursive: bool = False,
    extensions: set[str] = IMAGE_EXT,
) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in extensions and p not in seen:
                seen.add(p)
                out.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in it:
                if f.is_file() and f.suffix.lower() in extensions and f not in seen:
                    seen.add(f)
                    out.append(f)
    return sorted(out)


def _pick_backend(pref: str) -> str:
    if pref == "auto":
        if HAS_CV2:
            return "cv"
        if HAS_SKIMAGE:
            return "skimage"
        if HAS_PIL:
            return "pillow"
        msg = "no image backend available (need OpenCV, scikit-image or Pillow)"
        raise AppError(msg)
    if pref == "cv" and not HAS_CV2:
        msg = "OpenCV is not installed"
        raise AppError(msg)
    if pref == "skimage" and not HAS_SKIMAGE:
        msg = "scikit-image is not installed"
        raise AppError(msg)
    if pref == "pillow" and not HAS_PIL:
        msg = "Pillow is not installed"
        raise AppError(msg)
    return pref


def _require_tesseract() -> None:
    if not HAS_TESS:
        msg = "pytesseract is required for this command"
        raise AppError(msg)


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _enhance_cv(path: Path, suffix: str) -> bool:
    img = cv2.imread(str(path))
    if img is None:
        logger.error(f"could not read image: {path}")
        return False
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    h = cv2.GaussianBlur(g, (0, 0), 3)
    i = cv2.addWeighted(g, 1.5, h, -0.5, 0)
    j = cv2.adaptiveThreshold(i, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    out = path.with_stem(path.stem + suffix)
    cv2.imwrite(str(out), j)
    return True


def _enhance_skimage(path: Path, suffix: str) -> bool:
    try:
        arr = skiio.imread(str(path))
    except Exception as e:
        logger.error(f"could not read image {path}: {e}")
        return False
    f = skcolor.rgb2gray(arr) if arr.ndim == 3 else arr
    g = skfilters.gaussian(f, sigma=5 / 3)
    h = skfilters.gaussian(g, sigma=1.0)
    i = np.clip(1.5 * g - 0.5 * h, 0, 1)
    j = i > threshold_local(i, 11, "gaussian")
    n = img_as_ubyte(j)
    out = path.with_stem(path.stem + suffix)
    skiio.imsave(str(out), n)
    return True


def _enhance_one(path: Path, backend: str, suffix: str) -> bool:
    if backend == "cv":
        return _enhance_cv(path, suffix)
    if backend == "skimage":
        return _enhance_skimage(path, suffix)
    msg = f"unsupported backend for enhance: {backend}"
    raise AppError(msg)


def cmd_enhance(args: argparse.Namespace) -> int:
    backend = _pick_backend(args.backend)
    inputs = args.paths or [Path.cwd()]
    files = find_images(inputs, recursive=False)
    if not files:
        print("no image files found to process")
        return 0
    variants = args.variants
    if variants == "auto":
        variants = "pil" if len(files) == 1 else "both"
    before = _dir_size(Path.cwd())

    def work(f: Path) -> bool:
        ok = True
        if variants in ("pil", "both"):
            ok &= _enhance_one(f, backend, "_enhanced_pil")
        if variants in ("cv", "both"):
            ok &= _enhance_one(f, backend, "_enhanced_cv")
        return ok

    _pmap(work, files, MAX_WORKERS)
    after = _dir_size(Path.cwd())
    print(f"space saved: {_fmt_size(before - after)}")
    return 0


def _prepare_cv(path: Path) -> bool:
    try:
        img = cv2.imread(str(path))
        if img is None:
            logger.error(f"failed to read image: {path}")
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(gray, (5, 5), 0)
        y = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
        z = cv2.fastNlMeansDenoising(y, None, 10, 7, 21)
        cv2.imwrite(str(path), z)
        return True
    except Exception as e:
        logger.error(f"error processing {path}: {e}")
        return False


def _prepare_pillow(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            if img.mode != "L":
                img = img.convert("L")
            img = ImageEnhance.Contrast(img).enhance(2.0)
            img = ImageEnhance.Sharpness(img).enhance(2.0)
            img = img.filter(ImageFilter.GaussianBlur(radius=0.5))
            img = img.point(lambda p: 255 if p > 128 else 0)
            img.save(str(path))
        return True
    except Exception as e:
        logger.error(f"error processing {path}: {e}")
        return False


def cmd_prepare(args: argparse.Namespace) -> int:
    if args.verbose and HAS_LOGURU:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    backend = _pick_backend(args.backend)
    inputs = args.paths or [Path.cwd()]
    if not args.paths:
        print(f"no input specified, processing current directory: {Path.cwd()}")
    files = find_images(inputs, recursive=args.recursive)
    if not files:
        logger.error("no supported image files found")
        print(f"supported extensions: {', '.join(sorted(IMAGE_EXT))}")
        return 1
    print(f"found {len(files)} image(s) to process")
    worker = _prepare_cv if backend == "cv" else _prepare_pillow
    results = _pmap(worker, files, MAX_WORKERS)
    ok = sum(1 for r in results if r)
    fail = len(results) - ok
    print("=" * 40)
    print("processing complete:")
    print(f"  ✓ success: {ok}")
    print(f"  ✗ failed:  {fail}")
    print(f"  total:     {len(results)}")
    return 0 if fail == 0 else 1


def _resize_scale(bgr: "np.ndarray", scale: float) -> "np.ndarray":
    h, w = bgr.shape[:2]
    return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)


def _rotate_center(bgr: "np.ndarray", angle: float) -> "np.ndarray":
    h, w = bgr.shape[:2]
    m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _deskew_min_area_rect(bgr: "np.ndarray") -> "np.ndarray":
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    coords = np.column_stack(np.where(gray > 0))
    if coords.size == 0:
        return bgr
    angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    return _rotate_center(bgr, angle)


def cmd_grid_variants(args: argparse.Namespace) -> int:
    if not (HAS_CV2 and HAS_NUMPY):
        msg = "grid-variants requires OpenCV + numpy"
        raise AppError(msg)
    _require_tesseract()
    fname: Path = args.image
    if not fname.is_file():
        msg = f"not a file: {fname}"
        raise AppError(msg)
    out_root: Path = args.out
    out_root.mkdir(parents=True, exist_ok=True)
    pil_img = Image.open(fname).convert("RGB")
    bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    variants: dict[str, "Image.Image"] = {
        "original": pil_img,
        "grayscale": Image.fromarray(gray),
        "rescaled": Image.fromarray(cv2.cvtColor(_resize_scale(bgr, 2.0), cv2.COLOR_BGR2RGB)),
        "deskewed": Image.fromarray(cv2.cvtColor(_deskew_min_area_rect(bgr), cv2.COLOR_BGR2RGB)),
        "rotated_90": Image.fromarray(cv2.cvtColor(_rotate_center(bgr, 90), cv2.COLOR_BGR2RGB)),
    }
    index: list[dict[str, Any]] = []
    for name, img in variants.items():
        d = out_root / name
        d.mkdir(exist_ok=True)
        for psm, oem, dpi in product(args.psm, args.oem, args.dpi):
            config = f"--psm {psm} --oem {oem} -c user_defined_dpi={dpi}"
            text = pytesseract.image_to_string(img, config=config)
            tag = f"psm{psm}_oem{oem}_dpi{dpi}"
            (d / f"{tag}.txt").write_text(text, encoding="utf-8")
            (d / f"{tag}.json").write_text(
                json.dumps(
                    {
                        "image_variant": name,
                        "source_file": str(fname),
                        "tesseract": {
                            "psm": psm,
                            "oem": oem,
                            "dpi": dpi,
                            "config": config,
                            "text": text,
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            index.append({
                "variant": name,
                "psm": psm,
                "oem": oem,
                "dpi": dpi,
                "text_file": str(d / f"{tag}.txt"),
            })
    (out_root / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"done: {len(index)} runs -> {out_root}")
    return 0


def _grid_search_preprocess(path: Path) -> "np.ndarray":
    img = cv2.imread(str(path))
    if img is None:
        msg = f"could not read image: {path}"
        raise AppError(msg)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, h=15)
    bw = cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2)
    coords = cv2.findNonZero(bw)
    if coords is not None:
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        if angle < -45:
            angle += 90
        h, w = bw.shape
        m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        bw = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_CUBIC)
    return bw


def cmd_grid_search(args: argparse.Namespace) -> int:
    if not HAS_CV2:
        msg = "grid-search requires OpenCV"
        raise AppError(msg)
    _require_tesseract()
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = [Path(p) for p in args.paths] if args.paths else [Path.cwd()]
    files = find_images(inputs, recursive=False)
    if not files:
        print("no images found")
        return 0
    rows: list[dict[str, Any]] = []
    for w2 in files:
        print(f"processing: {w2}")
        try:
            processed = _grid_search_preprocess(w2)
        except AppError as e:
            print(f"  ✗ {e}")
            continue
        for oem, psm in product(args.oem, args.psm):
            config = f"--oem {oem} --psm {psm} -l {args.lang}"
            t0 = time.time()
            err = ""
            try:
                text = pytesseract.image_to_string(processed, config=config)
            except Exception as e:
                text = ""
                err = str(e)
            elapsed = time.time() - t0
            (out_dir / f"{w2.stem}__oem{oem}_psm{psm}.txt").write_text(text, encoding="utf-8")
            rows.append({
                "image": w2.name,
                "config": config,
                "oem": oem,
                "psm": psm,
                "duration_sec": elapsed,
                "error": err,
                "text": text,
            })
    _write_csv(rows, out_dir / "ocr_summary.csv")
    print(f"\ndone. all results saved in: {out_dir}")
    return 0


def _ocr_single(path: Path, lang: str | None) -> bool:
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXT:
        print(f"error: '{path.name}' is not a supported image file")
        return False
    try:
        if HAS_CV2:
            img = cv2.imread(str(path))
            if img is None:
                print(f"error: could not read {path.name}")
                return False
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            img = Image.open(path).convert("L")
        print(f"processing '{path.name}'...")
        cfg = f"-l {lang}" if lang else ""
        text = pytesseract.image_to_string(img, config=cfg)
        if not text.strip():
            print(f"warning: no text detected in '{path.name}'")
        out = path.with_suffix(".txt")
        out.write_text(text, encoding="utf-8")
        print(f"success! text saved to '{out.name}'")
        return True
    except Exception as e:
        print(f"error: {e}")
        return False


def _ocr_file(path: Path, lang: str | None) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            cfg = f"-l {lang}" if lang else ""
            text = pytesseract.image_to_string(im, config=cfg)
        out = path.with_suffix(".txt")
        out.write_text(text, encoding="utf-8")
        chars = len(text)
        lines = len(text.strip().split("\n")) if text.strip() else 0
        return {
            "file": str(path),
            "success": True,
            "char_count": chars,
            "line_count": lines,
            "error": None,
            "preview": text[:200],
        }
    except Exception as e:
        return {
            "file": str(path),
            "success": False,
            "char_count": 0,
            "line_count": 0,
            "error": str(e),
            "preview": "",
        }


def cmd_ocr(args: argparse.Namespace) -> int:
    _require_tesseract()
    if args.verbose if hasattr(args, "verbose") else False:
        pass
    inputs = [Path(p) for p in args.paths] if args.paths else [Path.cwd()]
    files = find_images(inputs, recursive=args.recursive)
    if not files:
        print("no images found")
        return 1
    if len(files) == 1:
        return 0 if _ocr_single(files[0], args.lang) else 1
    workers = MAX_WORKERS or cpu_count()
    print(f"processing {len(files)} image(s) with {workers} worker(s)")
    results = _pmap(lambda f: _ocr_file(f, args.lang), files, workers)
    ok = sum(1 for r in results if r["success"])
    fail = len(results) - ok
    if not args.silent:
        for r in results:
            if r["success"]:
                print(f"✓ {r['file']}  chars={r['char_count']} lines={r['line_count']}")
            else:
                print(f"✗ {r['file']}  error={r['error']}")
    print("=" * 40)
    print("summary:")
    print(f"  ✓ successful: {ok}/{len(results)}")
    print(f"  ✗ failed:     {fail}/{len(results)}")
    print(f"  chars total:  {sum(r['char_count'] for r in results):,}")
    print(f"  lines total:  {sum(r['line_count'] for r in results):,}")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "total_files": len(results),
                    "successful": ok,
                    "failed": fail,
                    "results": results,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"detailed report saved to: {args.json}")
    return 0 if fail == 0 else 1


def _translate_preprocess_pil(img: "Image.Image") -> "Image.Image":
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.point(lambda x: 0 if x < 160 else 255)
    return img.filter(ImageFilter.MedianFilter(size=3))


def _detect_lang(text: str) -> str:
    if not (stripped := text.strip()):
        return "unknown"
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(stripped[:500])
    except Exception:
        return "unknown"


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def cmd_translate(args: argparse.Namespace) -> int:
    from deep_translator import GoogleTranslator

    p: Path = args.input_path
    if not p.exists():
        msg = f"file not found: {p}"
        raise AppError(msg)
    suffix = p.suffix.lower()
    ocr_written: Path | None = None
    if suffix in TEXT_EXT:
        text = p.read_text(encoding="utf-8")
    elif suffix in PHOTO_EXT:
        if not (HAS_PIL and HAS_TESS):
            msg = "translate on images requires Pillow and pytesseract"
            raise AppError(msg)
        with Image.open(p) as im:
            prepped = _translate_preprocess_pil(im)
            text = pytesseract.image_to_string(prepped)
        ocr_written = p.with_name(f"{p.stem}_ocr.txt")
        ocr_written.write_text(text, encoding="utf-8")
    else:
        msg = f"unsupported file type: {suffix}"
        raise AppError(msg)
    lang = args.lang if args.lang != "auto" else _detect_lang(text)
    translator = GoogleTranslator(source=lang, target="en")
    translated = "".join(translator.translate(c) for c in _chunks(text, args.chunk_size))
    if suffix in PHOTO_EXT:
        out = p.with_name(f"{p.stem}_eng.txt")
    else:
        out = p.with_name(f"{p.stem}_eng{suffix}")
    out.write_text(translated, encoding="utf-8")
    if ocr_written is not None:
        print(f"OCR text saved to: {ocr_written}")
    print(f"✓ translated output saved to {out}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocr_toolkit.py",
        description="Unified OCR / image pre-processing toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples\n"
            "--------\n"
            "  python ocr_toolkit.py enhance ./images --backend cv\n"
            "  python ocr_toolkit.py prepare -r ./scans\n"
            "  python ocr_toolkit.py grid-variants page.png -o out\n"
            "  python ocr_toolkit.py grid-search ./scans -o results\n"
            "  python ocr_toolkit.py ocr page.png\n"
            "  python ocr_toolkit.py ocr ./scans -l rus+eng -w 4 -j report.json\n"
            "  python ocr_toolkit.py translate scan.png --lang auto\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pe = sub.add_parser(
        "enhance",
        help="Binarise/enhance images, saving alongside originals (image2text.py).",
    )
    pe.add_argument("paths", nargs="*", type=Path, help="Image files or directories (default: cwd).")
    pe.add_argument("--backend", choices=["auto", "cv", "skimage"], default="auto")
    pe.add_argument(
        "--variants",
        choices=["auto", "pil", "cv", "both"],
        default="auto",
        help="Output suffix set. 'auto' = pil for one file, both for many (matches image2text.py).",
    )
    pe.set_defaults(func=cmd_enhance)
    pp = sub.add_parser("prepare", help="Prepare images for Tesseract OCR, in-place (ocr_prepare.py).")
    pp.add_argument("paths", nargs="*", type=Path)
    pp.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Process subdirectories recursively.",
    )
    pp.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (requires loguru).",
    )
    pp.add_argument("--backend", choices=["auto", "cv", "pillow"], default="auto")
    pp.set_defaults(func=cmd_prepare)
    pv = sub.add_parser("grid-variants", help="Tesseract grid over 5 image variants (ocrgrid.py).")
    pv.add_argument("image", type=Path)
    pv.add_argument("-o", "--out", type=Path, default=Path("ocr_output"))
    pv.add_argument("--psm", type=int, nargs="+", default=list(DEFAULT_GRID_VARIANTS_PSM))
    pv.add_argument("--oem", type=int, nargs="+", default=list(DEFAULT_GRID_VARIANTS_OEM))
    pv.add_argument("--dpi", type=int, nargs="+", default=list(DEFAULT_GRID_VARIANTS_DPI))
    pv.set_defaults(func=cmd_grid_variants)
    ps = sub.add_parser(
        "grid-search",
        help="Tesseract grid over oem/psm on preprocessed images (ocrgrid2.py).",
    )
    ps.add_argument("paths", nargs="*", type=Path)
    ps.add_argument("-o", "--out", type=Path, default=Path("ocr_results"))
    ps.add_argument("--oem", type=int, nargs="+", default=list(DEFAULT_GRID_SEARCH_OEM))
    ps.add_argument("--psm", type=int, nargs="+", default=list(DEFAULT_GRID_SEARCH_PSM))
    ps.add_argument("--lang", default="eng", help="Tesseract language(s) (default: eng).")
    ps.set_defaults(func=cmd_grid_search)
    po = sub.add_parser("ocr", help="Extract text from image(s) (pyocr.py / ruimg.py).")
    po.add_argument("paths", nargs="*", type=Path)
    po.add_argument(
        "-l",
        "--lang",
        default=None,
        help="Tesseract language(s), e.g. 'eng' or 'rus+eng'. Default: tesseract default (pyocr.py behaviour).",
    )
    po.add_argument(
        "-j",
        "--json",
        type=Path,
        default=None,
        help="Save detailed JSON report (ruimg.py behaviour).",
    )
    po.add_argument(
        "-s",
        "--silent",
        action="store_true",
        help="Suppress per-file output (summary only).",
    )
    po.add_argument("-r", "--recursive", action="store_true", help="Walk directories recursively.")
    po.set_defaults(func=cmd_ocr)
    pt = sub.add_parser(
        "translate",
        help="OCR a text/image file and translate it to English (transocr.py).",
    )
    pt.add_argument("input_path", type=Path)
    pt.add_argument("--lang", default="auto", help="Source language code or 'auto' (default).")
    pt.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_TRANSLATE_CHUNK,
        help="Characters per translation chunk.",
    )
    pt.set_defaults(func=cmd_translate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AppError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
