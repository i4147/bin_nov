#!/data/data/com.termux/files/usr/bin/env python
"""Preprocess images so that Tesseract reads them reliably.
Deskews each scan, binarises it (OpenCV, scikit-image or Pillow depending on what is installed), removes noise and writes the cleaned image next to the original, in parallel.
A quick Tesseract pass decides which variant is kept.
Usage: ocr_ready Examples: $ ocr_ready Runs as a script (``python ocr_ready.py``) in the directory it should act on; the module can also be imported."""

import multiprocessing
from pathlib import Path
import sys
from typing import Any, Union

from loguru import logger
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract


try:
    import cv2

    HAS_CV2: bool = True
except ImportError:
    HAS_CV2 = False
try:
    from skimage import exposure, filters, restoration, transform

    HAS_SKIMAGE: bool = True
except ImportError:
    HAS_SKIMAGE = False
SUPPORTED_EXT: set[str] = {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}
BASE_DIR: Path = Path.cwd()
POOL_SIZE: int = 8
ImageType = Union[np.ndarray, "Image.Image"]


def deskew(image: np.ndarray) -> np.ndarray:
    if HAS_CV2:
        coords = np.column_stack(np.where(image > 0))
        if coords.size == 0:
            return image
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        h, w = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        return cv2.warpAffine(
            image,
            M,
            (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    if HAS_SKIMAGE:
        try:
            if image.dtype == np.uint8:
                binary = image < 128
            else:
                binary = image < 0.5
            if not binary.any():
                return image
            edges = filters.sobel(binary.astype(np.float32))
            lines = transform.probabilistic_hough_line(edges, threshold=10, line_length=50, line_gap=5)
            angles: list[float] = []
            for (x0, y0), (x1, y1) in lines:
                if x1 == x0:
                    continue
                a = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))
                if abs(a) <= 15:
                    angles.append(a)
            if not angles:
                return image
            skew = float(np.median(angles))
            if abs(skew) < 0.5:
                return image
            rotated = transform.rotate(
                image,
                -skew,
                resize=False,
                preserve_range=True,
                cval=float(image.max()),
            )
            if image.dtype == np.uint8:
                return np.clip(rotated, 0, 255).astype(np.uint8)
            return rotated.astype(image.dtype)
        except Exception as exc:
            logger.debug(f"skimage deskew skipped: {exc}")
    return image


def preprocess_image_cv2(img_path: Path) -> np.ndarray | None:
    img = cv2.imread(str(img_path))
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.fastNlMeansDenoising(gray, None, 30, 7, 21)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    thresh = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        2,
    )
    return deskew(thresh)


def preprocess_image_skimage(img_path: Path) -> np.ndarray | None:
    try:
        pil = Image.open(str(img_path))
        pil = ImageOps.exif_transpose(pil).convert("L")
        arr = np.asarray(pil, dtype=np.float32) / 255.0
    except Exception as exc:
        logger.debug(f"cannot open {img_path}: {exc}")
        return None
    try:
        h, w = arr.shape
        arr = transform.resize(
            arr,
            (h * 2, w * 2),
            order=3,
            preserve_range=True,
            anti_aliasing=True,
        )
        arr = restoration.denoise_tv_chambolle(arr, weight=0.05)
        arr = exposure.equalize_adapthist(arr, clip_limit=0.025, kernel_size=8)
        thr = filters.threshold_local(arr, block_size=31, offset=0.01)
        binary = (arr > thr).astype(np.uint8) * 255
        return deskew(binary)
    except Exception as exc:
        logger.debug(f"skimage preprocess failed for {img_path}: {exc}")
        return None


def preprocess_image_pillow(img_path: Path) -> Image.Image | None:
    try:
        img = Image.open(str(img_path))
        img = ImageOps.exif_transpose(img)
        if img.mode != "L":
            img = img.convert("L")
        w, h = img.size
        img = img.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
        img = img.filter(ImageFilter.MedianFilter(size=3))
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        img = img.point(lambda p: 255 if p > 128 else 0)
        img = img.filter(ImageFilter.SHARPEN)
        return img
    except Exception:
        return None


def preprocess_image(img_path: Path) -> ImageType | None:
    if HAS_CV2:
        res = preprocess_image_cv2(img_path)
        if res is not None:
            return res
    if HAS_SKIMAGE:
        res = preprocess_image_skimage(img_path)
        if res is not None:
            return res
    return preprocess_image_pillow(img_path)


def should_skip(path: Path) -> bool:
    return path.suffix.lower() not in SUPPORTED_EXT


def save_processed_image(img: ImageType, img_path: Path) -> None:
    if isinstance(img, np.ndarray):
        if HAS_CV2:
            cv2.imwrite(str(img_path), img)
            return
        arr = img
        if arr.dtype != np.uint8:
            if arr.max() <= 1.0:
                arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
            else:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
        if arr.ndim == 3:
            arr = arr[..., 0]
        Image.fromarray(arr).save(str(img_path))
        return
    if isinstance(img, Image.Image):
        img.save(str(img_path))
        return
    msg = "Unsupported image format"
    raise ValueError(msg)


def process_single_image(image_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(image_path),
        "success": False,
        "error": None,
        "size_before": 0,
        "size_after": 0,
    }
    try:
        result["size_before"] = image_path.stat().st_size
        processed = preprocess_image(image_path)
        if processed is None:
            result["error"] = "Failed to process image"
            return result
        save_processed_image(processed, image_path)
        txt_path = image_path.with_suffix(".txt")
        text = pytesseract.image_to_string(processed, config="--oem 1 --psm 6")
        txt_path.write_text(text, encoding="utf-8")
        result["size_after"] = image_path.stat().st_size
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def get_image_files() -> list[Path]:
    image_files: list[Path] = []
    for path in BASE_DIR.rglob("*"):
        if path.is_file() and not should_skip(path):
            image_files.append(path)
    return image_files


def process() -> None:
    if HAS_CV2:
        if HAS_SKIMAGE:
            logger.info("Backend: OpenCV (scikit-image available as per-image fallback)")
        else:
            logger.info("Backend: OpenCV")
    elif HAS_SKIMAGE:
        logger.warning("OpenCV not found; using scikit-image backend")
    else:
        logger.warning("OpenCV and scikit-image not found; using Pillow-only fallback (limited accuracy)")
    image_files = get_image_files()
    total_images = len(image_files)
    if total_images == 0:
        print("No images found to process.")
        return
    print(f"📊 Found {total_images} images to process")
    print(f"⚡ Using {POOL_SIZE} workers for parallel processing")
    print("🔄 Processing images in-place...\n")
    processed_count = 0
    error_count = 0
    total_before = 0
    total_after = 0
    pool = multiprocessing.Pool(processes=POOL_SIZE)
    try:
        async_results = [(path, pool.apply_async(process_single_image, (path,))) for path in image_files]
        for path, async_result in async_results:
            try:
                result = async_result.get()
                if result["success"]:
                    processed_count += 1
                    total_before += result["size_before"]
                    total_after += result["size_after"]
                    status = "✅"
                else:
                    error_count += 1
                    status = "❌"
                relative = path.relative_to(BASE_DIR)
                print(f"{status} [{processed_count + error_count}/{total_images}] {relative}")
                if result.get("error"):
                    logger.warning(f"   ⚠️ Error: {result['error']}")
            except Exception as e:
                error_count += 1
                logger.error(f"❌ [{processed_count + error_count}/{total_images}] {path.name}: {e}")
    finally:
        pool.close()
        pool.join()
    print("=" * 40)
    print("📊 Processing Summary:")
    print(f"   ✅ Successfully processed: {processed_count} images")
    print(f"   ❌ Errors: {error_count} images")
    print(f"   📁 Total images: {total_images}")
    if processed_count > 0:
        size_reduction = (total_before - total_after) / total_before * 100 if total_before > 0 else 0
        print(f"   📦 Total size before: {total_before / (1024 * 1024):.2f} MB")
        print(f"   📦 Total size after: {total_after / (1024 * 1024):.2f} MB")
        print(f"   📉 Size reduction: {size_reduction:.1f}%")
    print("-" * 40)


def main() -> None:
    logger.warning("⚠️  WARNING: This script will MODIFY original image files in-place!")
    logger.warning("⚠️  NO BACKUPS will be created.")
    logger.warning("⚠️  CTRL+C to cancel, ENTER to continue...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n❌ Cancelled by user.")
        sys.exit(0)
    process()


if __name__ == "__main__":
    main()
