#!/data/data/com.termux/files/usr/bin/env python
"""Detect and extract an archive or compressed file whatever its format.
Sniffs the magic bytes, ranks the candidate decoders and tries them in order: libarchive, the stream decompressors (gzip, bz2, lzma, brotli, zstd, lzma, cramjam), tarfile, zipfile, py7zr, rarfile, acefile, cabarchive and pycdlib, with a pickle fallback.
Members are written under a safe path.
Usage: trydecomp FILE [OUTDIR] Examples: $ trydecomp mystery.bin out/ Runs as a script (``python trydecomp.py``) in the directory it should act on; the module can also be imported."""

import bz2
import contextlib
from dataclasses import dataclass
import gzip
import lzma
from pathlib import Path
import pickle
import sys
import tarfile
from typing import Callable
import zipfile
import zlib


try:
    import brotli as _brotli_mod
except ImportError:
    _brotli_mod = None
try:
    import zstandard as _zstd_mod
except ImportError:
    _zstd_mod = None
try:
    import py7zr as _py7zr_mod
except ImportError:
    _py7zr_mod = None
try:
    import cramjam as _cramjam_mod
except ImportError:
    _cramjam_mod = None
try:
    import pylzma as _pylzma_mod
except ImportError:
    _pylzma_mod = None
try:
    import rarfile as _rarfile_mod
except ImportError:
    _rarfile_mod = None
try:
    import libarchive as _libarchive_mod
except ImportError:
    _libarchive_mod = None
try:
    import pycdlib as _pycdlib_mod
except ImportError:
    _pycdlib_mod = None
try:
    import cabarchive as _cabarchive_mod
except ImportError:
    _cabarchive_mod = None
try:
    import acefile as _acefile_mod
except ImportError:
    _acefile_mod = None
try:
    import pyppmd as _pyppmd_mod
except ImportError:
    _pyppmd_mod = None
try:
    import bz3 as _bz3_mod
except ImportError:
    _bz3_mod = None
try:
    import lzo as _lzo_mod
except ImportError:
    _lzo_mod = None


@dataclass
class Detection:
    name: str
    kind: str
    mime: str = "application/octet-stream"
    confidence: str = "medium"


_MAGIC_TABLE: list[tuple[bytes, int, Detection]] = [
    (b"PK\x03\x04", 0, Detection("zip", "archive", "application/zip", "high")),
    (b"PK\x05\x06", 0, Detection("zip (empty)", "archive", "application/zip", "high")),
    (b"PK\x07\x08", 0, Detection("zip (split)", "archive", "application/zip", "high")),
    (
        b"7z\xbc\xaf\x27\x1c",
        0,
        Detection("7z", "archive", "application/x-7z-compressed", "high"),
    ),
    (
        b"Rar!\x1a\x07\x00",
        0,
        Detection("rar4", "archive", "application/vnd.rar", "high"),
    ),
    (
        b"Rar!\x1a\x07\x01\x00",
        0,
        Detection("rar5", "archive", "application/vnd.rar", "high"),
    ),
    (
        b"MSCF",
        0,
        Detection("cab", "archive", "application/vnd.ms-cab-compressed", "high"),
    ),
    (
        b"**ACE**",
        0,
        Detection("ace", "archive", "application/x-ace-compressed", "high"),
    ),
    (b"!<arch>\n", 0, Detection("ar/deb", "archive", "application/x-archive", "high")),
    (b"\xed\xab\xee\xdb", 0, Detection("rpm", "archive", "application/x-rpm", "high")),
    (b"070701", 0, Detection("cpio (newc)", "archive", "application/x-cpio", "high")),
    (b"070702", 0, Detection("cpio (crc)", "archive", "application/x-cpio", "high")),
    (b"070707", 0, Detection("cpio (odc)", "archive", "application/x-cpio", "high")),
    (b"xar!", 0, Detection("xar", "archive", "application/x-xar", "high")),
    (b"ustar", 257, Detection("tar", "archive", "application/x-tar", "high")),
    (
        b"CD001",
        0x8001,
        Detection("iso9660", "archive", "application/x-iso9660-image", "high"),
    ),
    (b"\x1f\x8b", 0, Detection("gzip", "stream", "application/gzip", "high")),
    (
        b"\x1f\x9d",
        0,
        Detection("compress (.Z)", "stream", "application/x-compress", "high"),
    ),
    (b"BZh", 0, Detection("bzip2", "stream", "application/x-bzip2", "high")),
    (b"\xfd7zXZ\x00", 0, Detection("xz", "stream", "application/x-xz", "high")),
    (b"\x28\xb5\x2f\xfd", 0, Detection("zstd", "stream", "application/zstd", "high")),
    (
        b"\x04\x22\x4d\x18",
        0,
        Detection("lz4 frame", "stream", "application/x-lz4", "high"),
    ),
    (
        b"\x02\x21\x4c\x18",
        0,
        Detection("lz4 legacy", "stream", "application/x-lz4", "medium"),
    ),
    (b"BZ3v1", 0, Detection("bzip3", "stream", "application/x-bzip3", "high")),
    (
        b"\xff\x06\x00\x00sNaPpY",
        0,
        Detection("snappy (framed)", "stream", "application/x-snappy-framed", "high"),
    ),
    (
        b"\x80\x04",
        0,
        Detection("pickle proto 4", "serialized", "application/x-python-pickle", "low"),
    ),
    (
        b"\x80\x05",
        0,
        Detection("pickle proto 5", "serialized", "application/x-python-pickle", "low"),
    ),
]


def sniff_magic(data: bytes) -> Detection:
    best: Detection | None = None
    for sig, off, det in _MAGIC_TABLE:
        if len(data) < off + len(sig):
            continue
        if data[off : off + len(sig)] != sig:
            continue
        if best is None or (det.confidence == "high" and best.confidence != "high"):
            best = det
            if det.confidence == "high":
                break
    return best or Detection("unknown", "unknown", "application/octet-stream", "low")


_RECOGNIZED: list[tuple[str, Callable[[Path], None]]] = []


def _register(name: str, extractor: Callable[[Path], None]) -> None:
    _RECOGNIZED.append((name, extractor))


def _safe_member_path(base: Path, member: str) -> Path | None:
    try:
        p = (base / member).resolve()
        p.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return p


def try_libarchive(filename: str) -> bool:
    libarchive = _libarchive_mod
    if libarchive is None:
        print("  SKIP: libarchive-c not installed.\n")
        return False
    print("Trying libarchive (catch-all)...")
    try:
        with libarchive.file_reader(filename) as arc:
            entries = list(arc)
        if not entries:
            print("  FAILED: libarchive yielded no entries.\n")
            return False
        first = entries[0].pathname
        fmt = getattr(entries[0], "archive_format", None) or "?"
        print(f"  SUCCESS: libarchive opened {len(entries)} entries [format={fmt}]. First: {first}\n")
        _register("libarchive", extract_libarchive)
        return True
    except Exception as exc:
        print(f"  FAILED: libarchive raised {type(exc).__name__}: {exc}\n")
        return False


def _build_decompressors() -> dict[str, Callable[[bytes], bytes]]:
    methods: dict[str, Callable[[bytes], bytes]] = {
        "zlib": zlib.decompress,
        "raw-deflate": lambda d: zlib.decompress(d, -15),
        "bz2": bz2.decompress,
        "gzip": gzip.decompress,
        "lzma": lzma.decompress,
    }
    brotli = _brotli_mod
    if brotli is not None:
        methods["brotli"] = brotli.decompress
    zstd = _zstd_mod
    if zstd is not None:

        def _zstd_dec(data: bytes) -> bytes:
            return zstd.ZstdDecompressor().decompress(data)

        methods["zstandard"] = _zstd_dec
    cramjam = _cramjam_mod
    if cramjam is not None:
        snappy = cramjam.snappy
        lz4 = cramjam.lz4
        cj_zstd = cramjam.zstd
        cj_brotli = cramjam.brotli
        cj_bzip2 = cramjam.bzip2
        methods["snappy (framed)"] = lambda d: bytes(snappy.decompress(d))
        methods["snappy (raw)"] = lambda d: bytes(snappy.decompress_raw(d))
        methods["lz4 (frame)"] = lambda d: bytes(lz4.decompress(d))
        methods["lz4 (block)"] = lambda d: bytes(lz4.decompress_block(d))
        methods["cramjam-zstd"] = lambda d: bytes(cj_zstd.decompress(d))
        methods["cramjam-brotli"] = lambda d: bytes(cj_brotli.decompress(d))
        methods["cramjam-bzip2"] = lambda d: bytes(cj_bzip2.decompress(d))
    pylzma = _pylzma_mod
    if pylzma is not None:

        def _pylzma_dec(data: bytes) -> bytes:
            out = pylzma.decompress(data)
            return out if isinstance(out, (bytes, bytearray)) else bytes(out)

        methods["pylzma (raw lzma/lzma2)"] = _pylzma_dec
    pyppmd = _pyppmd_mod
    if pyppmd is not None:

        def _ppmd_dec(data: bytes) -> bytes:
            for order, mem in ((6, 16 << 20), (8, 32 << 20), (16, 64 << 20)):
                try:
                    return pyppmd.Ppmd7Decoder(order, mem).decode(data, 64 << 20)
                except Exception:
                    continue
            msg = "no PPMd7 variant matched"
            raise ValueError(msg)

        methods["ppmd7 (raw)"] = _ppmd_dec
    bz3 = _bz3_mod
    if bz3 is not None:
        methods["bzip3"] = bz3.decompress
    lzo = _lzo_mod
    if lzo is not None:
        methods["lzo"] = lambda d: lzo.decompress(d, False, len(d) * 20)
    return methods


def _hint_boost_order(
    methods: dict[str, Callable[[bytes], bytes]],
    hint: Detection,
) -> dict[str, Callable[[bytes], bytes]]:
    if hint.kind == "unknown":
        return methods
    aliases = {
        "gzip": "gzip",
        "bzip2": "bz2",
        "xz": "lzma",
        "zstd": "zstandard",
        "lz4 frame": "lz4",
        "lz4 legacy": "lz4",
        "snappy (framed)": "snappy",
        "bzip3": "bzip3",
        "compress (.Z)": None,
        "pickle proto 4": None,
        "pickle proto 5": None,
    }
    key = aliases.get(hint.name)
    if not key:
        return methods
    preferred = {k: v for k, v in methods.items() if key in k.lower()}
    rest = {k: v for k, v in methods.items() if k not in preferred}
    return {**preferred, **rest}


def try_stream_decompressors(data: bytes, hint: Detection) -> bool:
    methods = _hint_boost_order(_build_decompressors(), hint)
    success = False
    for name, func in methods.items():
        try:
            print(f"Trying {name}...")
            out = func(data)
            if out and len(out) < len(data) * 10:
                print(f"  SUCCESS: {name} decoded {len(out)} bytes.\n")
                _register(
                    name,
                    (lambda p, blob=out, n=name: extract_stream(n, blob, p.name)),
                )
                success = True
            else:
                print(f"  FAILED: {name} produced {len(out or b'')} bytes (suspicious ratio).\n")
        except Exception as exc:
            print(f"  FAILED: {name}: {type(exc).__name__}: {exc}\n")
    return success


def try_tarfile(filename: str) -> bool:
    if not tarfile.is_tarfile(filename):
        return False
    try:
        print("Trying tarfile (stdlib)...")
        with tarfile.open(filename, "r") as tar:
            members = tar.getmembers()
        if not members:
            print("  FAILED: tar is empty.\n")
            return False
        print(f"  SUCCESS: tar with {len(members)} members. First: {members[0].name}\n")
        _register("tar", extract_tar)
        return True
    except Exception as exc:
        print(f"  FAILED: tarfile: {type(exc).__name__}: {exc}\n")
        return False


def try_zipfile(filename: str) -> bool:
    if not zipfile.is_zipfile(filename):
        return False
    try:
        print("Trying zipfile (stdlib)...")
        with zipfile.ZipFile(filename, "r") as zf:
            names = zf.namelist()
        if not names:
            print("  FAILED: zip is empty.\n")
            return False
        print(f"  SUCCESS: zip with {len(names)} files. First: {names[0]}\n")
        _register("zip", extract_zip)
        return True
    except Exception as exc:
        print(f"  FAILED: zipfile: {type(exc).__name__}: {exc}\n")
        return False


def try_py7zr(filename: str) -> bool:
    py7zr = _py7zr_mod
    if py7zr is None:
        return False
    try:
        print("Trying py7zr (7z)...")
        with py7zr.SevenZipFile(filename, mode="r") as z:
            names = z.getnames()
        if not names:
            print("  FAILED: 7z archive is empty.\n")
            return False
        print(f"  SUCCESS: 7z with {len(names)} files. First: {names[0]}\n")
        _register("7z", extract_7z)
        return True
    except Exception as exc:
        print(f"  FAILED: py7zr: {type(exc).__name__}: {exc}\n")
        return False


def try_rarfile(filename: str) -> bool:
    rarfile = _rarfile_mod
    if rarfile is None:
        return False
    if not rarfile.is_rarfile(filename):
        return False
    try:
        print("Trying rarfile...")
        with rarfile.RarFile(filename) as rf:
            names = rf.namelist()
        if not names:
            print("  FAILED: rar is empty.\n")
            return False
        print(f"  SUCCESS: rar with {len(names)} files. First: {names[0]}\n")
        _register("rar", extract_rar)
        return True
    except Exception as exc:
        print(f"  FAILED: rarfile: {type(exc).__name__}: {exc}\n")
        return False


def try_acefile(filename: str) -> bool:
    acefile = _acefile_mod
    if acefile is None:
        return False
    try:
        print("Trying acefile...")
        with acefile.open(filename) as af:
            names = [m.filename for m in af.members()]
        if not names:
            print("  FAILED: ace is empty.\n")
            return False
        print(f"  SUCCESS: ace with {len(names)} files. First: {names[0]}\n")
        _register("ace", extract_ace)
        return True
    except Exception as exc:
        print(f"  FAILED: acefile: {type(exc).__name__}: {exc}\n")
        return False


def try_cabarchive(filename: str) -> bool:
    cabarchive = _cabarchive_mod
    if cabarchive is None:
        return False
    try:
        print("Trying cabarchive...")
        with open(filename, "rb") as fp:
            cf = cabarchive.CabArchive(fp)
            names = [f.filename for f in cf.files]
        if not names:
            print("  FAILED: cab is empty.\n")
            return False
        print(f"  SUCCESS: cab with {len(names)} files. First: {names[0]}\n")
        _register("cab", extract_cab)
        return True
    except Exception as exc:
        print(f"  FAILED: cabarchive: {type(exc).__name__}: {exc}\n")
        return False


def try_pycdlib(filename: str) -> bool:
    pycdlib = _pycdlib_mod
    if pycdlib is None:
        return False
    iso = pycdlib.PyCdlib()
    try:
        print("Trying pycdlib (ISO/UDF)...")
        iso.open(filename)
        count, first = 0, None
        for child in iso.list_children(iso_path="/"):
            if child is None:
                continue
            name = child.file_identifier().decode(errors="replace")
            if name in (".", ".."):
                continue
            if first is None:
                first = name
            count += 1
        if not count:
            print("  FAILED: ISO has no root entries.\n")
            return False
        print(f"  SUCCESS: ISO/UDF with {count} root entries. First: {first}\n")
        _register("iso", extract_iso)
        return True
    except Exception as exc:
        print(f"  FAILED: pycdlib: {type(exc).__name__}: {exc}\n")
        return False
    finally:
        with contextlib.suppress(Exception):
            iso.close()


def try_pickle(data: bytes) -> bool:
    if not (data.startswith((b"\x80\x04", b"\x80\x05", b"c", b"("))):
        return False
    try:
        print("Trying pickle (WARNING: unsafe on untrusted input)...")
        obj = pickle.loads(data)
        print(f"  SUCCESS: pickle decoded → {type(obj).__name__}\n")
        _register("pickle", extract_pickle)
        return True
    except Exception as exc:
        print(f"  FAILED: pickle: {type(exc).__name__}: {exc}\n")
        return False


def extract_tar(src: Path) -> None:
    with tarfile.open(src, "r") as tar:
        for m in tar.getmembers():
            if _safe_member_path(Path.cwd(), m.name) is None:
                print(f"    SKIP (unsafe path): {m.name}")
                continue
            tar.extract(m, path=Path.cwd())


def extract_zip(src: Path) -> None:
    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            if _safe_member_path(Path.cwd(), info.filename) is None:
                print(f"    SKIP (unsafe path): {info.filename}")
                continue
            zf.extract(info, path=Path.cwd())


def extract_7z(src: Path) -> None:
    py7zr = _py7zr_mod
    if py7zr is None:
        return
    with py7zr.SevenZipFile(src, mode="r") as z:
        unsafe = [n for n in z.getnames() if _safe_member_path(Path.cwd(), n) is None]
        if unsafe:
            print(f"    SKIP unsafe members: {unsafe[:3]}{' …' if len(unsafe) > 3 else ''}")
        z.extractall(path=Path.cwd())


def extract_rar(src: Path) -> None:
    rarfile = _rarfile_mod
    if rarfile is None:
        return
    with rarfile.RarFile(src) as rf:
        for info in rf.infolist():
            if _safe_member_path(Path.cwd(), info.filename) is None:
                print(f"    SKIP (unsafe path): {info.filename}")
                continue
            rf.extract(info, path=Path.cwd())


def extract_ace(src: Path) -> None:
    acefile = _acefile_mod
    if acefile is None:
        return
    with acefile.open(src) as af:
        for member in af.members():
            target = _safe_member_path(Path.cwd(), member.filename)
            if target is None:
                print(f"    SKIP (unsafe path): {member.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                af.extract(member, out)


def extract_cab(src: Path) -> None:
    cabarchive = _cabarchive_mod
    if cabarchive is None:
        return
    with open(src, "rb") as fp:
        cf = cabarchive.CabArchive(fp)
        for f in cf.files:
            target = _safe_member_path(Path.cwd(), f.filename)
            if target is None:
                print(f"    SKIP (unsafe path): {f.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read())


def extract_iso(src: Path) -> None:
    pycdlib = _pycdlib_mod
    if pycdlib is None:
        return
    iso = pycdlib.PyCdlib()
    iso.open(str(src))
    try:

        def _walk(path: str) -> None:
            for child in iso.list_children(iso_path=path):
                if child is None:
                    continue
                name = child.file_identifier().decode(errors="replace")
                if name in (".", ".."):
                    continue
                clean = name.split(";", 1)[0]
                child_path = f"{path.rstrip('/')}/{name}"
                target = _safe_member_path(Path.cwd(), clean)
                if target is None:
                    print(f"    SKIP (unsafe path): {clean}")
                    continue
                if child.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    _walk(child_path + "/")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as out:
                        iso.get_file_from_iso_fp(out, iso_path=child_path)

        _walk("/")
    finally:
        iso.close()


def extract_libarchive(src: Path) -> None:
    libarchive = _libarchive_mod
    if libarchive is None:
        return
    with libarchive.file_reader(str(src)) as arc:
        for entry in arc:
            target = _safe_member_path(Path.cwd(), entry.pathname)
            if target is None:
                print(f"    SKIP (unsafe path): {entry.pathname}")
                continue
            if entry.isdir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                out.writelines(entry.get_blocks())


def extract_stream(name: str, blob: bytes, src_name: str) -> None:
    suffixes = {
        "gzip": ".gz",
        "bz2": ".bz2",
        "lzma": ".xz",
        "brotli": ".br",
        "zstandard": ".zst",
        "snappy (framed)": ".sz",
        "snappy (raw)": ".sz",
        "lz4 (frame)": ".lz4",
        "lz4 (block)": ".lz4",
        "bzip3": ".bz3",
        "lzo": ".lzo",
        "raw-deflate": ".zz",
        "zlib": ".zz",
    }
    ext = suffixes.get(name)
    if ext and src_name.endswith(ext):
        stem = src_name[: -len(ext)]
    else:
        stem = src_name + ".out"
    target = Path.cwd() / stem
    target.write_bytes(blob)
    print(f"    WROTE: {target.name} ({len(blob)} bytes)")


def extract_pickle(src: Path) -> None:
    obj = pickle.loads(src.read_bytes())
    out = Path.cwd() / (src.name + ".repr.txt")
    out.write_text(repr(obj), encoding="utf-8")
    print(f"    WROTE: {out.name} (repr of {type(obj).__name__})")


def run_extraction(src: Path) -> None:
    if not _RECOGNIZED:
        print("Nothing recognized — no extraction performed.\n")
        return
    print(f"═══ Extracting to: {Path.cwd()} ═══\n")
    for name, extractor in _RECOGNIZED:
        print(f"→ {name}")
        try:
            extractor(src)
            print(f"  DONE: {name}\n")
        except Exception as exc:
            print(f"  ERROR: {name}: {type(exc).__name__}: {exc}\n")
    _RECOGNIZED.clear()


def process(filename: str) -> bool:
    print(f"═══ Analyzing: {filename} ═══\n")
    _RECOGNIZED.clear()
    try:
        data = Path(filename).read_bytes()
    except FileNotFoundError:
        print(f"Error: file not found: {filename}\n")
        return False
    except Exception as exc:
        print(f"Error reading {filename}: {exc}\n")
        return False
    print(f"File size: {len(data)} bytes\n")
    hint = sniff_magic(data)
    print(f"[STAGE 1] Magic sniff → {hint.name} (kind={hint.kind}, mime={hint.mime}, confidence={hint.confidence})\n")
    success = False
    print("[STAGE 2] libarchive catch-all")
    if hint.kind == "stream":
        print(f"  SKIP: magic says {hint.name} is a raw stream, not a container.\n")
    elif hint.kind == "serialized":
        print("  SKIP: magic says this is a serialized object.\n")
    else:
        if try_libarchive(filename):
            success = True
        success |= try_tarfile(filename)
        success |= try_zipfile(filename)
        success |= try_py7zr(filename)
        success |= try_rarfile(filename)
        success |= try_acefile(filename)
        success |= try_cabarchive(filename)
        success |= try_pycdlib(filename)
    print("[STAGE 3] Byte-stream decompressors")
    if try_stream_decompressors(data, hint):
        success = True
    if hint.kind == "serialized":
        success |= try_pickle(data)
    if success:
        print("✔ At least one format was successfully recognized.\n")
        run_extraction(Path(filename))
    else:
        print("✘ No compression or archive format was recognized.\n")
    return success


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {Path(argv[0]).name} <filename>\n")
        return 1
    return 0 if process(argv[1]) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
