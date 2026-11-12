#!/data/data/com.termux/files/usr/bin/env python
"""Render a PDF to PNG files with ``pdftoppm``.
Calls poppler's ``pdftoppm`` for the given file at the requested dpi (200 by default), writes the images into the output directory (named after the PDF when omitted) and renumbers them as ``<name>_page_NNN.png``.
Usage: pdf2png FILE.pdf [OUTPUT_DIR] [DPI] Examples: $ pdf2png report.pdf pages/ 150 Runs as a script (``python pdf2png.py``) in the directory it should act on; the module can also be imported.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys


def pdf_to_png(pdf_path: Path, output_dir=None, dpi=200) -> None:
    if not Path(pdf_path).is_file():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if shutil.which("pdftoppm") is None:
        print(
            "Error: 'pdftoppm' not found. Install with: pkg install poppler",
            file=sys.stderr,
        )
        sys.exit(1)
    base_name = (str(Path(Path(pdf_path).name).with_suffix("")), Path(Path(pdf_path).name).suffix)[0]
    if output_dir is None:
        output_dir = base_name
    os.makedirs(output_dir, exist_ok=True)
    prefix = Path(output_dir).joinpath(f"{base_name}_page")
    cmd = [
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        pdf_path,
        prefix,
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("pdftoppm failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    generated = sorted(f for f in os.listdir(output_dir) if f.startswith(f"{base_name}_page-"))
    for f in generated:
        num_part = f.rsplit("-", 1)[-1].split(".")[0]
        try:
            num = int(num_part)
        except ValueError:
            continue
        new_name = f"{base_name}_page_{num:03d}.png"
        os.rename(Path(output_dir).joinpath(f), Path(output_dir).joinpath(new_name))
        print(f"  {new_name}")
    print(f"Done. {len(generated)} image(s) saved to '{output_dir}/'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    input_pdf = sys.argv[1]
    output_directory = sys.argv[2] if len(sys.argv) > 2 else None
    dpi_value = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    pdf_to_png(input_pdf, output_directory, dpi_value)
