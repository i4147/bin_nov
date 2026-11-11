#!/data/data/com.termux/files/usr/bin/env python
"""Unified archive extractor, fixer and integrity checker.
Subcommands: ``extract`` unpacks 7z/zip/rar/tar and the stream-compressed formats with a Python backend or the external tools, ``fix`` repairs the directory and file name collisions of a bad extraction, ``whl`` unpacks the wheels into version-stripped folders and ``check`` verifies the archives without extracting them.
``--organize``, ``--keep`` and ``--dry-run`` control the layout and the cleanup.
Usage: archive_tools {extract,fix,whl,check} [options] [paths ...] Positional arguments: paths ...
Files or directories to process (default: cwd).
root Root directory to scan (default: cwd).
Subcommands: extract Extract archives.
fix Fix mis-extracted dir/file name collisions.
whl Extract .whl wheels into version-stripped dirs.
check Integrity-check archives without extracting.
Options: -r, --recursive Recurse into directories.
-e, --engine Extraction backend (default: auto = try python, fall back to CLI tools).
-k, --keep Keep the original archive after extraction.
-n, --dry-run Only print what would be done.
--organize Flat = extract next to the archive; stem = subdir named after archive stem; versioned = stem with version stripped (whl-style).
--single-file-subdir Ar_extract.py behaviour: only create a subdir when the archive's contents share a single top-level directory.
--subdir-truncate Truncate subdir names to first N characters (subdir.py uses 8).
--formats Comma-separated extensions to process (e.g.
.tar.gz,.zip).
--integrity-check Validate each archive before extracting.
-o, --output-dir Force a single output directory (overrides --organize).
...
plus 3 more; run ``archive_tools --help`` for the full list.
Examples: $ archive_tools extract -r downloads/ Command-line script: run ``python archive_tools.py`` or the ``archive_tools`` command.
The module is importable as well."""

import argparse
import bz2
import contextlib
from dataclasses import dataclass
import gzip
import lzma
import multiprocessing as mp
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Iterable, Iterator, Optional, Sequence
import zipfile

from dh import MAX_WORKERS


try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import brotli
except ImportError:
    brotli = None
try:
    import lz4.frame as lz4frame
except ImportError:
    lz4frame = None
DEFAULT_JOBS: int = 8
DEFAULT_TIMEOUT: int = 300
CHUNK: int = 1024 * 1024
CLI_COMMANDS: dict[str, list[str]] = {
    ".7z": ["7z", "x", "-y"],
    ".zip": ["unzip", "-o"],
    ".rar": ["unrar", "x", "-y"],
    ".tar": ["tar", "-xf"],
    ".tar.gz": ["tar", "-xzf"],
    ".tgz": ["tar", "-xzf"],
    ".tar.bz2": ["tar", "-xjf"],
    ".tbz2": ["tar", "-xjf"],
    ".tar.xz": ["tar", "-xJf"],
    ".txz": ["tar", "-xJf"],
    ".gz": ["gunzip", "-f"],
    ".bz2": ["bunzip2", "-f"],
    ".xz": ["unxz", "-f"],
    ".lz4": ["lz4", "-d", "-f"],
    ".lzma": ["unlzma", "-f"],
    ".zst": ["unzstd", "-f"],
    ".cab": ["cabextract"],
    ".arj": ["arj", "x", "-y"],
    ".ace": ["unace", "x"],
}
ALL_EXT: tuple[str, ...] = tuple(
    sorted(
        {
            ".tar.gz",
            ".tar.bz2",
            ".tar.xz",
            ".tar.zst",
            ".tar.br",
            ".tar.lz4",
            ".tgz",
            ".tbz2",
            ".txz",
            ".whl",
            ".7z",
            ".zip",
            ".rar",
            ".tar",
            ".cab",
            ".arj",
            ".ace",
            ".gz",
            ".bz2",
            ".xz",
            ".lz4",
            ".lzma",
            ".zst",
            ".br",
        },
        key=len,
        reverse=True,
    )
)


@dataclass(slots=True)
class ExtractResult:
    archive_path: Path
    status: str = "failed"
    output_dir: Optional[Path] = None
    extracted_files: int = 0
    extracted_size: int = 0
    extraction_time: float = 0.0
    original_size: int = 0
    error_message: str = ""

    def __str__(self) -> str:
        mb = self.original_size / (1024 * 1024)
        icon = {"success": "✓", "failed": "✗", "skipped": "○"}.get(self.status, "?")
        out = f"{icon} {self.archive_path.name} [{mb:.1f}MB] - {self.status}"
        if self.status == "success":
            out += f" ({self.extracted_files} files, {self.extraction_time:.1f}s)"
            if self.output_dir:
                out += f" → {self.output_dir.name}"
        elif self.status == "failed" and self.error_message:
            out += f" - {self.error_message}"
        return out


def count_files(p: Path) -> int:
    try:
        return sum(1 for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def unique_dir(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.name}_{i}")
        if not q.exists():
            return q
        i += 1


def strip_version(stem: str) -> str:
    parts = stem.replace(".whl", "").split("-")
    for i, part in enumerate(parts):
        if part and part[0].isdigit():
            return "-".join(parts[:i])
    return parts[0]


def detect_extension(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith(".whl"):
        return ".whl"
    if name.endswith(".br"):
        return ".br"
    if name.endswith(".tar.lz4"):
        return ".tar.lz4"
    for ext in sorted(CLI_COMMANDS.keys(), key=len, reverse=True):
        if name.endswith(ext):
            return ext
    return None


@contextlib.contextmanager
def _open_decompressor(path: Path, kind: str) -> Iterator:
    if kind == "gz":
        with gzip.open(path, "rb") as f:
            yield f
    elif kind == "bz2":
        with bz2.open(path, "rb") as f:
            yield f
    elif kind == "xz":
        with lzma.open(path, "rb") as f:
            yield f
    elif kind == "zst":
        if zstd is None:
            msg = "zstandard is not installed"
            raise RuntimeError(msg)
        with open(path, "rb") as raw, zstd.ZstdDecompressor().stream_reader(raw) as f:
            yield f
    elif kind == "br":
        if brotli is None:
            msg = "brotli is not installed"
            raise RuntimeError(msg)
        import io

        yield io.BytesIO(brotli.decompress(path.read_bytes()))
    elif kind == "lz4":
        if lz4frame is None:
            msg = "lz4 is not installed"
            raise RuntimeError(msg)
        with lz4frame.open(path, "rb") as f:
            yield f
    else:
        msg = f"unknown compression kind: {kind}"
        raise ValueError(msg)


def _decompress_stream(src: Path, dst: Path, kind: str) -> None:
    with _open_decompressor(src, kind) as f, open(dst, "wb") as o:
        shutil.copyfileobj(f, o, CHUNK)


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(path=dest, filter="data")
    except TypeError:
        tar.extractall(path=dest, filter="data")


def _extract_tar_python(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    for sfx, kind in ((".tar.zst", "zst"), (".tar.br", "br"), (".tar.lz4", "lz4")):
        if name.endswith(sfx):
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
                tmp = Path(tf.name)
            try:
                _decompress_stream(archive, tmp, kind)
                with tarfile.open(tmp, "r:") as tar:
                    _safe_extract_tar(tar, dest)
            finally:
                tmp.unlink(missing_ok=True)
            return
    if name.endswith((".tar.gz", ".tgz")):
        mode = "r:gz"
    elif name.endswith((".tar.bz2", ".tbz2")):
        mode = "r:bz2"
    elif name.endswith((".tar.xz", ".txz")):
        mode = "r:xz"
    elif name.endswith(".tar"):
        mode = "r:"
    else:
        msg = f"not a tar: {archive.name}"
        raise ValueError(msg)
    with tarfile.open(archive, mode) as tar:
        _safe_extract_tar(tar, dest)


def extract_with_python(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    tar_suffixes = (
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
        ".tar.zst",
        ".tar.br",
        ".tar.lz4",
    )
    if name.endswith(tar_suffixes):
        _extract_tar_python(archive, dest)
        return
    if name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(path=dest)
        return
    if name.endswith(".7z"):
        if py7zr is None:
            msg = "py7zr is not installed"
            raise RuntimeError(msg)
        with py7zr.SevenZipFile(archive, mode="r") as sz:
            sz.extractall(path=dest)
        return
    for ext, kind in (
        (".gz", "gz"),
        (".bz2", "bz2"),
        (".xz", "xz"),
        (".lzma", "xz"),
        (".zst", "zst"),
        (".br", "br"),
        (".lz4", "lz4"),
    ):
        if name.endswith(ext):
            out_name = archive.name[: -len(ext)]
            _decompress_stream(archive, dest / out_name, kind)
            return
    msg = f"python engine does not support: {archive.name}"
    raise ValueError(msg)


def extract_with_external(archive: Path, dest: Path, cwd: Path) -> None:
    ext = detect_extension(archive)
    if not ext or ext not in CLI_COMMANDS:
        msg = f"no external tool for: {archive.name}"
        raise ValueError(msg)
    cmd = list(CLI_COMMANDS[ext])
    tool = cmd[0]
    if shutil.which(tool) is None:
        msg = f"tool '{tool}' not found in PATH"
        raise RuntimeError(msg)
    dest.mkdir(parents=True, exist_ok=True)
    if ext == ".7z":
        cmd += [f"-o{dest}", str(archive)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext == ".zip":
        cmd += [str(archive), "-d", str(dest)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"):
        cmd += ["-C", str(dest), str(archive)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext == ".rar":
        cmd += [str(archive), str(dest)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext in (".gz", ".bz2", ".xz", ".lzma", ".zst", ".lz4"):
        local = dest / archive.name
        shutil.copy2(archive, local)
        cmd.append(local.name)
        try:
            subprocess.run(
                cmd,
                cwd=dest,
                capture_output=True,
                text=True,
                check=True,
                timeout=DEFAULT_TIMEOUT,
            )
        finally:
            local.unlink(missing_ok=True)
        return
    cmd.append(str(archive))
    if dest != cwd:
        cmd.append(str(dest))
    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        timeout=DEFAULT_TIMEOUT,
    )


def _try_extract(archive: Path, dest: Path, engine: str) -> None:
    if engine == "python":
        extract_with_python(archive, dest)
    elif engine == "external":
        extract_with_external(archive, dest, archive.parent)
    elif engine == "auto":
        errs: list[str] = []
        try:
            extract_with_python(archive, dest)
            return
        except Exception as e:
            errs.append(f"python: {e}")
        try:
            extract_with_external(archive, dest, archive.parent)
            return
        except Exception as e:
            errs.append(f"external: {e}")
        raise RuntimeError(" | ".join(errs))
    else:
        msg = f"unknown engine: {engine}"
        raise ValueError(msg)


def should_use_subdir(archive: Path) -> bool:
    name = archive.name.lower()
    for ext in (".gz", ".bz2", ".xz", ".lz4", ".lzma", ".zst", ".br"):
        if name.endswith(ext) and ".tar." not in name:
            return True
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
        elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(archive, "r:*") as tar:
                names = tar.getnames()
        else:
            return False
        parts = [Path(n).parts for n in names if n]
        if not parts:
            return False
        roots = {p[0] for p in parts}
        return len(roots) == 1 and any(len(p) > 1 for p in parts)
    except Exception:
        return False


def check_integrity(archive: Path) -> tuple[bool, str]:
    name = archive.name.lower()
    try:
        if name.endswith((".zip", ".whl")):
            if not zipfile.is_zipfile(archive):
                return False, f"Invalid zip: {archive.name}"
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad is not None:
                    return False, f"Corrupted: {archive.name} (bad member {bad})"
            return True, f"Valid: {archive.name}"
        if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            if not tarfile.is_tarfile(archive):
                return False, f"Invalid tar: {archive.name}"
            with tarfile.open(archive, "r:*") as tar:
                tar.getmembers()
            return True, f"Valid: {archive.name}"
        if name.endswith(".tar.zst"):
            with _open_decompressor(archive, "zst") as f:
                while f.read(CHUNK):
                    pass
            return True, f"Valid: {archive.name}"
        if name.endswith(".tar.br"):
            with _open_decompressor(archive, "br") as f:
                while f.read(CHUNK):
                    pass
            return True, f"Valid: {archive.name}"
        if name.endswith(".7z"):
            if py7zr is None:
                return False, f"py7zr not installed: {archive.name}"
            with py7zr.SevenZipFile(archive) as sz:
                sz.getnames()
            return True, f"Valid: {archive.name}"
        for ext, kind in (
            (".gz", "gz"),
            (".bz2", "bz2"),
            (".xz", "xz"),
            (".zst", "zst"),
            (".br", "br"),
            (".lz4", "lz4"),
        ):
            if name.endswith(ext):
                with _open_decompressor(archive, kind) as f:
                    while f.read(CHUNK):
                        pass
                return True, f"Valid: {archive.name}"
    except Exception as e:
        return False, f"Corrupted: {archive.name} ({e})"
    return True, f"Skipped check: {archive.name}"


def find_archives(roots: Iterable[Path], recursive: bool, formats: Optional[set[str]] = None) -> list[Path]:
    exts = formats or set(ALL_EXT)
    ordered = sorted(exts, key=len, reverse=True)
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            found.append(root)
            continue
        if not root.is_dir():
            continue
        it = root.rglob("*") if recursive else root.iterdir()
        for p in it:
            if not p.is_file():
                continue
            low = p.name.lower()
            if any(low.endswith(e) for e in ordered) and p not in seen:
                found.append(p)
                seen.add(p)
    return sorted(found)


def extract_one(
    archive: Path,
    *,
    engine: str = "auto",
    keep: bool = False,
    dry_run: bool = False,
    organize: str = "flat",
    out_dir: Optional[Path] = None,
    single_file_subdir: bool = False,
    subdir_truncate: int = 0,
    quiet: bool = False,
) -> ExtractResult:
    start = time.time()
    size = archive.stat().st_size if archive.exists() else 0
    res = ExtractResult(archive_path=archive, original_size=size)
    if not archive.exists():
        res.error_message = "file not found"
        res.extraction_time = time.time() - start
        return res
    if dry_run:
        res.status = "success"
        res.extraction_time = time.time() - start
        if not quiet:
            print(f"[DRY RUN] would extract: {archive.name}")
        return res
    parent = archive.parent
    stem = archive.stem
    if subdir_truncate > 0:
        stem = stem[:subdir_truncate]
    use_subdir = False
    if organize == "flat":
        if single_file_subdir and should_use_subdir(archive):
            dest = parent / stem
            use_subdir = True
        else:
            dest = out_dir or parent
    elif organize == "stem":
        dest = parent / stem
        use_subdir = True
    elif organize == "versioned":
        dest = unique_dir(parent / strip_version(archive.stem))
        use_subdir = True
    else:
        dest = out_dir or parent
    try:
        _try_extract(archive, dest, engine)
    except Exception as e:
        res.status = "failed"
        msg = str(e)
        res.error_message = msg.splitlines()[0][:200] if msg else type(e).__name__
        res.extraction_time = time.time() - start
        return res
    res.status = "success"
    res.output_dir = dest if use_subdir else None
    res.extracted_files = count_files(dest if use_subdir else parent)
    res.extracted_size = dir_size(dest if use_subdir else parent)
    if not keep:
        try:
            archive.unlink()
        except OSError as e:
            res.error_message = f"extracted, but couldn't remove original: {e}"
    res.extraction_time = time.time() - start
    return res


def _worker(payload: tuple[Path, dict]) -> ExtractResult:
    archive, opts = payload
    return extract_one(archive, **opts)


def _parse_formats(s: Optional[str]) -> Optional[set[str]]:
    if not s:
        return None
    out: set[str] = set()
    for tok in s.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if not tok.startswith("."):
            tok = "." + tok
        out.add(tok)
    return out


def _summary(results: Sequence[ExtractResult], elapsed: float, *, quiet: bool) -> None:
    ok = sum(1 for r in results if r.status == "success")
    fail = sum(1 for r in results if r.status == "failed")
    skip = sum(1 for r in results if r.status == "skipped")
    if quiet:
        return
    print("\n" + "=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"Total: {len(results)}")
    print(f"✓ Success: {ok}")
    print(f"✗ Failed : {fail}")
    print(f"○ Skipped: {skip}")
    print(f"Time    : {elapsed:.1f}s")


def cmd_extract(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    formats = _parse_formats(args.formats)
    archives = find_archives(roots, args.recursive, formats)
    if not archives:
        print("No archives found.")
        return 0
    if args.integrity_check:
        valid: list[Path] = []
        for a in archives:
            ok, msg = check_integrity(a)
            if not args.quiet:
                print(("✓ " if ok else "✗ ") + msg)
            if ok:
                valid.append(a)
        archives = valid
        if not archives:
            print("No valid archives to extract.")
            return 0
    if not args.quiet:
        print(
            f"Processing {len(archives)} archive(s)  engine={args.engine}  jobs={MAX_WORKERS}  organize={args.organize}"
        )
    opts = dict(
        engine=args.engine,
        keep=args.keep,
        dry_run=args.dry_run,
        organize=args.organize,
        out_dir=Path(args.output_dir) if args.output_dir else None,
        single_file_subdir=args.single_file_subdir,
        subdir_truncate=args.subdir_truncate,
        quiet=args.quiet,
    )
    start = time.time()
    results: list[ExtractResult]
    if MAX_WORKERS == 1 or len(archives) == 1:
        results = [extract_one(a, **opts) for a in archives]
    else:
        jobs = MAX_WORKERS if MAX_WORKERS > 0 else min(len(archives), mp.cpu_count())
        with mp.Pool(processes=jobs) as pool:
            results = pool.map(_worker, [(a, opts) for a in archives])
    elapsed = time.time() - start
    for r in results:
        line = str(r)
        if r.status == "failed":
            print(line, file=sys.stderr)
        else:
            print(line)
    _summary(results, elapsed, quiet=args.quiet)
    return 0 if all(r.status != "failed" for r in results) else 1


def fix_misextracted(root: Path, do_fix: bool, verbose: bool) -> int:
    count = 0
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        if len(entries) != 1:
            continue
        f = entries[0]
        if not f.is_file() or f.name != d.name:
            continue
        count += 1
        tag = "[FIX]" if do_fix else "[DRY]"
        if verbose or not do_fix:
            print(f"{tag} {d}/  →  file {f.name}")
        if not do_fix:
            continue
        try:
            tmp = d.parent / (d.name + ".fixtmp")
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            shutil.move(str(d), str(tmp))
            shutil.move(str(tmp / d.name), str(d.parent / d.name))
            tmp.rmdir()
        except Exception as e:
            print(f"Failed to fix {d}: {e}", file=sys.stderr)
    return count


def cmd_fix(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1
    print(f"Scanning {root}")
    if not args.fix:
        print("DRY RUN — pass --fix to apply changes\n")
    n = fix_misextracted(root, do_fix=args.fix, verbose=args.verbose)
    print(f"\nSummary: {n} issue(s) {'fixed' if args.fix else 'found'}.")
    return 0


def cmd_whl(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    wheels: list[Path] = []
    for r in roots:
        if r.is_file() and r.suffix == ".whl":
            wheels.append(r)
        elif r.is_dir():
            wheels.extend(r.glob("*.whl"))
    if not wheels:
        print("No .whl files found.")
        return 0
    start = time.time()
    ok = 0
    fail = 0
    for w in wheels:
        try:
            dest = w.parent / strip_version(w.name)
            dest.mkdir(exist_ok=True)
            with zipfile.ZipFile(w) as zf:
                zf.extractall(dest)
            if not args.keep:
                w.unlink()
            ok += 1
            print(f"✓ {w.name} → {dest.name}/")
        except Exception as e:
            fail += 1
            print(f"✗ {w.name}: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"\nDone: {ok} extracted, {fail} failed, {time.time() - start:.1f}s")
    return 0 if fail == 0 else 1


def cmd_check(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    archives = find_archives(roots, args.recursive, _parse_formats(args.formats))
    if not archives:
        print("No archives found.")
        return 0
    start = time.time()
    fail = 0
    for a in archives:
        ok, msg = check_integrity(a)
        stream = sys.stdout if ok else sys.stderr
        print(("✓ " if ok else "✗ ") + msg, file=stream)
        if not ok:
            fail += 1
    if not args.quiet:
        print(
            f"\nChecked {len(archives)} archive(s) in {time.time() - start:.1f}s "
            f"— {len(archives) - fail} valid, {fail} invalid."
        )
    return 0 if fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive_tool.py",
        description="Unified archive extractor / fixer / checker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  archive_tool.py extract .\n"
            "  archive_tool.py extract -r --engine python .\n"
            "  archive_tool.py extract -k -n file.zip\n"
            "  archive_tool.py extract --organize stem .\n"
            "  archive_tool.py extract --integrity-check -r .\n"
            "  archive_tool.py fix --fix .\n"
            "  archive_tool.py whl .\n"
            "  archive_tool.py check -r .\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("extract", help="Extract archives.")
    pe.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: cwd).",
    )
    pe.add_argument("-r", "--recursive", action="store_true", help="Recurse into directories.")
    pe.add_argument(
        "-e",
        "--engine",
        choices=("auto", "python", "external"),
        default="auto",
        help="Extraction backend (default: auto = try python, fall back to CLI tools).",
    )
    pe.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the original archive after extraction.",
    )
    pe.add_argument("-n", "--dry-run", action="store_true", help="Only print what would be done.")
    pe.add_argument(
        "--organize",
        choices=("flat", "stem", "versioned"),
        default="flat",
        help="flat = extract next to the archive; "
        "stem = subdir named after archive stem; "
        "versioned = stem with version stripped (whl-style).",
    )
    pe.add_argument(
        "--single-file-subdir",
        action="store_true",
        help="ar_extract.py behaviour: only create a subdir when the "
        "archive's contents share a single top-level directory.",
    )
    pe.add_argument(
        "--subdir-truncate",
        type=int,
        default=0,
        metavar="N",
        help="Truncate subdir names to first N characters (subdir.py uses 8).",
    )
    pe.add_argument(
        "--formats",
        type=str,
        default=None,
        help="Comma-separated extensions to process (e.g. .tar.gz,.zip).",
    )
    pe.add_argument(
        "--integrity-check",
        action="store_true",
        help="Validate each archive before extracting.",
    )
    pe.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Force a single output directory (overrides --organize).",
    )
    pe.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress and summary output.",
    )
    pe.set_defaults(func=cmd_extract)
    pf = sub.add_parser("fix", help="Fix mis-extracted dir/file name collisions.")
    pf.add_argument("root", nargs="?", default=".", help="Root directory to scan (default: cwd).")
    pf.add_argument(
        "--fix",
        action="store_true",
        help="Actually apply fixes (default is a dry-run).",
    )
    pf.add_argument("-v", "--verbose", action="store_true", help="Print every candidate.")
    pf.set_defaults(func=cmd_fix)
    pw = sub.add_parser("whl", help="Extract .whl wheels into version-stripped dirs.")
    pw.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Directories or .whl files (default: cwd).",
    )
    pw.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the .whl files after extraction.",
    )
    pw.add_argument("-q", "--quiet", action="store_true", help="Suppress summary output.")
    pw.set_defaults(func=cmd_whl)
    pc = sub.add_parser("check", help="Integrity-check archives without extracting.")
    pc.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to scan (default: cwd).",
    )
    pc.add_argument("-r", "--recursive", action="store_true", help="Recurse into directories.")
    pc.add_argument("--formats", type=str, default=None, help="Comma-separated extensions to check.")
    pc.add_argument("-q", "--quiet", action="store_true", help="Suppress summary output.")
    pc.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
