#!/data/data/com.termux/files/usr/bin/env python
"""Compress or decompress files with snappy, lz4 or gzip through cramjam.
Takes a file or a folder, compresses every match (or decompresses the ``.sz``/``.lz4``/``.gz`` files again) with the chosen algorithm, guessing the algorithm from the extension when decompressing.
``--keep`` preserves the originals and ``--skip-existing`` avoids overwriting earlier output.
Usage: cramer PATH [--compress|--decompress] [--algo snappy|lz4|gzip] Positional arguments: path File or folder to process Options: -c, --compress Compress files (default) -d, --decompress Decompress files -a, --algo -k, --keep Keep the original files --dry-run Show what would happen, don't write or delete --skip-existing Skip files whose output already exists Examples: $ cramer data/ --algo lz4 $ cramer data/ --decompress Command-line script: run ``python cramer.py`` or the ``cramer`` command.
The module is importable as well."""

import argparse
import contextlib
import os
from pathlib import Path
import sys
from typing import Any

try:
    import cramjam as cj
except ImportError:
    print("cramjam is required: pip install cramjam", file=sys.stderr)
    sys.exit(1)
CODECS: dict[str, tuple[Any, ...]] = {
    "snappy": (
        ".sz",
        lambda d, l: bytes(cj.snappy.compress(d)),
        lambda d: bytes(cj.snappy.decompress(d)),
        None,
    ),
    "lz4": (
        ".lz4",
        lambda d, l: bytes(cj.lz4.compress(d)),
        lambda d: bytes(cj.lz4.decompress(d)),
        None,
    ),
    "gzip": (
        ".gz",
        lambda d, l: bytes(cj.gzip.compress(d, level=l)),
        lambda d: bytes(cj.gzip.decompress(d)),
        9,
    ),
    "deflate": (
        ".deflate",
        lambda d, l: bytes(cj.deflate.compress(d, level=l)),
        lambda d: bytes(cj.deflate.decompress(d)),
        9,
    ),
    "bzip2": (
        ".bz2",
        lambda d, l: bytes(cj.bzip2.compress(d, level=l)),
        lambda d: bytes(cj.bzip2.decompress(d)),
        9,
    ),
    "xz": (
        ".xz",
        lambda d, l: bytes(cj.xz.compress(d, level=l)),
        lambda d: bytes(cj.xz.decompress(d)),
        9,
    ),
    "zstd": (
        ".zst",
        lambda d, l: bytes(cj.zstd.compress(d, level=l)),
        lambda d: bytes(cj.zstd.decompress(d)),
        22,
    ),
    "brotli": (
        ".br",
        lambda d, l: bytes(cj.brotli.compress(d, level=l)),
        lambda d: bytes(cj.brotli.decompress(d)),
        11,
    ),
}
DEFAULT_ALGO: str = "snappy"
EXT_TO_ALGO: dict[Any, Any] = {ext: name for name, (ext, _, _, _) in CODECS.items()}


def compress_file(path: Path, algo: str, level: int, keep: bool, dry_run: bool) -> dict:
    ext, comp_fn, decomp_fn, _ = CODECS[algo]
    out_path = path.with_name(path.name + ext)
    result = {
        "input": str(path),
        "output": str(out_path),
        "algo": algo,
        "level": level,
        "ok": False,
        "removed": False,
        "in_bytes": None,
        "out_bytes": None,
        "ratio": None,
        "error": None,
    }
    try:
        result["in_bytes"] = path.stat().st_size
    except OSError as e:
        result["error"] = f"stat failed: {e}"
        return result
    if out_path.exists() and not dry_run:
        result["error"] = f"output already exists: {out_path}"
        return result
    if dry_run:
        result["ok"] = True
        return result
    try:
        data = path.read_bytes()
    except OSError as e:
        result["error"] = f"read failed: {e}"
        return result
    try:
        compressed = comp_fn(data, level) if level is not None else comp_fn(data, None)
    except Exception as e:
        result["error"] = f"compress failed: {type(e).__name__}: {e}"
        return result
    try:
        restored = decomp_fn(compressed)
        if restored != data:
            result["error"] = "round-trip mismatch (refusing to write)"
            return result
    except Exception as e:
        result["error"] = f"decompress failed: {type(e).__name__}: {e}"
        return result
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(compressed)
        os.replace(tmp_path, out_path)
    except OSError as e:
        result["error"] = f"write failed: {e}"
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return result
    result["out_bytes"] = len(compressed)
    result["ratio"] = result["in_bytes"] / len(compressed) if compressed else None
    result["ok"] = True
    if not keep:
        try:
            path.unlink()
            result["removed"] = True
        except OSError as e:
            result["error"] = f"compressed OK but unlink failed: {e}"
    return result


def decompress_file(path: Path, algo: str, keep: bool, dry_run: bool) -> dict:
    ext, comp_fn, decomp_fn, level = CODECS[algo]
    result = {
        "input": str(path),
        "output": None,
        "algo": algo,
        "level": None,
        "ok": False,
        "removed": False,
        "in_bytes": None,
        "out_bytes": None,
        "ratio": None,
        "error": None,
    }
    if not path.name.endswith(ext):
        result["error"] = f"file does not end with {ext}"
        return result
    out_path = path.with_name(path.name[: -len(ext)])
    result["output"] = str(out_path)
    try:
        result["in_bytes"] = path.stat().st_size
    except OSError as e:
        result["error"] = f"stat failed: {e}"
        return result
    if out_path.exists() and not dry_run:
        result["error"] = f"output already exists: {out_path}"
        return result
    if dry_run:
        result["ok"] = True
        return result
    try:
        data = path.read_bytes()
    except OSError as e:
        result["error"] = f"read failed: {e}"
        return result
    try:
        restored = decomp_fn(data)
    except Exception as e:
        result["error"] = f"decompress failed: {type(e).__name__}: {e}"
        return result
    try:
        recompressed = comp_fn(restored, level) if level is not None else comp_fn(restored, None)
        rechecked = decomp_fn(recompressed)
        if rechecked != restored:
            result["error"] = "round-trip mismatch (refusing to write)"
            return result
    except Exception as e:
        result["error"] = f"re-compress failed: {type(e).__name__}: {e}"
        return result
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(restored)
        os.replace(tmp_path, out_path)
    except OSError as e:
        result["error"] = f"write failed: {e}"
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return result
    result["out_bytes"] = len(restored)
    result["ratio"] = len(restored) / result["in_bytes"] if result["in_bytes"] else None
    result["ok"] = True
    if not keep:
        try:
            path.unlink()
            result["removed"] = True
        except OSError as e:
            result["error"] = f"decompressed OK but unlink failed: {e}"
    return result


def collect_files_for_compress(root: Path, skip_suffixes: Any) -> list[Path]:
    if root.is_file():
        return [] if root.suffix.lower() in skip_suffixes else [root]
    if root.is_dir():
        return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() not in skip_suffixes]
    return []


def collect_files_for_decompress(root: Path, algo_filter: Any):
    def matches(p: Path) -> bool:
        if algo_filter is not None:
            return p.name.endswith(CODECS[algo_filter][0])
        return any(p.name.endswith(ext) for ext in EXT_TO_ALGO)

    if root.is_file():
        return [root] if matches(root) else []
    if root.is_dir():
        return [p for p in sorted(root.rglob("*")) if p.is_file() and matches(p)]
    return []


def detect_algo(path: Path, forced: str) -> str | None:
    if forced is not None:
        return forced
    for ext in sorted(EXT_TO_ALGO, key=len, reverse=True):
        if path.name.endswith(ext):
            return EXT_TO_ALGO[ext]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", type=Path, help="File or folder to process")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-c",
        "--compress",
        dest="mode",
        action="store_const",
        const="compress",
        help="Compress files (default)",
    )
    mode.add_argument(
        "-d",
        "--decompress",
        dest="mode",
        action="store_const",
        const="decompress",
        help="Decompress files",
    )
    parser.set_defaults(mode="compress")
    parser.add_argument(
        "-a",
        "--algo",
        default=None,
        choices=sorted(CODECS.keys()),
        help=f"cramjam algorithm (compress default: {DEFAULT_ALGO}; decompress default: auto by extension)",
    )
    parser.add_argument("-k", "--keep", action="store_true", help="Keep the original files")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen, don't write or delete",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files whose output already exists",
    )
    args = parser.parse_args()
    if not args.path.exists():
        print(f"Path not found: {args.path}", file=sys.stderr)
        sys.exit(1)
    if args.mode == "compress":
        algo = args.algo or DEFAULT_ALGO
        ext, _, _, level = CODECS[algo]
        skip_suffixes = {
            ".gz",
            ".bz2",
            ".xz",
            ".zst",
            ".br",
            ".lz4",
            ".sz",
            ".deflate",
            ".tmp",
        }
        files = collect_files_for_compress(args.path, skip_suffixes)
        if not files:
            print("No input files to compress.", file=sys.stderr)
            sys.exit(0)
        level_str = f" (level {level}, max)" if level is not None else " (no level)"
        print(f"Mode     : compress")
        print(f"Algorithm: {algo}{level_str}")
        print(f"Target   : {args.path}")
        print(
            f"Files    : {len(files)}"
            + ("  [dry-run]" if args.dry_run else "")
            + ("  [keep originals]" if args.keep else "  [originals will be deleted]")
        )
        print()
        total_in = total_out = 0
        n_ok = n_fail = n_removed = 0
        for path in files:
            if args.skip_existing and path.with_name(path.name + ext).exists():
                print(f"  SKIP  {path}  (output exists)")
                continue
            r = compress_file(path, algo, level, args.keep, args.dry_run)
            if r["error"]:
                n_fail += 1
                print(f"  FAIL  {path}  -> {r['error']}")
                continue
            if args.dry_run:
                print(f"  PLAN  {path}  ->  {r['output']}")
                continue
            n_ok += 1
            total_in += r["in_bytes"]
            total_out += r["out_bytes"]
            if r["removed"]:
                n_removed += 1
            ratio = f"{r['ratio']:.2f}x" if r["ratio"] else "?"
            flag = "  (kept original)" if not r["removed"] else ""
            print(f"  OK    {path}  ->  {r['output']}  [{r['in_bytes']:,} -> {r['out_bytes']:,} B, {ratio}]{flag}")
        print()
        if args.dry_run:
            print(f"Dry run complete. {len(files)} file(s) would be compressed.")
            return
        print(f"Done. ok={n_ok}  failed={n_fail}  originals_removed={n_removed}")
        if total_in:
            print(f"Total: {total_in:,} B -> {total_out:,} B ({total_in / total_out:.2f}x)")
    else:
        algo_filter = args.algo
        files = collect_files_for_decompress(args.path, algo_filter)
        if not files:
            target = algo_filter or "any known codec"
            print(f"No files found for decompression ({target}).", file=sys.stderr)
            sys.exit(0)
        print(f"Mode     : decompress")
        print(f"Algorithm: {algo_filter or 'auto by extension'}")
        print(f"Target   : {args.path}")
        print(
            f"Files    : {len(files)}"
            + ("  [dry-run]" if args.dry_run else "")
            + ("  [keep originals]" if args.keep else "  [compressed files will be deleted]")
        )
        print()
        total_in = total_out = 0
        n_ok = n_fail = n_removed = n_skipped = 0
        for path in files:
            algo = detect_algo(path, algo_filter)
            if algo is None:
                n_skipped += 1
                print(f"  SKIP  {path}  (unknown compression suffix)")
                continue
            ext = CODECS[algo][0]
            expected_out = path.with_name(path.name[: -len(ext)])
            if args.skip_existing and expected_out.exists():
                print(f"  SKIP  {path}  (output exists)")
                continue
            r = decompress_file(path, algo, args.keep, args.dry_run)
            if r["error"]:
                n_fail += 1
                print(f"  FAIL  {path}  -> {r['error']}")
                continue
            if args.dry_run:
                print(f"  PLAN  {path}  ->  {r['output']}  [{algo}]")
                continue
            n_ok += 1
            total_in += r["in_bytes"]
            total_out += r["out_bytes"]
            if r["removed"]:
                n_removed += 1
            ratio = f"{r['ratio']:.2f}x" if r["ratio"] else "?"
            flag = "  (kept original)" if not r["removed"] else ""
            print(
                f"  OK    {path}  ->  {r['output']}  [{algo}]  "
                f"[{r['in_bytes']:,} -> {r['out_bytes']:,} B, {ratio}]{flag}"
            )
        print()
        if args.dry_run:
            print(f"Dry run complete. {len(files)} file(s) would be decompressed.")
            return
        print(f"Done. ok={n_ok}  failed={n_fail}  skipped={n_skipped}  originals_removed={n_removed}")
        if total_in:
            print(f"Total: {total_in:,} B -> {total_out:,} B ({total_out / total_in:.2f}x)")


if __name__ == "__main__":
    main()
