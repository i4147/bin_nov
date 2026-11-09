#!/data/data/com.termux/files/usr/bin/env python
"""Shrink PDF files with Ghostscript's screen profile.
Runs ``gs`` with 85 dpi downsampling and font subsetting into a temporary file and replaces the original only when the result is smaller, printing the before/after sizes and the space saved.
Files are processed in parallel.
Usage: spdf [pdfs ...] Examples: $ spdf scans/ Runs as a script (``python spdf.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import fsz, get_files, gsz, mpf, runcmd


def process_file(path):
    path = Path(path)
    temp_gs = path.with_name(f"temp_gs_{path.name}")
    size_before = path.stat().st_size
    print(f"{path.name} Before : {fsz(size_before)}")
    gs_cmd = [
        "gs",
        "-dBATCH",
        "-dColorConversionStrategy=/sRGB",
        "-dColorImageDownsampleType=/Bicubic",
        "-dColorImageResolution=85",
        "-dCompatibilityLevel=1.4",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-dEmbedAllFonts=false",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dGrayImageResolution=85",
        "-dHaveTransparency=false",
        "-dMonoImageDownsampleType=/Bicubic",
        "-dMonoImageResolution=85",
        "-dNOPAUSE",
        "-dOptimize=true",
        "-dPDFSETTINGS=/screen",
        "-dSubsetFonts=true",
        "-sDEVICE=pdfwrite",
        f"-sOutputFile={temp_gs}",
        str(path),
    ]
    runcmd(gs_cmd, show_output=True)
    if temp_gs.exists():
        size_after = temp_gs.stat().st_size
        if size_after:
            print(f"{path.name} After  : {fsz(size_after)}")
            diff = size_before - size_after
            sign = "-" if diff >= 0 else "+"
            if size_after < size_before:
                temp_gs.replace(path)
                print(f"Saved  : {sign}{fsz(diff)}")
            else:
                print("original file is smaller")
                temp_gs.unlink(missing_ok=True)


def main():
    cwd = Path.cwd()
    before = gsz(cwd)
    args = sys.argv[1:]
    files = list([Path(p) for p in args] if args else get_files(cwd, ext=[".pdf"]))
    if len(files) == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)
    after = gsz(cwd)
    dsz = before - after
    if dsz:
        print(f"space freed : {fsz(dsz)}")


if __name__ == "__main__":
    raise SystemExit(main())
