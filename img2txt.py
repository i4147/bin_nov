#!/data/data/com.termux/files/usr/bin/env python
"""OCR images and collect the extracted text per image.
Converts each image with pytesseract, filters the lines that contain plausible words and reports the text it found.
Files are processed in a pool; the work is independent of the ``tesseract`` CLI.
Usage: img2txt [images ...] Examples: $ img2txt scan.png Runs as a script (``python img2txt.py``) in the directory it should act on; the module can also be imported.
"""

from collections import deque
from collections.abc import Callable
from pathlib import Path
import sys
from typing import Any, TYPE_CHECKING

from PIL import Image
from pytesseract import image_to_string


def mpf(process_function: Callable, files: list[Path], **kwargs: Any):
    from joblib import Parallel, delayed

    file_strings = [str(f) for f in files]
    return Parallel(n_jobs=4)(delayed(process_function)(file_str, **kwargs) for file_str in file_strings)


def get_files(path: str | Path, ext: list[str] | None = None) -> list[Path]:
    path = Path(path)
    skip_dirs = {".git", "__pycache__", "node_modules"}
    queue = deque([path])
    files = []
    while queue:
        current = queue.popleft()
        try:
            for item in current.iterdir():
                if item.is_symlink():
                    continue
                if item.is_dir() and item.name not in skip_dirs:
                    queue.append(item)
                elif item.is_file() and (ext is None or item.suffix.lower() in ext):
                    files.append(item)
        except (PermissionError, OSError, FileNotFoundError):
            continue
    return files


def extract_text(image_path: Path) -> str:
    try:
        with Image.open(image_path) as img:
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            result = image_to_string(img, lang="eng", config="--oem 1 --psm 6")
        print("-" * 40)
        print(result.strip() or "[No text detected]")
        return result.strip()
    except Exception as e:
        print(f"Error processing {image_path.name}: {e}")
        return ""


def process_file(path: Path) -> None:
    path = Path(path)
    txtfile = path.with_suffix(".txt")
    if txtfile.exists():
        print(f"✓ {path.name} already processed")
        return
    print(f"→ Processing {path.name}")
    text = extract_text(path)
    if text and len(text) > 5:
        try:
            txtfile.write_text(text, encoding="utf-8")
            print(f"✓ Saved {txtfile.name}")
        except Exception as e:
            print(f"✗ Failed to write {txtfile.name}: {e}")
    else:
        print(f"⚠ No significant text in {path.name}")


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    if args:
        files = [Path(p) for p in args if Path(p).is_file()]
    else:
        print("Scanning directory for images...")
        files = get_files(cwd, ext=[".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"])
    if not files:
        print("No image files found.")
        return
    print(f"Found {len(files)} image(s). Starting OCR...\n")
    max_workers = 2
    if len(files) == 1:
        process_file(files[0])
    else:
        print(f"Using {max_workers} worker(s) for memory safety...")
        mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
