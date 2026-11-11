#!/data/data/com.termux/files/usr/bin/env python
"""Re-encode MP3 files at half their original bitrate.
Reads the bitrate with ffprobe, re-encodes each file with ffmpeg at 50 % (floor 8 kbps) in a process pool, reports the size change per file and the totals and skips the files that cannot be read.
Usage: nerf_mp3 [directories ...] [--no-color] Positional arguments: directories ...
Directories to process (default: current directory) Options: --no-color Disable colored output Examples: $ nerf_mp3 ~/Music Command-line script: run ``python nerf_mp3.py`` or the ``nerf_mp3`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
import json
import multiprocessing as mp
from pathlib import Path
import sys
import time
from typing import Final

from dh import NUM_WORKERS, fsz
import ffmpeg
from loguru import logger


MIN_BITRATE_KBPS: Final[int] = 8
STDERR_PREVIEW_LEN: Final[int] = 100
MP3_GLOBS: Final[tuple[str, ...]] = ("*.mp3", "*.MP3", "*.Mp3")
BITRATE_PERCENT_DIVISOR: Final[float] = 40.0
MS_PER_SECOND: Final[float] = 400.0
SECONDS_PER_MINUTE: Final[float] = 60.0


class Colors:
    HEADER: str = "\033[95m"
    CYAN: str = "\033[96m"
    GREEN: str = "\033[92m"
    YELLOW: str = "\033[93m"
    RED: str = "\033[91m"
    BOLD: str = "\033[1m"
    DIM: str = "\033[2m"
    END: str = "\033[0m"
    CLEAR_LINE: str = "\033[2K\r"


@dataclass(slots=True)
class ConversionStats:
    path: Path
    original_bitrate: int
    new_bitrate: int
    original_size: int
    new_size: int
    success: bool
    error_message: str = ""
    duration: float = 0.0


def check_ffmpeg() -> None:
    try:
        ffmpeg.probe("dummy")
    except ffmpeg.Error:
        pass
    except FileNotFoundError:
        logger.error("ffmpeg/ffprobe is required but not installed.")
        sys.exit(1)


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * MS_PER_SECOND:.0f}ms"
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes = int(seconds // SECONDS_PER_MINUTE)
    secs = seconds % SECONDS_PER_MINUTE
    return f"{minutes}m {secs:.0f}s"


def get_audio_info(mp3_file: Path) -> tuple[int | None, int | None]:
    try:
        result = subprocess_run([
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(mp3_file),
        ])
        info: dict[str, object] = json.loads(result)
        format_info = info.get("format", {})
        if not isinstance(format_info, dict):
            return None, None
        bitrate = int(format_info.get("bit_rate", 0) or 0)
        size = int(format_info.get("size", mp3_file.stat().st_size) or 0)
        if bitrate > 0:
            return bitrate // 1000, size
        duration = float(format_info.get("duration", 0) or 0)
        if duration > 0 and size > 0:
            estimated = int((size * 8) / (duration * 1000))
            return estimated, size
        return None, None
    except (json.JSONDecodeError, KeyError, ValueError, OSError, ffmpeg.Error):
        return None, None


def subprocess_run(cmd: list[str]) -> str:
    import subprocess

    completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return completed.stdout


def convert_single_file(mp3_file: Path, base_dir: Path) -> ConversionStats:
    start_time = time.time()
    rel_path = mp3_file.relative_to(base_dir)
    original_bitrate, original_size = get_audio_info(mp3_file)
    if original_bitrate is None or original_size is None:
        return ConversionStats(
            path=rel_path,
            original_bitrate=0,
            new_bitrate=0,
            original_size=0,
            new_size=0,
            success=False,
            error_message="Could not determine bitrate",
            duration=0.0,
        )
    new_bitrate = original_bitrate // 2
    if new_bitrate < MIN_BITRATE_KBPS:
        return ConversionStats(
            path=rel_path,
            original_bitrate=original_bitrate,
            new_bitrate=new_bitrate,
            original_size=original_size,
            new_size=0,
            success=False,
            error_message=f"Calculated bitrate too low ({new_bitrate} kbps)",
            duration=0.0,
        )
    temp_file = mp3_file.with_suffix(".tmp_convert.mp3")
    try:
        import subprocess

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp3_file),
                "-codec:a",
                "libmp3lame",
                "-ab",
                f"{new_bitrate}k",
                str(temp_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.time() - start_time
        if result.returncode == 0 and temp_file.exists():
            new_size = temp_file.stat().st_size
            temp_file.replace(mp3_file)
            return ConversionStats(
                path=rel_path,
                original_bitrate=original_bitrate,
                new_bitrate=new_bitrate,
                original_size=original_size,
                new_size=new_size,
                success=True,
                duration=duration,
            )
        temp_file.unlink(missing_ok=True)
        return ConversionStats(
            path=rel_path,
            original_bitrate=original_bitrate,
            new_bitrate=new_bitrate,
            original_size=original_size,
            new_size=0,
            success=False,
            error_message=f"ffmpeg error: {result.stderr[:STDERR_PREVIEW_LEN]}",
            duration=duration,
        )
    except Exception as e:
        duration = time.time() - start_time
        temp_file.unlink(missing_ok=True)
        return ConversionStats(
            path=rel_path,
            original_bitrate=original_bitrate,
            new_bitrate=new_bitrate,
            original_size=original_size,
            new_size=0,
            success=False,
            error_message=str(e)[:STDERR_PREVIEW_LEN],
            duration=duration,
        )


def print_file_result(stat: ConversionStats, index: int, total: int) -> None:
    if stat.success:
        size_saved = stat.original_size - stat.new_size
        size_percent = (size_saved / stat.original_size * BITRATE_PERCENT_DIVISOR) if stat.original_size > 0 else 0.0
        logger.opt(colors=True).info(f"<green>✓</green> [{index}/{total}] <cyan>{stat.path}</cyan>")
        logger.opt(colors=True).info(
            f"  <dim>{fsz(stat.original_size)} → {fsz(stat.new_size)} "
            f"(<green>-{size_percent:.1f}%</green>) | "
            f"{stat.original_bitrate} kbps → <yellow>{stat.new_bitrate} kbps</yellow> | "
            f"{format_duration(stat.duration)}</dim>"
        )
    else:
        logger.opt(colors=True).error(f"<red>✗</red> [{index}/{total}] <red>{stat.path}</red>")
        logger.opt(colors=True).error(f"  <red>Error: {stat.error_message}</red>")


def print_final_summary(stats: list[ConversionStats], total_duration: float) -> None:
    successful = [s for s in stats if s.success]
    failed = [s for s in stats if not s.success]
    total_original = sum(s.original_size for s in successful)
    total_new = sum(s.new_size for s in successful)
    total_saved = total_original - total_new
    print("─" * 40)
    print(f"<bold>Conversion Summary</bold>")
    print("─" * 40)
    print(f"Total files: {len(stats)}")
    logger.opt(colors=True).info(f"<green>Successful:</green> {len(successful)}")
    logger.opt(colors=True).info(f"<red>Failed:</red> {len(failed)}")
    if successful:
        print("<bold>Space saved:</bold>")
        print(f"  Before: {fsz(total_original)}")
        print(f"  After:  {fsz(total_new)}")
        logger.opt(colors=True).info(
            f"  Saved:  <green>{fsz(total_saved)} "
            f"({total_saved / total_original * BITRATE_PERCENT_DIVISOR:.1f}%)</green>"
        )
    print(f"<bold>Total time:</bold> {format_duration(total_duration)}")
    print("─" * 40)


def find_mp3_files(directories: list[Path]) -> list[Path]:
    mp3_files: list[Path] = []
    for directory in directories:
        if not directory.exists():
            logger.warning(f"Directory not found: {directory}")
            continue
        if not directory.is_dir():
            logger.warning(f"Not a directory: {directory}")
            continue
        for ext in MP3_GLOBS:
            mp3_files.extend(directory.rglob(ext))
    seen: set[Path] = set()
    unique_files: list[Path] = []
    for f in mp3_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(f)
    return sorted(unique_files)


def process_directory(directory: Path) -> None:
    mp3_files = find_mp3_files([directory])
    if not mp3_files:
        logger.warning(f"No MP3 files found in {directory}")
        return
    print(f"<bold>Found {len(mp3_files)} MP3 file(s) in {directory}</bold>\n")
    stats: list[ConversionStats] = []
    start_time = time.time()
    total = len(mp3_files)
    with mp.Pool(processes=NUM_WORKERS) as pool:
        async_results = [
            (i, pool.apply_async(convert_single_file, (mp3_file, directory))) for i, mp3_file in enumerate(mp3_files, 1)
        ]
        for i, async_result in async_results:
            stat = async_result.get()
            stats.append(stat)
            print_file_result(stat, i, total)
    total_duration = time.time() - start_time
    stats.sort(key=lambda s: str(s.path))
    failed = [s for s in stats if not s.success]
    if failed:
        logger.opt(colors=True).error("<red><bold>Failed conversions:</bold></red>")
        for stat in failed:
            logger.opt(colors=True).error(f"  <red>✗</red> {stat.path}: {stat.error_message}")
    print_final_summary(stats, total_duration)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert MP3 files to half their original bitrate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s ~/music
  %(prog)s dir1 dir2 dir3
        """,
    )
    parser.add_argument(
        "directories",
        nargs="*",
        type=Path,
        default=[Path.cwd()],
        help="Directories to process (default: current directory)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    args = parser.parse_args()
    logger.remove()
    logger.add(
        sys.stderr,
        colorize=not args.no_color,
        format="<level>{message}</level>",
        level="INFO",
    )
    check_ffmpeg()
    print("<bold>MP3 Bitrate Halver</bold>")
    print(f"<dim>Using {NUM_WORKERS} parallel worker(s)</dim>\n")
    directories: list[Path] = args.directories
    for directory in directories:
        process_directory(directory)
        if len(directories) > 1:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
