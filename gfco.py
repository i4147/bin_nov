#!/data/data/com.termux/files/usr/bin/env python
"""Convert font files between the TTF/OTF/WOFF/WOFF2/EOT containers.
Converts each input to the ``--to`` format with fontTools through a four-worker executor (``--pool-method``), writes the results into ``--output-dir`` or next to the source and removes the originals with ``-r``.
Usage: gfco FONT [...] --to ttf|otf|woff|woff2|eot [--output-dir DIR] [-r] Positional arguments: input_files ...
Path(s) to font file(s) to convert.
Options: --to Target format (ttf, otf, woff, woff2, eot).
Defaults to 'ttf'.
--output-dir Optional output directory for converted fonts.
-r, --remove Remove the original font file if conversion is successful.
--pool-method Method/Executor strategy to use with 4 workers.
Defaults to 'imap_unordered'.
Examples: $ gfco fonts/*.otf --to woff2 Command-line script: run ``python gfco.py`` or the ``gfco`` command.
The module is importable as well."""

import argparse
import concurrent.futures
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Any, Optional


try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: 'fonttools' library is not installed. Install it using: pip install fonttools[woff] brotli")
    sys.exit(1)
try:
    from concurrent.futures import InterpreterPoolExecutor
except ImportError:
    InterpreterPoolExecutor = None
SUPPORTED_EXTENSIONS: set[str] = {".ttf", ".otf", ".woff", ".woff2", ".eot"}


def convert_font(
    input_path: Path, target_format: str, output_dir: Optional[Path] = None, remove_original: bool = False
) -> None:
    target_format = target_format.lower().lstrip(".")
    valid_formats = {"ttf", "otf", "woff", "woff2", "eot"}
    if target_format not in valid_formats:
        msg = f"Unsupported format '{target_format}'. Choose from: {', '.join(valid_formats)}"
        raise ValueError(msg)
    if not input_path.exists() or not input_path.is_file():
        msg = f"Source font file not found: {input_path}"
        raise FileNotFoundError(msg)
    if target_format == "eot":
        print(
            f"[-] Notice: Native EOT writing requires specialized binary mapping. "
            f"Fonttools focuses on sfnt formats. Skipping EOT conversion for: {input_path.name}"
        )
        return
    out_dir = output_dir or input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{input_path.stem}.{target_format}"
    print(f"[*] Processing: {input_path.name} -> {target_format.upper()}")
    try:
        font = TTFont(str(input_path))
        if target_format in {"woff", "woff2"}:
            font.flavor = target_format
        else:
            font.flavor = None
        font.save(str(output_path))
    except Exception as e:
        if "woff2" in target_format and "brotli" in str(e).lower():
            print("[-] Error: WOFF2 conversion requires the 'brotli' package. Install it via: pip install brotli")
        else:
            print(f"[-] Error converting {input_path.name}: {e}")
        return
    original_size = input_path.stat().st_size
    new_size = output_path.stat().st_size
    size_diff = new_size - original_size
    percent_change = (size_diff / original_size) * 100 if original_size > 0 else 0
    if size_diff > 0:
        change_str = f"increased by {abs(size_diff):,} bytes ({abs(percent_change):.2f}%) 📈"
    elif size_diff < 0:
        change_str = f"decreased by {abs(size_diff):,} bytes ({abs(percent_change):.2f}%) 📉"
    else:
        change_str = "remained unchanged ⚖️"
    print(f"    [+] Saved successfully to: {output_path}")
    print(f"    [+] Original Size: {original_size / 1024:.2f} KB | New Size: {new_size / 1024:.2f} KB")
    print(f"    [+] Size {change_str}")
    if remove_original and output_path.exists():
        try:
            input_path.unlink()
            print(f"    [+] Removed original source file: {input_path.name} 🗑️")
        except Exception as rm_err:
            print(f"    [!] Warning: Could not remove original file: {rm_err}")
    print()


def _worker_wrapper(args: tuple[Path, str, Optional[Path], bool]) -> None:
    convert_font(*args)


def find_fonts_in_current_dir() -> list[Path]:
    current_dir = Path.cwd()
    found_fonts = [f for f in current_dir.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
    return found_fonts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert font files between formats using fonttools with multiprocessing / concurrent.futures."
    )
    parser.add_argument("input_files", nargs="*", type=Path, help="Path(s) to font file(s) to convert.")
    parser.add_argument(
        "--to", type=str, default="ttf", help="Target format (ttf, otf, woff, woff2, eot). Defaults to 'ttf'."
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="Optional output directory for converted fonts.")
    parser.add_argument(
        "-r", "--remove", action="store_true", help="Remove the original font file if conversion is successful."
    )
    parser.add_argument(
        "--pool-method",
        type=str,
        default="imap_unordered",
        choices=[
            "map",
            "imap",
            "imap_unordered",
            "starmap",
            "apply",
            "apply_async",
            "apply_asybc",
            "thread",
            "process",
            "interpreter",
        ],
        help="Method/Executor strategy to use with 4 workers. Defaults to 'imap_unordered'.",
    )
    args = parser.parse_args()
    pool_method = "apply_async" if args.pool_method == "apply_asybc" else args.pool_method
    input_files = args.input_files
    if not input_files:
        print("[*] No input files specified. Scanning current directory recursively...")
        input_files = find_fonts_in_current_dir()
        if not input_files:
            print("[-] No supported font files found in the current directory.")
            sys.exit(0)
    num_workers = 4
    tasks = [(file_path, args.to, args.output_dir, args.remove) for file_path in input_files]
    print(f"\n=== Font Converter Initialized ===")
    print(f"Target Format: {args.to.upper()}")
    print(f"Files to process: {len(input_files)}")
    print(f"Pool Method: {pool_method} (Workers: {num_workers})")
    print(f"Remove originals: {args.remove}\n")
    try:
        if pool_method in {"map", "imap", "imap_unordered", "starmap", "apply", "apply_async"}:
            with mp.Pool(processes=num_workers) as pool:
                if pool_method == "map":
                    pool.map(_worker_wrapper, tasks)
                elif pool_method == "imap":
                    for _ in pool.imap(_worker_wrapper, tasks):
                        pass
                elif pool_method == "imap_unordered":
                    for _ in pool.imap_unordered(_worker_wrapper, tasks):
                        pass
                elif pool_method == "starmap":
                    pool.starmap(convert_font, tasks)
                elif pool_method == "apply":
                    for task in tasks:
                        pool.apply(convert_font, task)
                elif pool_method == "apply_async":
                    results = [pool.apply_async(convert_font, task) for task in tasks]
                    for r in results:
                        r.get()
        elif pool_method == "thread":
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                list(executor.map(lambda t: convert_font(*t), tasks))
        elif pool_method == "process":
            with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
                list(executor.map(lambda t: convert_font(*t), tasks))
        elif pool_method == "interpreter":
            if InterpreterPoolExecutor is None:
                print("[-] Error: InterpreterPoolExecutor is not available (requires Python 3.14+).")
                sys.exit(1)
            with InterpreterPoolExecutor(max_workers=num_workers) as executor:
                list(executor.map(lambda t: convert_font(*t), tasks))
    except Exception as err:
        print(f"[-] Execution error during parallel batch processing: {err}")
        sys.exit(1)
    print("=== Conversion Complete ===")


if __name__ == "__main__":
    main()
