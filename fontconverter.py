#!/data/data/com.termux/files/usr/bin/env python
"""Font conversion toolkit.
Subcommands: ``convert`` rewrites the TTF/OTF/WOFF/WOFF2 fonts in another container, ``otf2ttf`` and ``otf2ttf-fontforge`` outline the OTF fonts as TTF (fontTools or FontForge), ``tottf`` uses the FontForge CLI for SVG/WOFF/EOT/OTF/TTC and ``woff22ttf`` decompresses WOFF2; ``-r`` removes the sources and the work runs in parallel.
Usage: fontconverter {convert,otf2ttf,otf2ttf-fontforge,tottf,woff22ttf} [options] [paths ...] Positional arguments: paths ...
Font files or directories (default: scan cwd) Subcommands: convert Convert TTF/OTF/WOFF/WOFF2 fonts to another container flavour.
otf2ttf Convert OTF to TTF using fontTools (pure Python).
otf2ttf-fontforge Convert OTF to TTF using FontForge Python bindings.
tottf Convert SVG/WOFF/EOT/OTF/TTC to TTF using FontForge CLI.
woff22ttf Decompress WOFF2 to TTF using fontTools.
Options: --to Target format -r, --rm Delete source file after successful conversion -k, --keep-source Keep original OTF files (default: remove) -r, --remove-source Delete source file after successful conversion Examples: $ fontconverter otf2ttf fonts/ $ fontconverter convert --to woff2 fonts/ Command-line script: run ``python fontconverter.py`` or the ``fontconverter`` command.
The module is importable as well."""

import argparse
import multiprocessing
from pathlib import Path
import subprocess
import sys
from typing import Callable, Sequence

from dh import MAX_WORKERS


try:
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, woff2

    HAS_FONTTOOLS = True
except ImportError:
    HAS_FONTTOOLS = False


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def collect_files(
    paths: Sequence[str],
    extensions: Sequence[str],
) -> list[Path]:
    ext_set = {ext.lower() for ext in extensions}
    result: list[Path] = []
    if not paths:
        paths = [str(Path.cwd())]
    for p_str in paths:
        p = Path(p_str)
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in ext_set:
                    result.append(f)
        elif p.is_file():
            if p.suffix.lower() in ext_set:
                result.append(p)
            else:
                print(
                    f"Warning: {p} does not match extensions {extensions}",
                    file=sys.stderr,
                )
        else:
            print(f"Warning: path not found: {p}", file=sys.stderr)
    seen = set()
    unique: list[Path] = []
    for f in result:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def run_parallel(
    func: Callable,
    items: list,
    workers: int,
    *args,
) -> list:
    if workers <= 1 or len(items) <= 1:
        return [func(item, *args) for item in items]
    with multiprocessing.Pool(processes=workers) as pool:
        return pool.starmap(func, [(item, *args) for item in items])


def _convert_worker(src: Path, target_ext: str, remove_source: bool) -> None:
    if not HAS_FONTTOOLS:
        print(
            "Error: fontTools is required for 'convert'. Install with: pip install fonttools",
            file=sys.stderr,
        )
        return
    flavor_map = {
        "woff": "woff",
        "woff2": "woff2",
        "ttf": None,
    }
    flavor = flavor_map[target_ext]
    dst = src.with_suffix(f".{target_ext}")
    if dst.exists():
        dst = unique_path(dst)
    try:
        font = TTFont(src)
        font.flavor = flavor
        font.save(dst)
        print(f"{src.name} -> {dst.name}")
        if remove_source and src.exists():
            src.unlink()
    except Exception as exc:
        print(f"Error converting {src.name}: {exc}", file=sys.stderr)


def cmd_convert(args: argparse.Namespace) -> int:
    if not HAS_FONTTOOLS:
        print(
            "Error: fontTools is required. Install with: pip install fonttools",
            file=sys.stderr,
        )
        return 1
    extensions = [".ttf", ".otf", ".woff", ".woff2"]
    files = collect_files(args.paths, extensions)
    target_suffix = f".{args.to}"
    files = [f for f in files if f.suffix.lower() != target_suffix]
    if not files:
        print(f"No font files to convert to {target_suffix}", file=sys.stderr)
        return 1
    run_parallel(_convert_worker, files, MAX_WORKERS, args.to, args.rm)
    return 0


def _otf2ttf_worker(src: Path, keep_source: bool) -> dict:
    if not HAS_FONTTOOLS:
        return {"status": "failed", "error": "fontTools not installed", "otf": str(src)}
    dst = src.with_suffix(".ttf")
    status = {"otf": str(src), "ttf": str(dst), "status": "unknown"}
    if dst.exists():
        status["status"] = "skipped_exists"
        return status
    try:
        font = TTFont(src)
        if "glyf" in font:
            status["status"] = "skipped_already_ttf"
            return status
        cff_table = None
        if "CFF2" in font:
            cff_table = font["CFF2"]
        elif "CFF " in font:
            cff_table = font["CFF "]
        else:
            status["status"] = "failed_no_cff"
            return status
        glyph_order = font.getGlyphOrder()
        for glyph_name in glyph_order:
            if glyph_name in cff_table:
                pen = TTGlyphPen(None)
                cff_table[glyph_name].draw(pen)
                font["glyf"][glyph_name] = pen.glyph()
        font.flavor = None
        for tag in ["CFF ", "CFF2", "VORG"]:
            if tag in font:
                del font[tag]
        if "glyf" not in font:
            msg = "Failed to create glyf table"
            raise ValueError(msg)
        font.sfVersion = "\x00\x01\x00\x00"
        font.reader = None
        font.save(dst)
        if not keep_source:
            src.unlink()
        status["status"] = "success"
    except Exception as exc:
        status["status"] = "failed"
        status["error"] = str(exc)
        if dst.exists():
            dst.unlink()
    return status


def cmd_otf2ttf(args: argparse.Namespace) -> int:
    if not HAS_FONTTOOLS:
        print(
            "Error: fontTools is required. Install with: pip install fonttools",
            file=sys.stderr,
        )
        return 1
    files = collect_files(args.paths, [".otf"])
    if not files:
        print("No OTF files found.", file=sys.stderr)
        return 1
    print(f"Found {len(files)} OTF file(s)")
    print(f"Using {MAX_WORKERS} worker processes")
    results = run_parallel(_otf2ttf_worker, files, MAX_WORKERS, args.keep_source)
    summary = {
        "total": len(results),
        "success": 0,
        "skipped_exists": 0,
        "skipped_already_ttf": 0,
        "failed": 0,
    }
    for res in results:
        st = res["status"]
        if st == "success":
            summary["success"] += 1
            print(f"  ✓ Converted: {res['ttf']} (original {'kept' if args.keep_source else 'removed'})")
        elif st == "skipped_exists":
            summary["skipped_exists"] += 1
            print(f"  ⚠ Skipped: {res['ttf']} (already exists)")
        elif st == "skipped_already_ttf":
            summary["skipped_already_ttf"] += 1
            print(f"  ⚠ Skipped: {res['otf']} (already has TrueType outlines)")
        else:
            summary["failed"] += 1
            err = res.get("error", "Unknown error")
            print(f"  ✗ Failed: {res['otf']} — {err}")
    print("\n" + "=" * 40)
    print("Conversion Summary:")
    print(f"  Total OTF files found: {summary['total']}")
    print(f"  Successfully converted: {summary['success']}")
    print(f"  Skipped (TTF exists): {summary['skipped_exists']}")
    print(f"  Skipped (already TrueType): {summary['skipped_already_ttf']}")
    print(f"  Failed: {summary['failed']}")
    return 0 if summary["failed"] == 0 else 1


def _otf2ttf_fontforge_worker(src: Path, keep_source: bool) -> tuple[str, str]:
    try:
        import fontforge
    except ImportError:
        return ("error", "FontForge Python module not available")
    dst = src.with_suffix(".ttf")
    if dst.exists():
        return ("skipped", str(dst))
    try:
        font = fontforge.open(str(src))
        font.generate(str(dst), flags=("opentype",))
        font.close()
        if not keep_source:
            src.unlink()
        return ("success", str(dst))
    except Exception as exc:
        return ("error", str(exc))


def cmd_otf2ttf_fontforge(args: argparse.Namespace) -> int:
    try:
        import fontforge
    except ImportError:
        print("This script must be run with FontForge's Python interpreter:")
        print("  fontforge-script font_toolkit.py otf2ttf-fontforge ...")
        return 1
    files = collect_files(args.paths, [".otf"])
    if not files:
        print("No OTF files found.", file=sys.stderr)
        return 1
    print(f"Found {len(files)} OTF file(s)\n")
    summary = {"success": 0, "skipped": 0, "error": 0}
    for src in files:
        print(f"Processing: {src}")
        status, msg = _otf2ttf_fontforge_worker(src, args.keep_source)
        if status == "success":
            print(f"  ✓ Converted: {msg} (original {'kept' if args.keep_source else 'removed'})")
            summary["success"] += 1
        elif status == "skipped":
            print(f"  ⚠ Skipped: {msg} (already exists)")
            summary["skipped"] += 1
        else:
            print(f"  ✗ Failed: {msg}")
            summary["error"] += 1
    print(f"\n{'=' * 40}")
    print(f"Summary: {summary['success']} converted, {summary['skipped']} skipped, {summary['error']} failed")
    return 0 if summary["error"] == 0 else 1


def _tottf_worker(src: Path, remove_source: bool) -> bool:
    dst = src.with_suffix(".ttf")
    cmd = [
        "fontforge",
        "-lang=ff",
        "-c",
        '"Open($1); Generate($2);"',
        str(src),
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            print(f"✓ {src.name}")
            if remove_source and src.exists():
                src.unlink()
            return True
        else:
            print(f"✘ {src.name}: {result.stderr.strip()}", file=sys.stderr)
            return False
    except Exception as exc:
        print(f"Error processing {src.name}: {exc}", file=sys.stderr)
        return False


def cmd_tottf(args: argparse.Namespace) -> int:
    extensions = [".svg", ".woff", ".eot", ".otf", ".ttc"]
    files = collect_files(args.paths, extensions)
    if not files:
        print("No matching font files found.", file=sys.stderr)
        return 1
    success = 0
    for src in files:
        if src.suffix.lower() != ".ttf" and _tottf_worker(src, args.remove_source):
            success += 1
    print(f"\nConverted {success}/{len(files)} files.")
    return 0


def _woff22ttf_worker(src: Path, keep_source: bool) -> bool:
    if not HAS_FONTTOOLS:
        print(
            "Error: fontTools is required. Install with: pip install fonttools",
            file=sys.stderr,
        )
        return False
    dst = src.with_suffix(".ttf")
    if dst.exists() and dst.stat().st_size:
        print(f"{src.name} already converted.")
        return True
    try:
        woff2.decompress(str(src), str(dst))
        print(f"{src.name} converted.")
        if not keep_source:
            src.unlink()
        return True
    except Exception as exc:
        print(f"Error converting {src.name}: {exc}", file=sys.stderr)
        return False


def cmd_woff22ttf(args: argparse.Namespace) -> int:
    if not HAS_FONTTOOLS:
        print(
            "Error: fontTools is required. Install with: pip install fonttools",
            file=sys.stderr,
        )
        return 1
    files = collect_files(args.paths, [".woff2"])
    if not files:
        print("No WOFF2 files found.", file=sys.stderr)
        return 1
    print(f"Found {len(files)} WOFF2 file(s)")
    run_parallel(_woff22ttf_worker, files, MAX_WORKERS, args.keep_source)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="font_toolkit.py",
        description="Merged font conversion toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_convert = subparsers.add_parser(
        "convert",
        help="Convert TTF/OTF/WOFF/WOFF2 fonts to another container flavour.",
    )
    p_convert.add_argument("paths", nargs="*", help="Font files or directories (default: scan cwd)")
    p_convert.add_argument("--to", required=True, choices=["ttf", "woff", "woff2"], help="Target format")
    p_convert.add_argument(
        "-r",
        "--rm",
        action="store_true",
        help="Delete source file after successful conversion",
    )
    p_convert.set_defaults(func=cmd_convert)
    p_otf2ttf = subparsers.add_parser(
        "otf2ttf",
        help="Convert OTF to TTF using fontTools (pure Python).",
    )
    p_otf2ttf.add_argument("paths", nargs="*", help="OTF files or directories (default: scan cwd)")
    p_otf2ttf.add_argument(
        "-k",
        "--keep-source",
        action="store_true",
        help="Keep original OTF files (default: remove)",
    )
    p_otf2ttf.set_defaults(func=cmd_otf2ttf)
    p_otf2ttf_ff = subparsers.add_parser(
        "otf2ttf-fontforge",
        help="Convert OTF to TTF using FontForge Python bindings.",
    )
    p_otf2ttf_ff.add_argument("paths", nargs="*", help="OTF files or directories (default: scan cwd)")
    p_otf2ttf_ff.add_argument(
        "-k",
        "--keep-source",
        action="store_true",
        help="Keep original OTF files (default: remove)",
    )
    p_otf2ttf_ff.set_defaults(func=cmd_otf2ttf_fontforge)
    p_tottf = subparsers.add_parser(
        "tottf",
        help="Convert SVG/WOFF/EOT/OTF/TTC to TTF using FontForge CLI.",
    )
    p_tottf.add_argument("paths", nargs="*", help="Font files or directories (default: scan cwd)")
    p_tottf.add_argument(
        "-r",
        "--remove-source",
        action="store_true",
        help="Delete source file after successful conversion",
    )
    p_tottf.set_defaults(func=cmd_tottf)
    p_woff22 = subparsers.add_parser(
        "woff22ttf",
        help="Decompress WOFF2 to TTF using fontTools.",
    )
    p_woff22.add_argument("paths", nargs="*", help="WOFF2 files or directories (default: scan cwd)")
    p_woff22.add_argument(
        "-k",
        "--keep-source",
        action="store_true",
        help="Keep original WOFF2 files (default: remove)",
    )
    p_woff22.set_defaults(func=cmd_woff22ttf)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nConversion interrupted by user.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
