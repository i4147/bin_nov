#!/data/data/com.termux/files/usr/bin/env python
"""Extract the TTF/WOFF/WOFF2 fonts packed inside APK files.
Opens each APK with zipfile, pulls the ``.ttf``/``.woff``/``.woff2`` members (also the fonts embedded as base64 resources), names them from their internal font names and writes them to the output directory with eight fixed workers.
Usage: apk_font_extractor [apks or directories] [-o OUTDIR] Positional arguments: inputs ...
Input APK files or directories (default: current directory) Options: -o, --output Output directory for extracted fonts (default: fonts) -v, --verbose Enable verbose logging Examples: $ apk_font_extractor apps/ -o fonts Command-line script: run ``python apk_font_extractor.py`` or the ``apk_font_extractor`` command.
The module is importable as well."""

import argparse
import contextlib
from dataclasses import dataclass, field
import multiprocessing
from pathlib import Path
import re
import sys
import zipfile

from dh import MAX_WORKERS
from fontTools.ttLib import TTFont
from loguru import logger


DEFAULT_OUTPUT_DIR: Path = Path("/sdcard/_static/fonts")
FONT_EXTENSIONS: frozenset[str] = frozenset({".ttf", ".woff", ".woff2"})
APK_TIMEOUT_SECONDS: int = 600


@dataclass
class FontInfo:
    family_name: str
    style_name: str
    weight: int
    is_italic: bool
    extension: str
    original_path: Path = field(default_factory=Path)


class APKFontExtractor:
    FONT_EXTENSIONS: frozenset[str] = FONT_EXTENSIONS

    def __init__(self, output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
        self.output_dir: Path = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.processed_fonts: dict[str, Path] = {}
        print(
            "Initialized APKFontExtractor output_dir={} workers={}",
            self.output_dir,
            MAX_WORKERS,
        )

    def _find_apk_files(self, paths: list[Path]) -> list[Path]:
        apk_files: list[Path] = []
        for path in paths:
            if path.is_file() and path.suffix.lower() == ".apk":
                apk_files.append(path)
                logger.debug("Found APK file: {}", path)
            elif path.is_dir():
                for apk_path in path.rglob("*.apk"):
                    apk_files.append(apk_path)
                    logger.debug("Found APK file: {}", apk_path)
        print("Found {} APK file(s) to process", len(apk_files))
        return apk_files

    def _extract_fonts_from_apk(self, apk_path: Path) -> list[tuple[Path, bytes, str]]:
        fonts: list[tuple[Path, bytes, str]] = []
        try:
            with zipfile.ZipFile(apk_path, "r") as zip_ref:
                for file_info in zip_ref.filelist:
                    internal_path = Path(file_info.filename)
                    if internal_path.suffix.lower() not in self.FONT_EXTENSIONS:
                        continue
                    try:
                        font_data: bytes = zip_ref.read(file_info.filename)
                    except Exception as exc:
                        logger.warning(
                            "Failed to read {} from {}: {}",
                            internal_path,
                            apk_path.name,
                            exc,
                        )
                        continue
                    fonts.append((
                        internal_path,
                        font_data,
                        internal_path.suffix.lower(),
                    ))
                    logger.debug(
                        "Extracted font candidate: {} from {}",
                        internal_path,
                        apk_path.name,
                    )
        except zipfile.BadZipFile:
            logger.error("Invalid APK file: {}", apk_path)
        except Exception as exc:
            logger.error("Error processing {}: {}", apk_path, exc)
        return fonts

    def _get_font_metadata(self, font_data: bytes) -> FontInfo | None:
        font: TTFont | None = None
        try:
            font = TTFont(io=None, fontData=font_data)
            family_name: str = "Unknown"
            style_name: str = "Regular"
            weight: int = 400
            is_italic: bool = False
            if "name" in font:
                name_table = font["name"]
                best_family = name_table.getBestFamilyName()
                if best_family:
                    family_name = best_family
                style_record = name_table.getName(2, 3, 1, 1033)
                if style_record:
                    style_name = style_record.toStr()
                is_italic = "italic" in style_name.lower()
            if "OS/2" in font:
                weight = int(font["OS/2"].usWeightClass)
            else:
                weight_lower = style_name.lower()
                if "thin" in weight_lower:
                    weight = 100
                elif "extralight" in weight_lower or "ultralight" in weight_lower:
                    weight = 200
                elif "light" in weight_lower:
                    weight = 300
                elif "medium" in weight_lower:
                    weight = 500
                elif "semibold" in weight_lower or "demibold" in weight_lower:
                    weight = 600
                elif "bold" in weight_lower:
                    weight = 700
                elif "extrabold" in weight_lower or "ultrabold" in weight_lower:
                    weight = 800
                elif "black" in weight_lower or "heavy" in weight_lower:
                    weight = 900
            if font_data[:4] == b"OTTO":
                extension = ".otf"
            elif font_data[:4] == b"wOFF":
                extension = ".woff"
            elif font_data[:4] == b"wOF2":
                extension = ".woff2"
            else:
                extension = ".ttf"
            return FontInfo(
                family_name=family_name,
                style_name=style_name,
                weight=weight,
                is_italic=is_italic,
                extension=extension,
                original_path=Path(),
            )
        except Exception as exc:
            logger.warning("Failed to extract font metadata: {}", exc)
            return None
        finally:
            if font is not None:
                with contextlib.suppress(Exception):
                    font.close()

    def _generate_font_filename(self, font_info: FontInfo) -> str:
        family_name = re.sub(r"[^\w\s-]", "", font_info.family_name)
        family_name = re.sub(r"\s+", "-", family_name.strip())
        style_str = font_info.style_name
        style_str = re.sub(r"[^\w\s-]", "", style_str)
        style_str = re.sub(r"\s+", "-", style_str.strip())
        if style_str.lower() in ("regular", "normal", "medium", ""):
            if font_info.weight <= 300:
                style_str = "Light"
            elif font_info.weight >= 700:
                style_str = "Bold"
            else:
                style_str = "Regular"
        if font_info.is_italic and "italic" not in style_str.lower():
            style_str = f"{style_str}-Italic"
        return f"{family_name}-{style_str}{font_info.extension}"

    def _handle_duplicate_filename(self, filename: str, source_apk: Path, font_data: bytes) -> str:
        base_name = Path(filename).stem
        extension = Path(filename).suffix
        counter = 1
        while True:
            new_filename = filename if counter == 1 else f"{base_name}-{counter}{extension}"
            output_path = self.output_dir / new_filename
            if not output_path.exists():
                return new_filename
            if self._is_same_font(output_path, source_apk, font_data):
                print("Font already exists: {}", new_filename)
                return new_filename
            counter += 1

    def _is_same_font(self, existing_path: Path, source_apk: Path, font_data: bytes) -> bool:
        try:
            existing_size = existing_path.stat().st_size
        except OSError:
            return False
        if existing_size != len(font_data):
            return False
        key = f"{source_apk.name}-{existing_size}"
        return key in self.processed_fonts

    def _save_font(self, font_data: bytes, filename: str, source_apk: Path) -> Path | None:
        final_filename = self._handle_duplicate_filename(filename, source_apk, font_data)
        output_path = self.output_dir / final_filename
        try:
            output_path.write_bytes(font_data)
            print("Saved font: {}", final_filename)
            self.processed_fonts[f"{source_apk.name}-{len(font_data)}"] = output_path
            return output_path
        except Exception as exc:
            logger.error("Failed to save font {}: {}", final_filename, exc)
            return None

    def _process_apk(self, apk_path: Path) -> int:
        print("Processing APK: {}", apk_path.name)
        fonts = self._extract_fonts_from_apk(apk_path)
        extracted_count = 0
        for original_path, font_data, extension in fonts:
            font_info = self._get_font_metadata(font_data)
            if font_info is not None:
                font_info.original_path = original_path
                font_info.extension = extension
                filename = self._generate_font_filename(font_info)
            else:
                filename = original_path.name
            if self._save_font(font_data, filename, apk_path) is not None:
                extracted_count += 1
        print("Extracted {} font(s) from {}", extracted_count, apk_path.name)
        return extracted_count

    def process(self, input_paths: list[Path] | None = None) -> int:
        if input_paths is None:
            input_paths = [Path.cwd()]
        else:
            input_paths = [Path(p) for p in input_paths]
        apk_files = self._find_apk_files(input_paths)
        if not apk_files:
            logger.warning("No APK files found to process")
            return 0
        total_extracted = 0
        try:
            with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
                async_results: list[tuple[Path, multiprocessing.pool.AsyncResult[int]]] = []
                for apk_path in apk_files:
                    result = pool.apply_async(self._process_apk, (apk_path,))
                    async_results.append((apk_path, result))
                for apk_path, async_result in async_results:
                    try:
                        count = async_result.get(timeout=APK_TIMEOUT_SECONDS)
                        total_extracted += count
                    except multiprocessing.TimeoutError:
                        logger.error("Timeout processing {}", apk_path.name)
                    except Exception as exc:
                        logger.error("Error processing {}: {}", apk_path.name, exc)
        except Exception as exc:
            logger.error("Error in parallel processing: {}", exc)
        print("Total fonts extracted: {}", total_extracted)
        return total_extracted


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Extract .ttf, .woff, and .woff2 fonts from APK files using a fixed pool of 8 workers."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process all APKs in current directory recursively
  %(prog)s
  # Process specific APK files
  %(prog)s app1.apk app2.apk
  # Process APKs in specific directories
  %(prog)s /path/to/apks /another/path
  # Specify custom output directory
  %(prog)s -o custom_fonts /path/to/apks
        """,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input APK files or directories (default: current directory)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for extracted fonts (default: fonts)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.remove()
        logger.add(sys.stderr, level="INFO")
    try:
        args.output.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("Cannot create output directory {}: {}", args.output, exc)
        return 1
    extractor = APKFontExtractor(output_dir=args.output)
    try:
        total_fonts = extractor.process(input_paths=list(args.inputs) if args.inputs else None)
        if total_fonts > 0:
            print("Successfully extracted {} font(s)", total_fonts)
        else:
            logger.warning("No fonts were extracted")
        return 0
    except KeyboardInterrupt:
        print("Interrupted by user")
        return 130
    except Exception as exc:
        logger.error("Fatal error: {}", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
