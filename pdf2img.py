#!/data/data/com.termux/files/usr/bin/env python
"""Render the PDFs below the current directory to JPEG pages.
Converts each page at 300 dpi into ``output_jpgs/<name>/<name>_page_N.jpg`` with PyMuPDF and deletes the original PDF after a successful conversion; failed conversions keep their sources and are counted in the summary.
Usage: pdf2img Examples: $ pdf2img Deletes each PDF once all of its pages were written."""

from pathlib import Path
import shutil

import pymupdf


def convert_pdf_to_jpg(pdf_path: Path, output_folder: Path) -> bool:
    pdf_output_dir: Path = output_folder / pdf_path.stem
    try:
        print(f"Converting '{pdf_path.name}'...")
        pdf_output_dir.mkdir(parents=True, exist_ok=True)
        converted_files: list[Path] = []
        with pymupdf.open(pdf_path) as document:
            for page_index, page in enumerate(document):
                pixmap: pymupdf.Pixmap = page.get_pixmap(dpi=300)
                final_jpg_path: Path = pdf_output_dir / f"{pdf_path.stem}_page_{page_index + 1}.jpg"
                pixmap.save(str(final_jpg_path), jpg_quality=95)
                converted_files.append(final_jpg_path)
        print(f"Successfully converted '{pdf_path.name}' to {len(converted_files)} JPG files in '{pdf_output_dir}'.")
        return True
    except Exception as e:
        print(f"Error converting '{pdf_path.name}': {e}")
        if pdf_output_dir.exists():
            try:
                shutil.rmtree(pdf_output_dir)
            except Exception as cleanup_e:
                print(f"Error during cleanup of '{pdf_output_dir}': {cleanup_e}")
        return False


def process_directory(start_dir: Path, output_base_dir: Path) -> None:
    print(f"Starting PDF to JPG conversion in directory: {start_dir}")
    print(f"Output will be saved in: {output_base_dir}")
    converted_count: int = 0
    failed_count: int = 0
    for item in start_dir.rglob("*"):
        if item.is_file() and item.suffix.lower() == ".pdf":
            if output_base_dir in item.parents:
                print(f"Skipping PDF '{item.name}' as it's within the output directory.")
                continue
            if convert_pdf_to_jpg(item, output_base_dir):
                try:
                    item.unlink()
                    print(f"Removed original PDF: '{item.name}'")
                    converted_count += 1
                except OSError as e:
                    print(f"Error removing original PDF '{item.name}': {e}")
                    failed_count += 1
            else:
                failed_count += 1
    print("\n--- Conversion Summary ---")
    print(f"Successfully converted and removed: {converted_count} PDF files.")
    print(f"Failed to convert: {failed_count} PDF files.")
    print("------------------------")


if __name__ == "__main__":
    cwdectory: Path = Path.cwd()
    output_directory: Path = cwdectory / "output_jpgs"
    output_directory.mkdir(exist_ok=True)
    process_directory(cwdectory, output_directory)
    print("\nScript finished.")
    print(f"Converted JPG files are located in: {output_directory}")
