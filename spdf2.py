#!/data/data/com.termux/files/usr/bin/env python
"""Optimise PDF files with qpdf's object streams.
Runs ``qpdf --linearize --object-streams=generate`` into a temporary file and keeps it only when it is smaller than the original, reporting the sizes and the total space freed.
Files are processed in parallel.
Usage: spdf2 [pdfs ...] Examples: $ spdf2 books/ Runs as a script (``python spdf2.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import fsz, get_files, gsz, mpf, runcmd


def process_file(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        print("Input file not found.", file=sys.stderr)
        sys.exit(1)
    temp_qpdf = path.with_name(f"temp_qpdf_{path.name}")
    before = path.stat().st_size
    print(f"{path.name} Before : {fsz(before)}")
    qpdf_cmd = [
        "qpdf",
        "--linearize",
        "--object-streams=generate",
        str(path),
        str(temp_qpdf),
    ]
    runcmd(qpdf_cmd, show_output=True)
    if temp_qpdf.exists():
        after = temp_qpdf.stat().st_size
        print(f"{path.name} After  : {fsz(after)}")
        diff = before - after
        sign = "-" if diff >= 0 else "+"
        if after < before:
            temp_qpdf.replace(path)
            print(f"Saved  : {sign}{fsz(abs(diff))}")
        else:
            print("original file is smaller")
            temp_qpdf.unlink(missing_ok=True)


def main() -> None:
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
        print(f"space freed: {fsz(dsz)}")


if __name__ == "__main__":
    raise SystemExit(main())
