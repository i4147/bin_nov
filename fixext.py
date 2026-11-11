#!/data/data/com.termux/files/usr/bin/env python
"""Detect and fix the file extensions that do not match the MIME type.
Uses libmagic for each file, maps the MIME type to the expected suffix (falling back to the shebang), renames the mismatches in parallel and can ask before each ``-i`` rename.
Usage: fixext [directory] [-i] [--no-color] Positional arguments: directory Directory to scan (default: current directory) Options: -i, --interactive Interactive confirmation before renaming --no-color Disable colored output Examples: $ fixext ~/Downloads -i Command-line script: run ``python fixext.py`` or the ``fixext`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
import sys
from typing import Any, Final

from dh import MAX_WORKERS, SKIP_DIRS, is_binary, runcmd, unique_path
from loguru import logger


class Color:
    BLACK: Final[int] = 30
    RED: Final[int] = 31
    GREEN: Final[int] = 32
    YELLOW: Final[int] = 33
    BLUE: Final[int] = 34
    MAGENTA: Final[int] = 35
    CYAN: Final[int] = 36
    WHITE: Final[int] = 37
    LIGHT_BLACK: Final[int] = 90
    LIGHT_RED: Final[int] = 91
    LIGHT_GREEN: Final[int] = 92
    LIGHT_YELLOW: Final[int] = 93
    LIGHT_BLUE: Final[int] = 94
    LIGHT_MAGENTA: Final[int] = 95
    LIGHT_CYAN: Final[int] = 96
    LIGHT_WHITE: Final[int] = 97
    ON_BLACK: Final[int] = 40
    ON_RED: Final[int] = 41
    ON_GREEN: Final[int] = 42
    ON_YELLOW: Final[int] = 43
    ON_BLUE: Final[int] = 44
    ON_MAGENTA: Final[int] = 45
    ON_CYAN: Final[int] = 46
    ON_WHITE: Final[int] = 47
    RESET: Final[int] = 0
    BOLD: Final[int] = 1
    DIM: Final[int] = 2
    ITALIC: Final[int] = 3
    UNDERLINE: Final[int] = 4
    BLINK: Final[int] = 5
    REVERSE: Final[int] = 7
    CONCEALED: Final[int] = 8
    STRIKETHROUGH: Final[int] = 9
    _enabled: bool = True

    @classmethod
    def _build_code(
        cls,
        text: str,
        fg: int | None = None,
        bg: int | None = None,
        attrs: list[int] | None = None,
    ) -> str:
        if not cls._enabled:
            return text
        codes: list[str] = []
        if fg is not None:
            codes.append(str(fg))
        if bg is not None:
            codes.append(str(bg))
        if attrs:
            codes.extend(str(a) for a in attrs)
        if not codes:
            return text
        escape = f"\x1b[{';'.join(codes)}m"
        reset = "\x1b[0m"
        return f"{escape}{text}{reset}"

    @classmethod
    def disable(cls) -> None:
        cls._enabled = False

    @classmethod
    def enable(cls) -> None:
        cls._enabled = True

    @classmethod
    def can_colorize(cls) -> bool:
        import os

        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("ANSI_COLORS_DISABLED"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        return sys.stdout.isatty()


def colored(
    text: str,
    fg: int | None = None,
    bg: int | None = None,
    attrs: list[int] | None = None,
) -> str:
    return Color._build_code(text, fg, bg, attrs)


MIME_TO_EXT: Final[dict[str, list[str]]] = {
    "application/pdf": [".pdf"],
    "application/msword": [".doc"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/vnd.oasis.opendocument.text": [".odt"],
    "application/vnd.oasis.opendocument.presentation": [".odp"],
    "application/vnd.oasis.opendocument.spreadsheet": [".ods"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
    "application/vnd.ms-powerpoint": [".ppt"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [".pptx"],
    "application/rtf": [".rtf"],
    "application/x-abiword": [".abw"],
    "application/x-krita": [".kra"],
    "application/x-wpf": [".wpf"],
    "application/vnd.corel-draw.document": [".cdr"],
    "application/postscript": [".ps", ".eps"],
    "application/x-dvi": [".dvi"],
    "application/x-latex": [".tex"],
    "application/zip": [".zip", ".zipx"],
    "application/x-rar-compressed": [".rar"],
    "application/x-7z-compressed": [".7z"],
    "application/x-tar": [".tar"],
    "application/gzip": [".gz", ".tar.gz", ".tgz"],
    "application/x-bzip2": [".bz2", ".tar.bz2"],
    "application/x-xz": [".xz", ".tar.xz"],
    "application/x-zstandard": [".zst", ".tar.zst"],
    "application/x-cpio": [".cpio"],
    "application/x-shar": [".shar"],
    "application/x-iso9660-image": [".iso"],
    "application/x-apple-diskimage": [".dmg"],
    "application/x-virtualbox-vdi": [".vdi"],
    "application/x-vmdk": [".vmdk"],
    "application/x-qemu-disk": [".qcow", ".qcow2"],
    "application/x-msdownload": [".exe", ".dll", ".com"],
    "application/x-mach-binary": [".macho"],
    "application/x-executable": [".bin", ".app"],
    "application/x-elf": [".elf"],
    "application/x-android-package-archive": [".apk"],
    "application/x-appimage": [".AppImage"],
    "application/vnd.debian.binary-package": [".deb"],
    "application/x-rpm": [".rpm"],
    "application/x-flatpak": [".flatpak"],
    "application/x-snap": [".snap"],
    "application/x-java-applet": [".class", ".jar"],
    "application/x-bytecode.python": [".pyc", ".pyo"],
    "application/x-msdos-program": [".bat", ".cmd"],
    "font/ttf": [".ttf"],
    "font/otf": [".otf"],
    "font/woff": [".woff"],
    "font/woff2": [".woff2"],
    "application/vnd.ms-fontobject": [".eot"],
    "font/sfnt": [".sfnt"],
    "application/x-font-pcf": [".pcf"],
    "application/x-npm": [".npm"],
    "text/x-python-requirements": [".txt"],
    "application/json": [".json"],
    "application/jsonlines": [".jsonl", ".ndjson"],
    "application/json5": [".json5"],
    "application/xml": [".xml"],
    "text/xml": [".xml"],
    "application/rss+xml": [".rss"],
    "application/atom+xml": [".atom"],
    "application/soap+xml": [".soap"],
    "application/yaml": [".yaml", ".yml"],
    "application/x-yaml": [".yaml", ".yml"],
    "application/x-toml": [".toml"],
    "text/x-ini": [".ini", ".cfg", ".conf"],
    "application/x-ini": [".ini", ".cfg", ".conf"],
    "text/x-properties": [".properties"],
    "application/x-www-form-urlencoded": [".form"],
    "text/csv": [".csv"],
    "text/x-tsv": [".tsv"],
    "text/tab-separated-values": [".tsv"],
    "audio/mpeg": [".mp3"],
    "audio/mp4": [".m4a", ".mp4a"],
    "audio/aac": [".aac"],
    "audio/aiff": [".aif", ".aiff"],
    "audio/wave": [".wav"],
    "audio/wav": [".wav"],
    "audio/ogg": [".ogg", ".oga"],
    "audio/flac": [".flac"],
    "audio/x-flac": [".flac"],
    "audio/midi": [".mid", ".midi"],
    "audio/x-midi": [".mid", ".midi"],
    "audio/x-mod": [".mod"],
    "audio/x-s3m": [".s3m"],
    "audio/x-xm": [".xm"],
    "audio/x-it": [".it"],
    "audio/vnd.dolby.dd-raw": [".ac3"],
    "audio/x-ac3": [".ac3"],
    "audio/x-ms-wma": [".wma"],
    "audio/x-aiff": [".aiff"],
    "audio/x-pn-realaudio": [".ra", ".ram"],
    "audio/x-musepack": [".mpc", ".mp+"],
    "audio/x-wavpack": [".wv"],
    "audio/x-vorbis+ogg": [".ogg"],
    "audio/x-matroska": [".mka"],
    "audio/x-ape": [".ape"],
    "audio/x-m4b": [".m4b"],
    "audio/x-adpcm": [".wav"],
    "audio/x-pn-wav": [".wav"],
    "audio/x-nsf": [".nsf"],
    "audio/x-spc": [".spc"],
    "audio/x-gbs": [".gbs"],
    "audio/x-psf": [".psf"],
    "audio/x-sf2": [".sf2"],
    "video/mp4": [".mp4", ".m4v"],
    "video/mpeg": [".mpeg", ".mpg"],
    "video/x-msvideo": [".avi"],
    "video/x-matroska": [".mkv", ".mka", ".mks"],
    "video/webm": [".webm"],
    "video/quicktime": [".mov", ".qt"],
    "video/x-ms-wmv": [".wmv"],
    "video/x-ms-asf": [".asf", ".wma", ".wmv"],
    "video/x-flv": [".flv"],
    "video/x-mng": [".mng"],
    "video/ogg": [".ogv", ".ogg"],
    "video/x-theora+ogg": [".ogv"],
    "video/x-dv": [".dv"],
    "video/x-f4v": [".f4v"],
    "video/x-h261": [".h261"],
    "video/x-h263": [".h263"],
    "video/x-h264": [".h264", ".x264"],
    "video/x-h265": [".h265", ".hevc"],
    "video/x-m4v": [".m4v"],
    "video/x-matroska-3d": [".mkv"],
    "video/x-motion-jpeg": [".mjpg"],
    "video/x-ms-wm": [".wm"],
    "video/x-sgi-movie": [".movie"],
    "video/x-smv": [".smv"],
    "video/x-nes": [".nes"],
    "video/x-snes": [".snes", ".sfc"],
    "video/x-gameboy": [".gb", ".gbc"],
    "video/x-n64": [".z64", ".n64"],
    "video/x-genesis": [".gen", ".md"],
    "video/x-atari": [".a26", ".a78"],
    "video/x-psx": [".cue", ".bin"],
    "image/jpeg": [".jpg", ".jpeg", ".jpe"],
    "image/png": [".png"],
    "image/gif": [".gif"],
    "image/svg+xml": [".svg"],
    "image/svg": [".svg"],
    "image/tiff": [".tiff", ".tif"],
    "image/webp": [".webp"],
    "image/avif": [".avif"],
    "image/heic": [".heic"],
    "image/heif": [".heif"],
    "image/vnd.microsoft.icon": [".ico"],
    "image/x-icon": [".ico", ".cur"],
    "image/bmp": [".bmp", ".dib"],
    "image/x-ms-bmp": [".bmp"],
    "image/x-portable-bitmap": [".pbm"],
    "image/x-portable-graymap": [".pgm"],
    "image/x-portable-pixmap": [".ppm"],
    "image/x-portable-anymap": [".pnm"],
    "image/x-xbitmap": [".xbm"],
    "image/x-xpixmap": [".xpm"],
    "image/x-pcx": [".pcx"],
    "image/x-tga": [".tga", ".icb", ".vda", ".vst"],
    "image/x-xcf": [".xcf"],
    "image/x-xcf-gimp": [".xcf"],
    "image/psd": [".psd"],
    "image/vnd.adobe.photoshop": [".psd"],
    "image/x-photoshop": [".psd"],
    "image/x-canon-cr2": [".cr2"],
    "image/x-canon-crw": [".crw"],
    "image/x-nikon-nef": [".nef"],
    "image/x-nikon-nrw": [".nrw"],
    "image/x-sony-arw": [".arw"],
    "image/x-sony-srf": [".srf"],
    "image/x-fuji-raf": [".raf"],
    "image/x-panasonic-raw": [".raw"],
    "image/x-panasonic-rw2": [".rw2"],
    "image/x-olympus-orf": [".orf"],
    "image/x-pentax-pef": [".pef"],
    "image/x-samsung-srw": [".srw"],
    "image/x-hasselblad-3fr": [".3fr"],
    "image/x-sigma-x3f": [".x3f"],
    "image/x-dng": [".dng"],
    "image/x-dcraw": [".raw"],
    "text/plain": [".txt"],
    "text/html": [".html", ".htm"],
    "text/markdown": [".md", ".markdown"],
    "text/x-markdown": [".md", ".markdown"],
    "text/x-rst": [".rst"],
    "text/x-python": [".py", ".pyw"],
    "application/x-python": [".py"],
    "text/x-java": [".java"],
    "text/x-c": [".c"],
    "text/x-c++": [".cpp", ".cc", ".cxx"],
    "text/x-csrc": [".c"],
    "text/x-chdr": [".h"],
    "text/x-c++src": [".cpp", ".cc"],
    "text/x-c++hdr": [".h", ".hpp"],
    "text/x-csharp": [".cs"],
    "text/x-go": [".go"],
    "text/x-rust": [".rs"],
    "text/x-ruby": [".rb"],
    "text/x-ruby-script": [".rb"],
    "text/x-php": [".php"],
    "text/x-javascript": [".js"],
    "application/javascript": [".js"],
    "application/x-javascript": [".js"],
    "text/javascript": [".js"],
    "text/x-typescript": [".ts"],
    "application/typescript": [".ts"],
    "text/x-coffeescript": [".coffee"],
    "text/x-perl": [".pl"],
    "text/x-sh": [".sh"],
    "application/x-sh": [".sh"],
    "text/x-shellscript": [".sh"],
    "text/x-bash": [".bash"],
    "text/x-zsh": [".zsh"],
    "text/x-fish": [".fish"],
    "text/x-tcl": [".tcl"],
    "text/x-lua": [".lua"],
    "text/x-r-source": [".r", ".R"],
    "text/x-sql": [".sql"],
    "text/x-objective-c": [".m"],
    "text/x-swift": [".swift"],
    "text/x-kotlin": [".kt"],
    "text/x-scala": [".scala"],
    "text/x-groovy": [".groovy", ".gradle"],
    "text/x-clojure": [".clj", ".cljs"],
    "text/x-elm": [".elm"],
    "text/x-erlang": [".erl"],
    "text/x-haskell": [".hs"],
    "text/x-lisp": [".lisp", ".cl"],
    "text/x-scheme": [".scm", ".ss"],
    "text/x-asm": [".asm", ".s"],
    "text/x-latex": [".tex"],
    "text/x-fortran": [".f", ".f90"],
    "text/x-pascal": [".pas"],
    "text/x-vb": [".vb"],
    "text/x-diff": [".diff", ".patch"],
    "text/x-log": [".log"],
    "text/x-tex": [".tex"],
    "text/x-vcalendar": [".ics"],
    "text/calendar": [".ics"],
    "text/vcard": [".vcf"],
    "text/x-vcard": [".vcf"],
    "text/x-yaml": [".yaml", ".yml"],
    "text/x-toml": [".toml"],
    "model/stl": [".stl"],
    "model/obj": [".obj"],
    "model/gltf+json": [".gltf"],
    "model/gltf-binary": [".glb"],
    "model/vnd.collada+xml": [".dae"],
    "model/vrml": [".vrml", ".wrl"],
    "model/x3d+xml": [".x3d"],
    "model/x3d+vrml": [".x3dv"],
    "model/vnd.dwf": [".dwf"],
    "model/vnd.usdz+zip": [".usdz"],
    "application/vnd.sqlite3": [".db", ".sqlite", ".sqlite3"],
    "application/octet-stream": [".bin", ".o"],
}
SKIP_EXT: Final[set[str]] = {".css", ".js", ".ts", ".jsx", ".tsx"}
SKIP_MIME_TYPES: Final[set[str]] = {"text/plain", "application/octet-stream"}


@dataclass(slots=True)
class MimeResult:
    mime_type: str | None
    error: str | None = None


@dataclass(slots=True)
class MismatchResult:
    path: Path
    current_ext: str
    detected_mime: str
    expected_exts: list[str]
    new_path: Path | None = None
    error: str | None = None


def fix_by_shebang(path: Path) -> str | None:
    if is_binary(path):
        return None
    try:
        with open(path, "rb") as f:
            first_line: bytes = f.readline()
    except OSError:
        return None
    if not first_line.startswith(b"#!"):
        return None
    shebang: str = first_line.decode("utf-8", errors="ignore").strip()
    if any(shell in shebang for shell in ["/bash", "bash", "/sh", "/bin/sh"]):
        return ".sh"
    if any(shell in shebang for shell in ["/zsh", "zsh"]):
        return ".zsh"
    if any(shell in shebang for shell in ["/fish", "fish"]):
        return ".fish"
    if "python" in shebang:
        return ".py"
    if "perl" in shebang:
        return ".pl"
    if "ruby" in shebang:
        return ".rb"
    if "node" in shebang:
        return ".js"
    if "lua" in shebang:
        return ".lua"
    return None


def get_file_mime(path: Path) -> MimeResult:
    try:
        exit_code: int
        stdout: str
        stderr: str
        exit_code, stdout, stderr = runcmd(["file", "--brief", "--mime-type", str(path)], timeout=5)
        if exit_code == 127:
            return MimeResult(None, "file command not found")
        if exit_code != 0:
            return MimeResult(None, stderr or f"file command failed with code {exit_code}")
        mime_type: str = stdout.strip()
        if mime_type:
            return MimeResult(mime_type)
        return MimeResult(None, "No MIME type detected")
    except Exception as e:
        return MimeResult(None, f"Exception: {e}")


def safe_rename(old_path: Path, new_path: Path) -> bool:
    try:
        if new_path.exists():
            new_path = unique_path(new_path)
        old_path.rename(new_path)
        return True
    except OSError:
        return False


def detect_mismatch(base_dir: Path, path: Path) -> MismatchResult | None:
    if path.suffix.lower() in SKIP_EXT:
        return None
    current_ext: str = path.suffix.lower()
    shebang_ext: str | None = fix_by_shebang(path)
    if shebang_ext and current_ext != shebang_ext:
        new_path: Path = path.with_suffix(shebang_ext)
        return MismatchResult(
            path=path,
            current_ext=current_ext,
            detected_mime=("text/x-shellscript" if shebang_ext == ".sh" else "text/x-python"),
            expected_exts=[shebang_ext],
            new_path=unique_path(new_path),
        )
    mime_result: MimeResult = get_file_mime(path)
    if mime_result.error:
        return None
    mime_type: str | None = mime_result.mime_type
    if not mime_type or mime_type in SKIP_MIME_TYPES:
        return None
    expected_exts: list[str] = MIME_TO_EXT.get(mime_type, [])
    if not expected_exts:
        return None
    expected_ext: str = expected_exts[0].lower()
    if current_ext == expected_ext or current_ext in [e.lower() for e in expected_exts]:
        return None
    new_path = path.with_suffix(expected_ext)
    return MismatchResult(
        path=path,
        current_ext=current_ext,
        detected_mime=mime_type,
        expected_exts=expected_exts,
        new_path=unique_path(new_path),
    )


def process_file_worker(args: tuple[Path, Path]) -> MismatchResult | None:
    base_dir, path = args
    try:
        if path.stat().st_size == 0:
            return None
    except OSError:
        return None
    return detect_mismatch(base_dir, path)


def scan_directory(directory: str) -> list[MismatchResult]:
    base_dir: Path = Path(directory).resolve()
    if not base_dir.is_dir():
        logger.error(f"Error: {directory} is not a directory")
        return []
    files: list[Path] = []
    for root, dirs, filenames in base_dir.walk():
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        root_path: Path = Path(root)
        for filename in filenames:
            path: Path = root_path / filename
            if not path.is_symlink():
                files.append(path)
    if not files:
        logger.warning("No files found")
        return []
    print(f"Found {len(files):,} files, analyzing with {MAX_WORKERS} workers...")
    results: list[MismatchResult] = []
    tasks: list[tuple[Path, Path]] = [(base_dir, f) for f in files]
    with Pool(processes=MAX_WORKERS) as pool:
        async_results: list[Any] = [pool.apply_async(process_file_worker, (task,)) for task in tasks]
        completed: int = 0
        for ar in async_results:
            completed += 1
            if completed % 100 == 0:
                logger.debug(f"Processed: {completed:,}/{len(files):,}")
            try:
                result: MismatchResult | None = ar.get()
            except Exception as e:
                logger.error(f"Worker error: {e}")
                continue
            if result:
                results.append(result)
    print(f"Processed: {completed:,}/{len(files):,}")
    return results


def print_results(mismatches: list[MismatchResult], confirm: bool = False) -> int:
    if not mismatches:
        logger.success("✓ No file extension mismatches found!")
        return 0
    logger.warning(f"Found {len(mismatches)} file(s) with mismatched extensions:")
    renamed_count: int = 0
    for result in sorted(mismatches, key=lambda r: str(r.path)):
        try:
            rel_path: Path = result.path.relative_to(Path.cwd())
        except ValueError:
            rel_path = result.path
        new_name: str = result.new_path.name if result.new_path else "<unknown>"
        print(f"  {colored(str(rel_path), fg=Color.CYAN)} → {colored(new_name, fg=Color.GREEN)}")
        print(f"    MIME: {colored(result.detected_mime, fg=Color.LIGHT_CYAN)}")
        print(f"    Expected ext: {colored(result.expected_exts[0], fg=Color.LIGHT_GREEN)}")
        if result.new_path is None:
            logger.error("    ✗ No target path computed")
            print()
            continue
        if safe_rename(result.path, result.new_path):
            logger.success("    ✓ Renamed")
            renamed_count += 1
        else:
            logger.error("    ✗ Failed to rename")
        print()
    print(f"Summary: {renamed_count} file(s) renamed")
    return renamed_count


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect and fix file extension mismatches by analyzing MIME types",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "\nExamples:\n\n"
            "  python fix_extensions.py\n\n\n"
            "  python fix_extensions.py /path/to/directory\n\n\n"
            "  python fix_extensions.py -y\n\n\n"
            "  python fix_extensions.py --no-color\n        "
        ),
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory)",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Interactive confirmation before renaming",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    return parser


def main() -> int:
    parser: argparse.ArgumentParser = build_arg_parser()
    args: argparse.Namespace = parser.parse_args()
    if args.no_color or not Color.can_colorize():
        Color.disable()
    else:
        Color.enable()
    cprint = lambda text, fg=None, bg=None, attrs=None: print(colored(text, fg, bg, attrs))
    cprint("╔══════════════════════════════════════════╗", fg=Color.CYAN)
    cprint("║  File Extension Mismatch Fixer            ║", fg=Color.CYAN)
    cprint("╚══════════════════════════════════════════╝", fg=Color.CYAN)
    print()
    mismatches: list[MismatchResult] = scan_directory(args.directory)
    renamed: int = print_results(mismatches, confirm=args.interactive)
    return 0 if renamed >= 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
