#!/data/data/com.termux/files/usr/bin/env python
"""Delete the ``Requires-Dist`` lines from package metadata and archives.
Processes ``METADATA``/``PKG-INFO``/``PKGINFO`` files and the same files inside wheels, zips and tarballs (including brotli/zstd/lz4/lzma compressed archives), removing the dependency declarations and logging every removed line to ``/sdcard/reqz.txt``.
Usage: noreq Examples: $ noreq Destructive: it strips dependency metadata from the packages in the current tree."""

from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile
from typing import Any


def is_wheel_ok(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as wheel:
            corrupt_file = wheel.testzip()
            return not corrupt_file
    except zipfile.BadZipFile:
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return False


def is_valid_archive(path: str | Path) -> bool:
    path = Path(path)
    try:
        if not path.is_file():
            return False
        if not path.stat().st_size:
            return False
        lower = path.name.lower()
        if lower.endswith((".zip", ".whl")):
            return is_wheel_ok(path)
        if lower.endswith(".tar"):
            return _check_tar(path, mode="r:")
        if lower.endswith((".tar.gz", ".tgz")):
            return _check_tar(path, mode="r:gz")
        if lower.endswith((".tar.xz", ".txz")):
            return _check_tar(path, mode="r:xz")
        if lower.endswith((".tar.bz2", ".tbz2", ".tbz")):
            return _check_tar(path, mode="r:bz2")
        if lower.endswith(".tar.br"):
            return _check_tar_with_brotli(path)
        if lower.endswith(".tar.zst"):
            return _check_tar_with_zstd(path)
        if lower.endswith(".tar.lz4"):
            return _check_tar_with_lz4(path)
        if lower.endswith(".tar.lzma"):
            return _check_tar_with_lzma(path)
        if lower.endswith(".br"):
            return _check_brotli_file(path)
        if lower.endswith(".zst"):
            return _check_zstd_file(path)
        if lower.endswith(".lz4"):
            return _check_lz4_file(path)
        if lower.endswith(".lzma"):
            return _check_lzma_file(path)
        return False
    except Exception:
        return False


def _check_tar(path: str | Path, mode: str) -> bool:
    path = Path(path)
    with tarfile.open(path, mode) as tf:
        bad = tf.badfile()
        if bad is not None:
            return False
        tf.getmembers()
        return True


def _check_brotli_file(path: str | Path) -> bool:
    from brotlicffi import decompress as brotli_decompress

    path = Path(path)
    try:
        data = Path(path).read_bytes()
        _ = brotli_decompress(data)
        return True
    except:
        return False


def _check_zstd_file(path: str | Path) -> bool:
    from zstandard import ZstdDecompressor as zstd_ZstdDecompressor

    path = Path(path)
    try:
        dctx = zstd_ZstdDecompressor()
        with open(path, "rb") as f:
            _ = dctx.decompress(f.read())
        return True
    except:
        return False


def _check_lz4_file(path: str | Path) -> bool:
    from lz4.frame import decompress as lz4_decompress

    path = Path(path)
    try:
        with open(path, "rb") as f:
            _ = lz4_decompress(f.read())
        return True
    except:
        return False


def _check_lzma_file(path: str | Path) -> bool:
    from lzma import decompress as lzma_decompress

    path = Path(path)
    try:
        with open(path, "rb") as f:
            _ = lzma_decompress(f.read())
        return True
    except:
        return False


def _check_tar_with_brotli(path: str | Path) -> bool:
    from brotlicffi import decompress as brotli_decompress

    path = Path(path)
    try:
        with open(path, "rb") as f:
            raw = brotli_decompress(f.read())
        return _check_tar_bytes(raw)
    except:
        return False


def _check_tar_with_zstd(path: str | Path) -> bool:
    path = Path(path).resolve()
    xpath = path.with_name(path.name.replace(".tar.zst", ""))
    try:
        with tarfile.open(path, "r:zst") as f:
            f.extractall(path=xpath, filter="data")
        return True
    except:
        return False


def _check_tar_with_lz4(path: str | Path) -> bool:
    from lz4.frame import decompress as lz4_decompress

    path = Path(path)
    try:
        with open(path, "rb") as f:
            raw = lz4_decompress(f.read())
        return _check_tar_bytes(raw)
    except:
        return False


def _check_tar_with_lzma(path: str | Path) -> bool:
    from lzma import decompress as lzma_decompress

    path = Path(path)
    try:
        with open(path, "rb") as f:
            raw = lzma_decompress(f.read())
        return _check_tar_bytes(raw)
    except:
        return False


def _check_tar_bytes(raw: bytes) -> bool:
    from io import BytesIO as io_BytesIO

    try:
        with tarfile.open(fileobj=io_BytesIO(raw), mode="r:") as tf:
            tf.getmembers()
        return True
    except:
        return False


TARGET_FILES: set[str] = {"METADATA", "PKGINFO", "PKG-INFO"}
PREFIX: str = "Requires-Dist:"
LOG_FILE: Path = Path("/sdcard/reqz.txt")
removed_lines_accumulator: list[Any] = []


def clean_text(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    cleaned = []
    removed = []
    for line in lines:
        if line.startswith(PREFIX):
            removed.append(line)
        else:
            cleaned.append(line)
    final_text = "\n".join(cleaned)
    if text.endswith("\n"):
        final_text += "\n"
    return (final_text, removed)


def clean_file(path: Path) -> None:
    try:
        original = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return
    cleaned, removed = clean_text(original)
    if removed:
        removed_lines_accumulator.extend(removed)
        path.write_text(cleaned, encoding="utf-8")


def process_zip(path: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp_path, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                base = Path(item.filename).name
                if base in TARGET_FILES:
                    try:
                        text = data.decode("utf-8", errors="ignore")
                        cleaned, removed = clean_text(text)
                        if removed:
                            removed_lines_accumulator.extend(removed)
                        data = cleaned.encode("utf-8")
                    except Exception:
                        pass
                zout.writestr(item, data)
        shutil.move(str(tmp_path), str(path))
    finally:
        tmp_path.unlink(missing_ok=True)


def process_tar(path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_dir = Path(tmp_dir)
        tmp_tar = temp_dir / "temp.tar.gz"
        with tarfile.open(path, "r:*") as tar:
            tar.extractall(temp_dir, filter="data")
        for target_file in TARGET_FILES:
            for path in temp_dir.rglob(target_file):
                if path.is_file():
                    clean_file(path)
        with tarfile.open(tmp_tar, "w:gz") as tar:
            tar.add(temp_dir, arcname="")
        shutil.move(str(tmp_tar), str(path))


def dispatch_archive(path: Path) -> None:
    if not is_valid_archive(str(path)):
        print(f"{path} is not valid archive")
        return
    path_str = str(path).lower()
    if path_str.endswith(".whl"):
        print(f"processing ... {path}")
        process_zip(path)
    elif path_str.endswith((".tar.gz", ".tgz", ".tar")):
        process_tar(path)


def find_files_to_process() -> list[Path]:
    files_to_process = []
    current_dir = Path.cwd()
    for path in current_dir.rglob("*"):
        if not path.is_file():
            continue
        file_name = path.name
        file_name_lower = file_name.lower()
        if (
            file_name in TARGET_FILES
            or file_name.endswith(".metadata")
            or file_name_lower.endswith((".zip", ".whl", ".tar.gz", ".tgz", ".tar"))
        ):
            files_to_process.append(path)
    return files_to_process


def main() -> None:
    files_to_process = find_files_to_process()
    for path in files_to_process:
        file_name = path.name
        file_name_lower = file_name.lower()
        if file_name in TARGET_FILES or file_name.endswith(".metadata"):
            clean_file(path)
        elif file_name_lower.endswith((".zip", ".whl", ".tar.gz", ".tgz", ".tar")):
            dispatch_archive(path)
    if removed_lines_accumulator:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.writelines(line + "\n" for line in removed_lines_accumulator)
            print(f"--- Saved {len(removed_lines_accumulator)} lines to {LOG_FILE} ---")
        except PermissionError:
            pass
        print("\nRemoved Lines:")
        print("-" * 40)
        for line in removed_lines_accumulator:
            print(line)
        print("-" * 40)
    else:
        print("No matching lines were found or removed.")


if __name__ == "__main__":
    raise SystemExit(main())
