#!/data/data/com.termux/files/usr/bin/env python
"""Extract the subtitle streams embedded in a video with ffmpeg/ffprobe.
Subcommands: ``extract`` writes every subtitle stream, ``first`` only the first one as ``<video>.srt`` and ``list`` prints the streams without extracting; the ``--naming`` scheme (indexed, rich or codec) controls the file names and ``--check-ffmpeg`` verifies the tools first.
Usage: pyxsrt {extract,first,list} VIDEO [-o OUTDIR] [--naming indexed|rich|codec] Positional arguments: video Video file containing subtitle streams Subcommands: extract Extract all subtitle streams first Extract one subtitle stream to <video>.srt list List subtitle streams without extracting Options: --check-ffmpeg Run 'ffmpeg -version' first and abort if missing (exsrt2.py behaviour) --no-overwrite Omit -y so ffmpeg prompts before overwriting existing files (exsrt.py behaviour) -o, --output-dir Output directory (default: video's directory; xsub.py used 'subtitles') --naming Output naming scheme: indexed=stem.sub<i>.<lang>.srt, rich=stem.sub<abs>.<lang>.<Title>.forced.srt, codec=stem.<lang>[.<Title>].<srt|codec> (default: indexed) --on-error Abort at first ffmpeg failure or continue with the next stream (default: abort) --verbose Also print raw and parsed stream details (exsrt3.py behaviour) -o, --output Output .srt path (default: <video>.srt) -n, --stream-index Subtitle stream to map, as 0:s:N (default: 0) Examples: $ pyxsrt list movie.mkv $ pyxsrt extract movie.mkv -o subs/ Command-line script: run ``python pyxsrt.py`` or the ``pyxsrt`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys


SRT_CODEC_NAMES: frozenset[str] = frozenset({"subrip", "srt"})
PROBE_ENTRIES: str = "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced"


class ToolError(RuntimeError):
    pass


class MissingToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SubtitleStream:
    stream_index: int
    relative_index: int
    codec_name: str
    language: str
    title: str | None
    forced: bool


def require_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except FileNotFoundError:
        msg = "ffmpeg is required but not installed."
        raise MissingToolError(msg) from None


def probe_subtitles(video: Path) -> list[dict]:
    cmd: list[str] = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        PROBE_ENTRIES,
        "-of",
        "json",
        str(video),
    ]
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        msg = "ffmpeg/ffprobe is required but not installed."
        raise MissingToolError(msg) from None
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or f"ffprobe failed on {video}")
    return json.loads(proc.stdout).get("streams", [])


def parse_stream(raw: dict, relative_index: int) -> SubtitleStream:
    tags: dict = raw.get("tags") or {}
    disposition: dict = raw.get("disposition") or {}
    return SubtitleStream(
        stream_index=int(raw["index"]),
        relative_index=relative_index,
        codec_name=raw.get("codec_name", "sub"),
        language=tags.get("language", "und"),
        title=tags.get("title") or None,
        forced=bool(disposition.get("forced", 0)),
    )


def run_ffmpeg(args: list[str]) -> None:
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(["ffmpeg", *args], capture_output=True, text=True)
    except FileNotFoundError:
        msg = "ffmpeg/ffprobe is required but not installed."
        raise MissingToolError(msg) from None
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or "ffmpeg failed")


def extract_one(video: Path, subtitle_index: int, dest: Path, overwrite: bool) -> None:
    args: list[str] = []
    if overwrite:
        args.append("-y")
    args += ["-i", str(video), "-map", f"0:s:{subtitle_index}", str(dest)]
    run_ffmpeg(args)


def sanitize_rich_title(title: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in title)


def build_output_path(info: SubtitleStream, naming: str, video: Path, out_dir: Path) -> Path:
    stem: str = video.stem
    if naming == "indexed":
        name: str = f"{stem}.sub{info.relative_index}.{info.language}.srt"
    elif naming == "rich":
        parts: list[str] = [stem, f"sub{info.stream_index}"]
        if info.language != "und":
            parts.append(info.language)
        if info.title:
            parts.append(sanitize_rich_title(info.title))
        if info.forced:
            parts.append("forced")
        name = ".".join(parts) + ".srt"
    else:
        ext: str = "srt" if info.codec_name in SRT_CODEC_NAMES else info.codec_name
        name = f"{stem}.{info.language}"
        if info.title:
            name += f".{info.title.replace(' ', '_')}"
        name += f".{ext}"
    return out_dir / name


def load_streams(video: Path) -> tuple[list[dict], list[SubtitleStream]]:
    raw: list[dict] = probe_subtitles(video)
    return raw, [parse_stream(item, i) for i, item in enumerate(raw)]


def cmd_extract(args: argparse.Namespace) -> int:
    video: Path = Path(args.video)
    if not video.exists():
        print(f"File not found:{video}")
        return 1
    if args.check_ffmpeg:
        require_ffmpeg()
    try:
        raw_streams: list[dict]
        streams: list[SubtitleStream]
        raw_streams, streams = load_streams(video)
    except MissingToolError as exc:
        print(exc)
        return 1
    except ToolError as exc:
        print(f"Error probing file:{exc}")
        return 1
    if args.verbose:
        for item in raw_streams:
            print(item)
        for info in streams:
            print(info)
    if not streams:
        print("No subtitle streams found.")
        return 0
    out_dir: Path = Path(args.output_dir) if args.output_dir else video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.verbose:
        print(f"Found {len(streams)} subtitle streams.")
    failures: int = 0
    for info in streams:
        dest: Path = build_output_path(info, args.naming, video, out_dir)
        if args.verbose:
            print(
                f"Extracting stream index {info.stream_index} "
                f"(Lang:{info.language},Forced:{info.forced},"
                f"Codec:{info.codec_name})->{dest}"
            )
        else:
            print(f"Extracting subtitle stream {info.stream_index}->{dest}")
        try:
            extract_one(video, info.relative_index, dest, overwrite=not args.no_overwrite)
        except MissingToolError as exc:
            print(exc)
            return 1
        except ToolError as exc:
            if args.on_error == "abort":
                print(f"Error:{exc}")
                return 1
            print(f"Failed to extract subtitle stream {info.stream_index}:{exc}")
            failures += 1
        else:
            if args.on_error == "continue":
                print(f"Extracted:{dest}")
    if failures:
        print(f"{failures} of {len(streams)} subtitle stream(s) failed.")
    print("Done.")
    return 0


def cmd_first(args: argparse.Namespace) -> int:
    video: Path = Path(args.video)
    if not video.exists():
        print(f"File not found:{video}")
        return 1
    dest: Path = Path(args.output) if args.output else video.with_suffix(".srt")
    if args.check_ffmpeg:
        require_ffmpeg()
    try:
        extract_one(video, args.stream_index, dest, overwrite=not args.no_overwrite)
    except MissingToolError as exc:
        print(exc)
        return 1
    except ToolError as exc:
        print(f"Error:{exc}")
        return 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    video: Path = Path(args.video)
    if not video.exists():
        print(f"File not found:{video}")
        return 1
    try:
        raw_streams: list[dict]
        streams: list[SubtitleStream]
        raw_streams, streams = load_streams(video)
    except MissingToolError as exc:
        print(exc)
        return 1
    except ToolError as exc:
        print(f"Error probing file:{exc}")
        return 1
    if args.verbose:
        for item in raw_streams:
            print(item)
    for info in streams:
        print(info)
    if not streams:
        print("No subtitle streams found.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="extract_subs.py",
        description="Extract embedded subtitle streams from a video file using ffmpeg/ffprobe.",
    )
    common: argparse.ArgumentParser = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--check-ffmpeg",
        action="store_true",
        help="run 'ffmpeg -version' first and abort if missing (exsrt2.py behaviour)",
    )
    common.add_argument(
        "--no-overwrite",
        action="store_true",
        help="omit -y so ffmpeg prompts before overwriting existing files (exsrt.py behaviour)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_extract = sub.add_parser("extract", parents=[common], help="extract all subtitle streams")
    p_extract.add_argument("video", help="video file containing subtitle streams")
    p_extract.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="output directory (default: video's directory; xsub.py used 'subtitles')",
    )
    p_extract.add_argument(
        "--naming",
        choices=("indexed", "rich", "codec"),
        default="indexed",
        help="output naming scheme: indexed=stem.sub<i>.<lang>.srt, "
        "rich=stem.sub<abs>.<lang>.<Title>.forced.srt, "
        "codec=stem.<lang>[.<Title>].<srt|codec> (default: indexed)",
    )
    p_extract.add_argument(
        "--on-error",
        choices=("abort", "continue"),
        default="abort",
        help="abort at first ffmpeg failure or continue with the next stream (default: abort)",
    )
    p_extract.add_argument(
        "--verbose",
        action="store_true",
        help="also print raw and parsed stream details (exsrt3.py behaviour)",
    )
    p_first = sub.add_parser("first", parents=[common], help="extract one subtitle stream to <video>.srt")
    p_first.add_argument("video", help="video file containing subtitle streams")
    p_first.add_argument("-o", "--output", default=None, help="output .srt path (default: <video>.srt)")
    p_first.add_argument(
        "-n",
        "--stream-index",
        type=int,
        default=0,
        help="subtitle stream to map, as 0:s:N (default: 0)",
    )
    p_list = sub.add_parser("list", help="list subtitle streams without extracting")
    p_list.add_argument("video", help="video file containing subtitle streams")
    p_list.add_argument("--verbose", action="store_true", help="also print raw ffprobe stream records")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = build_parser()
    if argv is None and len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        return 2
    args: argparse.Namespace = parser.parse_args(argv)
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "first":
        return cmd_first(args)
    return cmd_list(args)


if __name__ == "__main__":
    raise SystemExit(main())
