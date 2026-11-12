#!/data/data/com.termux/files/usr/bin/env python
"""Convert the PNG/WebP/BMP/TIFF/GIF images below the current directory to JPEG.
Flattens the transparency on white, saves each image as a quality-70 ``.jpg`` next to the original and deletes the source; existing ``.jpg`` targets are skipped and the run ends with the converted/error counts.
Usage: to_jpg Examples: $ to_jpg Destructive: the original images are removed after conversion, so back them up first.
"""

from pathlib import Path

from PIL import Image


def convert_images_to_jpg_recursive() -> None:
    extensions = {".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
    current_dir = Path()
    converted_count = 0
    error_count = 0
    print("Scanning directory and subdirectories for images...")
    for file_path in current_dir.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in extensions:
            target_path = file_path.with_suffix(".jpg")
            if target_path.exists():
                print(f"⚠️ Skipped {file_path.relative_to(current_dir)}: {target_path.name} already exists.")
                continue
            try:
                with Image.open(file_path) as img:
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        mask = img.convert("RGBA").split()[-1] if img.mode != "LA" else img.convert("LA").split()[-1]
                        bg.paste(img, mask=mask)
                        bg.save(target_path, "JPEG", quality=70)
                    else:
                        img.convert("RGB").save(target_path, "JPEG", quality=70)
                file_path.unlink()
                print(f"✅ Converted: {file_path.relative_to(current_dir)} -> {target_path.name} (Original removed)")
                converted_count += 1
            except Exception as e:
                print(f"❌ Failed to process {file_path.relative_to(current_dir)}: {e}")
                error_count += 1
    print(f"\n🎉 Done! Successfully converted {converted_count} images. Errors encountered: {error_count}.")


if __name__ == "__main__":
    convert_images_to_jpg_recursive()
