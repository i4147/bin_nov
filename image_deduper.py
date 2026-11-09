#!/data/data/com.termux/files/usr/bin/env python
"""Find visually duplicate images with perceptual hashing and keep the largest copy.
Computes the ``imagehash`` pHash of every JPEG/PNG/WebP/TIFF below the current directory, groups the images whose hashes match, keeps the one with the most pixels and deletes the rest.
``DRY_RUN`` is on by default, so only the report is printed until it is turned off.
Usage: image_deduper Examples: $ image_deduper Runs as a script (``python image_deduper.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
import os
from pathlib import Path

import imagehash
from PIL import Image


DRY_RUN = True
IMAGE_EXTENSIONS = (".webp", ".jpg", ".jpeg", ".png", ".tiff", ".tif")


def main():
    hash_groups = defaultdict(list)
    print("Scanning directories recursively for images...")
    image_count = 0
    for root, dirs, files in os.walk("."):
        for file_name in files:
            if file_name.lower().endswith(IMAGE_EXTENSIONS):
                image_count += 1
                file_path = Path(root).joinpath(file_name)
                try:
                    with Image.open(file_path) as img:
                        v_hash = imagehash.phash(img)
                        width, height = img.size
                        total_pixels = width * height
                        file_size = Path(file_path).stat().st_size
                        hash_groups[v_hash].append((file_path, total_pixels, file_size))
                except Exception:
                    print(f"⚠️ Could not read image: {file_path}")
                    continue
    print(f"\nScan complete. Processed {image_count} images.")
    print("Analyzing visual duplicates...\n")
    duplicates_to_remove = []
    for v_hash, instances in hash_groups.items():
        if len(instances) > 1:
            instances.sort(key=lambda x: (x[1], x[2]), reverse=True)
            keep_path, keep_pixels, keep_size = instances[0]
            print(f"🖼️  Visual Group [Hash: {v_hash}]")
            print(f"   ✅ KEEP: {keep_path} ({keep_pixels} px, {keep_size} bytes)")
            for dup_path, dup_pixels, dup_size in instances[1:]:
                duplicates_to_remove.append(dup_path)
                print(f"   ❌ DUP : {dup_path} ({dup_pixels} px, {dup_size} bytes)")
            print("-" * 50)
    print(f"\n--- Found {len(duplicates_to_remove)} duplicate images ---")
    if DRY_RUN:
        print("[DRY-RUN] No files were deleted.")
        print("👉 Set 'DRY_RUN = False' inside the script to permanently delete duplicate copies.")
    else:
        print("Deleting duplicate images, preserving highest resolutions...")
        for dup in duplicates_to_remove:
            try:
                os.remove(dup)
                print(f"Deleted: {dup}")
            except Exception as e:
                print(f"📁 Error deleting {dup}: {e}")


if __name__ == "__main__":
    main()
