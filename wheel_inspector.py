#!/data/data/com.termux/files/usr/bin/env python
"""Inspect and validate .whl files."""


from pathlib import Path
import zipfile

from loguru import logger


class WheelInspector:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[INSPECT] {message}")

    def inspect_wheel(self, wheel_path: Path) -> dict:
        if not wheel_path.exists():
            return {"error": f"File not found: {wheel_path}"}
        try:
            with zipfile.ZipFile(wheel_path, "r") as zf:
                info = {
                    "filename": wheel_path.name,
                    "size_mb": wheel_path.stat().st_size / 1024.0,
                    "file_count": len(zf.namelist()),
                    "files": zf.namelist(),
                    "metadata": {},
                    "file_types": {},
                }
                metadata_files = [f for f in zf.namelist() if f.endswith("/METADATA")]
                if metadata_files:
                    metadata_content = zf.read(metadata_files[0]).decode("utf-8")
                    for line in metadata_content.split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            info["metadata"][key.strip()] = value.strip()
                wheel_files = [f for f in zf.namelist() if f.endswith("/WHEEL")]
                if wheel_files:
                    wheel_content = zf.read(wheel_files[0]).decode("utf-8")
                    info["wheel_metadata"] = wheel_content
                info["file_types"] = {
                    ".py": len([f for f in zf.namelist() if f.endswith(".py")]),
                    ".so": len([f for f in zf.namelist() if f.endswith(".so")]),
                    ".pyd": len([f for f in zf.namelist() if f.endswith(".pyd")]),
                    ".c": len([f for f in zf.namelist() if f.endswith(".c")]),
                }
                return info
        except Exception as e:
            return {"error": str(e)}

    def validate_wheel(self, wheel_path: Path) -> tuple[bool, list[str]]:
        issues = []
        try:
            with zipfile.ZipFile(wheel_path, "r") as zf:
                files = zf.namelist()
                has_metadata = any(f.endswith("/METADATA") for f in files)
                if not has_metadata:
                    issues.append("Missing METADATA file")
                has_wheel = any(f.endswith("/WHEEL") for f in files)
                if not has_wheel:
                    issues.append("Missing WHEEL file")
                has_record = any(f.endswith("/RECORD") for f in files)
                if not has_record:
                    issues.append("Missing RECORD file")
                dist_info = [f for f in files if ".dist-info/" in f]
                if not dist_info:
                    issues.append("No dist-info directory found")
        except Exception as e:
            issues.append(f"Error reading wheel: {e!s}")
        return len(issues) == 0, issues

    def inspect_directory(self, directory: Path) -> list[dict]:
        wheels = list(directory.glob("*.whl"))
        results = []
        for wheel in wheels:
            info = self.inspect_wheel(wheel)
            print(info)
            is_valid, issues = self.validate_wheel(wheel)
            info["is_valid"] = is_valid
            info["issues"] = issues
            results.append(info)
        return results

    def print_inspection(self, wheel_path: Path) -> None:
        info = self.inspect_wheel(wheel_path)
        if "error" in info:
            print(f"Error: {info['error']}")
            return
        print(f"\n{'=' * 40}")
        print(f"Wheel: {info['filename']}")
        print(f"{'=' * 40}")
        print("\nBasic Info:")
        print(f"  Size: {info['size_mb']:.2f} KB")
        print(f"  Files: {info['file_count']}")
        if info["file_types"]:
            print("\nFile Types:")
            for ext, count in info["file_types"].items():
                if ext == ".py" and (not count or count == 1):
                    logger.debug(f"pkg:{wheel_path}\ncount : {count}")
                    outd = Path("/sdcard/test")
                    wn = wheel_path.name
                    outp = outd / wn
                    wheel_path.rename(outp)
                    continue
                if count > 0:
                    print(f"  {ext}: {count}")
        if info["metadata"]:
            print("\nMetadata:")
            for key, value in info["metadata"].items():
                if key in {"Name", "Version", "Summary", "Author"}:
                    print(f"  {key}: {value}")
        is_valid, issues = self.validate_wheel(wheel_path)
        print(f"\nValidation: {'✓ VALID' if is_valid else '✗ INVALID'}")
        if issues:
            print("Issues:")
            for issue in issues:
                print(f"  - {issue}")
        print(f"{'=' * 40}\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect and validate .whl files")
    parser.add_argument("wheel", nargs="?", help="Path to .whl file or directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    if not args.wheel:
        args.wheel = Path("/sdcard/whl")
    path = Path(args.wheel)
    inspector = WheelInspector(verbose=args.verbose)
    if path.is_file() and path.suffix == ".whl":
        inspector.print_inspection(path)
    elif path.is_dir():
        for p in path.rglob("*.whl"):
            inspector.print_inspection(p)


if __name__ == "__main__":
    raise SystemExit(main())
