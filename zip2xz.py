#!/data/data/com.termux/files/usr/bin/env python
"""Convert .zip archives to .tar.xz, preserving metadata and verifying the result."""

import calendar
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import lzma_mt


def convert(zip_path: Path) -> Path:
    tarxz_path = zip_path.with_suffix(".tar.xz")
    fd, tmp_name = tempfile.mkstemp(dir=tarxz_path.parent, suffix=".tar.xz.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with (
            zipfile.ZipFile(zip_path) as zf,
            lzma_mt.open(
                tmp_path,
                "wb",
                format=lzma_mt.FORMAT_XZ,
                preset=9 | lzma_mt.PRESET_EXTREME,
                threads=4,
            ) as xz,
            tarfile.open(fileobj=xz, mode="w|") as tf,
        ):
            for info in zf.infolist():
                ti = tarfile.TarInfo(name=info.filename)
                ti.mtime = calendar.timegm(info.date_time + (0, 0, 0))
                mode = (info.external_attr >> 16) & 0o7777
                if info.is_dir():
                    ti.type = tarfile.DIRTYPE
                    ti.mode = mode or 0o755
                    tf.addfile(ti)
                else:
                    ti.size = info.file_size
                    ti.mode = mode or 0o644
                    with zf.open(info) as src:
                        tf.addfile(ti, src)

        with zipfile.ZipFile(zip_path) as zf:
            expected = set(zf.namelist())
        with tarfile.open(tmp_path, mode="r:xz") as tf:
            actual = {m.name for m in tf.getmembers()}
        missing = expected - actual - {n.rstrip("/") for n in expected}
        if missing:
            msg = f"verification failed, missing members: {sorted(missing)[:5]}"
            raise RuntimeError(msg)

        tmp_path.replace(tarxz_path)
        return tarxz_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    zips = sorted(p for p in Path().rglob("*.zip") if p.is_file())
    if not zips:
        print("No .zip files found.")
        return 0
    failures = 0
    for zp in zips:
        print(f"Converting: {zp}")
        try:
            target = convert(zp)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            failures += 1
            continue
        try:
            zp.unlink()
            print(f"  OK -> {target} (removed {zp})")
        except OSError as e:
            print(f"  WARNING: could not remove {zp}: {e}", file=sys.stderr)
    if failures:
        print(f"\n{failures} conversion(s) failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
