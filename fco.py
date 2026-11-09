#!/data/data/com.termux/files/usr/bin/env python
"""Convert fonts between TTF, OTF, WOFF and WOFF2.
Detects the input format (Type 1 ``pfa``/``pfb`` are converted to a CFF-outlined OTF), writes the requested ``--to`` format next to the source (or under ``-o``), and offers ``-r`` to drop the original, ``-f`` to overwrite and ``--dry-run``; the files are processed in parallel.
Usage: fco [fonts ...] --to ttf|otf|woff|woff2 [-r] [-o OUTDIR] Positional arguments: inputs ...
Input font files or directories (default: current directory, recursive) Options: --to, -t -r, --remove Remove original file after successful conversion -o, --output-dir Output directory (default: alongside each input) -f, --force Overwrite existing output files --dry-run List files that would be converted without converting -v, --verbose Enable verbose (DEBUG) logging --allow-outline-mismatch Allow TTF↔OTF conversions even when outlines do not match the target convention.
This writes a non-standard font and only changes the sfnt version; outlines are NOT converted.
Examples: $ fco fonts/*.ttf --to woff2 Command-line script: run ``python fco.py`` or the ``fco`` command.
The module is importable as well."""

import argparse
import contextlib
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
import sys
import time
from typing import TYPE_CHECKING, Optional, Sequence

from dh import MAX_WORKERS, fsz
from loguru import logger


if TYPE_CHECKING:
    from fontTools.misc.psCharStrings import T2CharString


try:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.t1Lib import T1Font
    from fontTools.ttLib import TTFont
except ImportError:
    sys.stderr.write("fonttools is not installed.\n  pip install fonttools\n")
    sys.exit(1)
try:
    import brotli

    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False
INPUT_FORMATS = frozenset({"ttf", "otf", "woff", "woff2", "pfa", "pfb"})
OUTPUT_FORMATS = frozenset({"ttf", "otf", "woff", "woff2"})
TYPE1_FORMATS = frozenset({"pfa", "pfb"})
SUPPORTED_FORMATS = INPUT_FORMATS
SFNT_VERSIONS = {
    "ttf": 0x00010000,
    "otf": "OTTO",
}
FLAVORS = {
    "woff": "woff",
    "woff2": "woff2",
}
DEFAULT_OUTPUT_FORMAT = "woff2"
CFF_TABLES = frozenset({"CFF ", "CFF2"})
TRUETYPE_TABLE = "glyf"


@dataclass
class ConversionResult:
    input: Path
    output_format: str
    output: Optional[Path] = None
    input_format: Optional[str] = None
    input_size: int = 0
    output_size: int = 0
    time: float = 0.0
    success: bool = False
    skipped: bool = False
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    warning: Optional[str] = None
    removed_original: bool = False

    @property
    def failed(self) -> bool:
        return not self.success and not self.skipped


def detect_format(path: Path) -> Optional[str]:
    ext = path.suffix.lower().lstrip(".")
    return ext if ext in INPUT_FORMATS else None


def generate_output_path(
    input_path: Path,
    output_format: str,
    output_dir: Optional[Path] = None,
) -> Path:
    if output_dir is not None:
        return output_dir / f"{input_path.stem}.{output_format}"
    return input_path.with_suffix(f".{output_format}")


def _outline_kind(font: TTFont) -> str:
    tags = set(font.keys())
    if tags & CFF_TABLES:
        return "cff"
    if TRUETYPE_TABLE in tags:
        return "truetype"
    return "unknown"


def _t1_to_ttfont(input_path: Path) -> TTFont:
    t1 = T1Font(str(input_path))
    glyph_set = t1.getGlyphSet()
    glyph_order: list[str] = list(glyph_set.keys())
    charstrings: dict[str, T2CharString] = {}
    metrics: dict[str, tuple[int, int]] = {}
    for name in glyph_order:
        glyph = glyph_set[name]
        width = round(glyph.width)
        pen = T2CharStringPen(width, glyph_set)
        glyph.draw(pen)
        charstrings[name] = pen.getCharString()
        metrics[name] = (width, 0)
    fm = getattr(t1, "fontMatrix", (0.001, 0, 0, 0.001, 0, 0))
    upm = round(1 / abs(fm[0])) if fm and fm[0] else 1000
    bbox = getattr(t1, "fontBBox", None)
    if bbox and len(bbox) >= 4:
        ascent = round(bbox[3])
        descent = round(bbox[1])
    else:
        ascent, descent = 880, -120
    fb = FontBuilder(unitsPerEm=upm, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({})
    fb.setupCFF(
        t1.fontName,
        {"FullName": t1.fontName, "FamilyName": t1.fontName, "Weight": "Regular"},
        charstrings,
        {},
    )
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ascent, descent=descent)
    fb.setupNameTable({"familyName": t1.fontName, "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    return fb.font


def convert_font(
    input_path: Path,
    output_format: str,
    remove_original: bool,
    output_dir: Optional[Path],
    force: bool,
    allow_outline_mismatch: bool,
) -> ConversionResult:
    start = time.perf_counter()
    result = ConversionResult(input=input_path, output_format=output_format)
    output_existed_before = False
    try:
        input_format = detect_format(input_path)
        if input_format is None:
            msg = f"unsupported extension '{input_path.suffix}'"
            raise ValueError(msg)
        if output_format not in OUTPUT_FORMATS:
            msg = f"cannot write to .{output_format} (input-only format)"
            raise ValueError(msg)
        result.input_format = input_format
        result.input_size = input_path.stat().st_size
        if input_format == output_format:
            result.skipped = True
            result.skipped_reason = f"already .{output_format}"
            return result
        output_path = generate_output_path(input_path, output_format, output_dir)
        result.output = output_path
        output_existed_before = output_path.exists()
        if output_existed_before and not force:
            msg = f"output exists (use --force): {output_path}"
            raise FileExistsError(msg)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if input_format in TYPE1_FORMATS:
            font = _t1_to_ttfont(input_path)
        else:
            font = TTFont(
                str(input_path),
                lazy=True,
                recalcBBoxes=False,
                recalcTimestamp=False,
            )
        try:
            kind = _outline_kind(font)
            warning: Optional[str] = None
            if output_format in FLAVORS:
                font.flavor = FLAVORS[output_format]
            else:
                font.flavor = None
                if output_format == "otf":
                    if kind == "cff":
                        pass
                    elif kind == "truetype":
                        if not allow_outline_mismatch:
                            result.skipped = True
                            result.skipped_reason = (
                                "source has TrueType (glyf) outlines; .otf "
                                "conventionally uses CFF. Use "
                                "--allow-outline-mismatch to write a "
                                "non-standard .otf anyway."
                            )
                            return result
                        warning = (
                            "font has TrueType outlines; .otf conventionally uses CFF — outlines were NOT converted"
                        )
                    else:
                        if not allow_outline_mismatch:
                            result.skipped = True
                            result.skipped_reason = "no recognizable CFF outlines for .otf"
                            return result
                        warning = "no recognizable CFF outlines; .otf may be invalid"
                    font.sfntVersion = SFNT_VERSIONS["otf"]
                elif output_format == "ttf":
                    if kind == "truetype":
                        pass
                    elif kind == "cff":
                        if not allow_outline_mismatch:
                            result.skipped = True
                            result.skipped_reason = (
                                "source has CFF outlines; .ttf conventionally "
                                "uses TrueType outlines. Use "
                                "--allow-outline-mismatch to write a "
                                "non-standard .ttf anyway."
                            )
                            return result
                        warning = (
                            "font has CFF outlines; .ttf conventionally uses TrueType — outlines were NOT converted"
                        )
                    else:
                        if not allow_outline_mismatch:
                            result.skipped = True
                            result.skipped_reason = "no recognizable TrueType outlines for .ttf"
                            return result
                        warning = "no recognizable TrueType outlines; .ttf may be invalid"
                    font.sfntVersion = SFNT_VERSIONS["ttf"]
                else:
                    msg = f"unsupported output format: {output_format}"
                    raise ValueError(msg)
            font.save(str(output_path))
            result.output_size = output_path.stat().st_size
            result.success = True
            result.warning = warning
            if (
                remove_original
                and input_path.resolve() != output_path.resolve()
                and output_path.exists()
                and output_path.stat().st_size > 0
            ):
                try:
                    input_path.unlink()
                    result.removed_original = True
                except OSError as exc:
                    extra = f"conversion succeeded but could not remove original: {exc}"
                    result.warning = f"{result.warning}; {extra}" if result.warning else extra
        finally:
            with contextlib.suppress(Exception):
                font.close()
    except Exception as exc:
        result.error = str(exc)
        if result.output is not None and not output_existed_before and result.output.exists() and not result.success:
            with contextlib.suppress(OSError):
                result.output.unlink()
    finally:
        result.time = time.perf_counter() - start
    return result


def find_font_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if detect_format(path):
                files.append(path)
            else:
                logger.warning("skipping non-font file: {}", path)
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if candidate.is_file() and detect_format(candidate):
                    files.append(candidate)
        else:
            logger.warning("path not found: {}", path)
    seen = set()
    unique: list[Path] = []
    for f in files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)
    return unique


def print_file_stats(result: ConversionResult) -> None:
    name = result.input.name
    if result.success:
        ratio = result.output_size / result.input_size * 100 if result.input_size else 0.0
        saved = (1 - result.output_size / result.input_size) * 100 if result.input_size else 0.0
        print(f"  ✓ {name}")
        print(
            f"      {fsz(result.input_size)} → {fsz(result.output_size)}  "
            f"({ratio:.1f}% of original, {saved:+.1f}% change)"
        )
        print(f"      Time: {result.time:.3f}s")
        if result.warning:
            logger.warning("      ⚠  {}", result.warning)
        if result.removed_original:
            print("      🗑  original removed")
    elif result.skipped:
        print(f"  ↷ {name} — SKIPPED: {result.skipped_reason}")
    else:
        print(f"  ✗ {name} — ERROR: {result.error}", file=sys.stderr)


def print_summary(results: Sequence[ConversionResult]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.success)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if r.failed)
    print()
    print("=" * 40)
    print("Summary")
    print("-" * 40)
    print(f"  Files processed : {total}")
    print(f"  Successful      : {ok}")
    print(f"  Skipped         : {skipped}")
    print(f"  Failed          : {failed}")
    if ok:
        total_in = sum(r.input_size for r in results if r.success)
        total_out = sum(r.output_size for r in results if r.success)
        total_time = sum(r.time for r in results if r.success)
        print(f"  Input size      : {fsz(total_in)}")
        print(f"  Output size     : {fsz(total_out)}")
        if total_in:
            print(f"  Ratio           : {total_out / total_in * 100:.1f}% of original")
        print(f"  Total time      : {total_time:.3f}s")
        if total > 1:
            print(f"  Avg per file    : {total_time / total:.3f}s")
    print("=" * 40)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fontconverter.py",
        description=(
            "Convert font files between TTF, OTF, WOFF, and WOFF2. "
            "PFA/PFB (Type 1) are accepted as input and converted to a CFF-outlined OTF."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                              Convert all fonts in cwd → woff2
  %(prog)s font.ttf --to woff            Single file → woff
  %(prog)s ./fonts/ --to ttf -r          Dir → ttf, remove originals
  %(prog)s a.ttf b.otf --to woff2        Two files
  %(prog)s ./fonts/ --to otf -o ./out/   Output to ./out/ directory
  %(prog)s font.pfa --to otf             Type 1 → OTF (CFF outlines)
  %(prog)s ./fonts/ --to otf --allow-outline-mismatch
                                        Allow non-standard TTF↔OTF mismatch
""",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Input font files or directories (default: current directory, recursive)",
    )
    parser.add_argument(
        "--to",
        "-t",
        dest="output_format",
        choices=sorted(OUTPUT_FORMATS),
        default=DEFAULT_OUTPUT_FORMAT,
        help=f"Output format (default: {DEFAULT_OUTPUT_FORMAT})",
    )
    parser.add_argument(
        "-r",
        "--remove",
        default=True,
        action="store_true",
        help="Remove original file after successful conversion",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: alongside each input)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite existing output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without converting",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    parser.add_argument(
        "--allow-outline-mismatch",
        action="store_true",
        help=(
            "Allow TTF↔OTF conversions even when outlines do not match the "
            "target convention. This writes a non-standard font and only "
            "changes the sfnt version; outlines are NOT converted."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "WARNING",
        format="<level>{level: <8}</level> | <level>{message}</level>",
    )
    if args.output_format == "woff2" and not HAS_BROTLI:
        sys.stderr.write("WOFF2 output requires brotli.\n  pip install brotli\n")
        return 1
    input_paths = list(args.inputs) if args.inputs else [Path.cwd()]
    font_files = find_font_files(input_paths)
    if not font_files:
        print("No font files found.")
        return 0
    to_convert: list[Path] = []
    already_target: list[Path] = []
    for f in font_files:
        if detect_format(f) == args.output_format:
            already_target.append(f)
        else:
            to_convert.append(f)
    if already_target:
        print(f"Skipping {len(already_target)} file(s) already in .{args.output_format} format")
    if not to_convert:
        print("Nothing to convert.")
        return 0
    print()
    print(f"Converting {len(to_convert)} file(s) → .{args.output_format}")
    if args.remove:
        print("  (originals will be removed on success)")
    if args.allow_outline_mismatch:
        print("  (outline mismatches allowed; output may be non-standard)")
    print()
    if args.dry_run:
        for f in to_convert:
            out = generate_output_path(f, args.output_format, args.output_dir)
            print(f"  {f}  →  {out}")
        return 0
    worker_args = [
        (
            f,
            args.output_format,
            args.remove,
            args.output_dir,
            args.force,
            args.allow_outline_mismatch,
        )
        for f in to_convert
    ]
    all_results: list[ConversionResult] = []
    if len(worker_args) == 1:
        result = convert_font(*worker_args[0])
        all_results.append(result)
        print_file_stats(result)
    else:
        workers = min(MAX_WORKERS, len(worker_args))
        pool = Pool(processes=workers)
        try:
            async_results = [pool.apply_async(convert_font, args=wa) for wa in worker_args]
            try:
                for ar in async_results:
                    result = ar.get()
                    all_results.append(result)
                    print_file_stats(result)
            except KeyboardInterrupt:
                print()
                print("Interrupted — terminating pool …")
                pool.terminate()
                pool.join()
                for ar in async_results:
                    if ar.ready():
                        with contextlib.suppress(Exception):
                            all_results.append(ar.get(timeout=0))
                print_summary(all_results)
                return 130
        finally:
            with contextlib.suppress(Exception):
                pool.close()
            with contextlib.suppress(Exception):
                pool.join()
    print_summary(all_results)
    failed = sum(1 for r in all_results if r.failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
