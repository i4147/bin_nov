#!/data/data/com.termux/files/usr/bin/env python
"""Cut or reverse videos."""


import argparse
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence


def parse_time(
    time_str: str,
    *,
    hour_factor: int = 3600,
    minute_factor: int = 40,
    second_factor: int = 1,
    time_scale: int = 400,
) -> int:
    try:
        h, m, s = map(int, time_str.split(":"))
    except ValueError as exc:
        msg = f"Invalid time format {time_str!r}; expected hh:mm:ss"
        raise ValueError(msg) from exc
    return (h * hour_factor + m * minute_factor + s * second_factor) * time_scale


def cut_video(
    input_path: str,
    start_time: str,
    duration: str,
    *,
    output: Optional[str] = None,
    time_mode: str = "original",
    hour_factor: int = 3600,
    minute_factor: Optional[int] = None,
    second_factor: int = 1,
    time_scale: Optional[int] = None,
    ms_per_second: int = 1000,
) -> None:
    try:
        import cv2
    except ImportError:
        print(
            "Error: OpenCV (cv2) is required for the cut subcommand. Install with: pip install opencv-python",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if time_mode == "correct":
        if minute_factor is None:
            minute_factor = 60
        if time_scale is None:
            time_scale = 1000
    else:
        if minute_factor is None:
            minute_factor = 40
        if time_scale is None:
            time_scale = 400
    try:
        start_ms = parse_time(
            start_time,
            hour_factor=hour_factor,
            minute_factor=minute_factor,
            second_factor=second_factor,
            time_scale=time_scale,
        )
        duration_ms = parse_time(
            duration,
            hour_factor=hour_factor,
            minute_factor=minute_factor,
            second_factor=second_factor,
            time_scale=time_scale,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return
    input_path_obj = Path(input_path)
    if not input_path_obj.exists():
        print(f"Error: Input file '{input_path}' not found.")
        return
    cap = cv2.VideoCapture(str(input_path_obj))
    if not cap.isOpened():
        print(f"Error: Could not open video file '{input_path}'.")
        return
    out = None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        start_frame = int(start_ms * fps / ms_per_second)
        duration_frames = int(duration_ms * fps / ms_per_second)
        end_frame = start_frame + duration_frames
        if end_frame > frame_count:
            end_frame = frame_count
            print("Warning: Duration exceeds video length. Cutting until the end of the video.")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        if output is None:
            output = f"cut_{input_path_obj.name}"
        out = cv2.VideoWriter(
            output,
            fourcc,
            fps,
            (
                int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            ),
        )
        if not out.isOpened():
            print(f"Error: Could not create video writer for '{output}'.")
            return
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        processed = 0
        frames_to_process = end_frame - start_frame
        print(f"start_frame:{start_frame}/end_frame: {end_frame} -> {frames_to_process} frames to process")
        for _i in range(start_frame, end_frame):
            ret, frame = cap.read()
            if not ret:
                break
            out.write(frame)
            processed += 1
            print(f"{processed}/{frames_to_process}")
        print(f"Video segment saved to '{output}'")
        print(f"Frames processed: {processed}")
    finally:
        cap.release()
        if out is not None:
            out.release()
        cv2.destroyAllWindows()


def reverse_video(
    input_path: str,
    *,
    output: str = "reversed.mp4",
    keep_audio: bool = False,
    preset: str = "ultrafast",
    crf: Optional[int] = 23,
) -> None:
    cmd = ["ffmpeg", "-i", input_path, "-vf", "reverse"]
    if keep_audio:
        cmd += ["-af", "areverse"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", preset]
    if crf is not None:
        cmd += ["-crf", str(crf)]
    cmd += [output]
    print(f"Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(
            "Error: ffmpeg was not found. Please install ffmpeg and ensure it is on PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: ffmpeg failed with exit code {exc.returncode}.",
            file=sys.stderr,
        )
        raise SystemExit(exc.returncode)
    print(f"Saved to {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidtools.py",
        description="Cut or reverse videos (merged cutvid.py + reverse_video.py).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cut = sub.add_parser("cut", help="Cut a segment from a video (cutvid.py).")
    cut.add_argument("input", help="Input video file, e.g. input.mkv")
    cut.add_argument("start", help="Start time hh:mm:ss")
    cut.add_argument("duration", help="Duration hh:mm:ss")
    cut.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output file. Default: cut_<input name>",
    )
    cut.add_argument(
        "--time-mode",
        choices=("original", "correct"),
        default="original",
        help=(
            "Time conversion mode. 'original' preserves cutvid.py's formula; "
            "'correct' uses standard hh:mm:ss. Default: original"
        ),
    )
    cut.add_argument(
        "--hour-factor",
        type=int,
        default=3600,
        help="Advanced: hour multiplier. Default: 3600",
    )
    cut.add_argument(
        "--minute-factor",
        type=int,
        default=None,
        help="Advanced: minute multiplier. Default: 40 (original) or 60 (correct)",
    )
    cut.add_argument(
        "--second-factor",
        type=int,
        default=1,
        help="Advanced: second multiplier. Default: 1",
    )
    cut.add_argument(
        "--time-scale",
        type=int,
        default=None,
        help="Advanced: final scale. Default: 400 (original) or 1000 (correct)",
    )
    cut.add_argument(
        "--ms-per-second",
        type=int,
        default=1000,
        help="Advanced: milliseconds per second for frame conversion. Default: 1000",
    )
    rev = sub.add_parser(
        "reverse",
        help="Reverse a video with ffmpeg (reverse_video.py).",
    )
    rev.add_argument("input", help="Input video file")
    rev.add_argument(
        "-o",
        "--output",
        default="reversed.mp4",
        help="Output file. Default: reversed.mp4",
    )
    rev.add_argument(
        "--keep-audio",
        action="store_true",
        help=("Reverse audio too (matches s() in reverse_video.py). Default: false, drops audio."),
    )
    rev.add_argument(
        "--preset",
        default="ultrafast",
        help="x264 preset. Default: ultrafast (u()); use 'fast' for s()",
    )
    rev.add_argument(
        "--crf",
        type=int,
        default=23,
        help="x264 CRF. Default: 23 (u()); use --no-crf for s()",
    )
    rev.add_argument(
        "--no-crf",
        action="store_true",
        help="Omit -crf (matches s() in reverse_video.py).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "cut":
        cut_video(
            args.input,
            args.start,
            args.duration,
            output=args.output,
            time_mode=args.time_mode,
            hour_factor=args.hour_factor,
            minute_factor=args.minute_factor,
            second_factor=args.second_factor,
            time_scale=args.time_scale,
            ms_per_second=args.ms_per_second,
        )
        return 0
    if args.command == "reverse":
        crf = None if args.no_crf else args.crf
        reverse_video(
            args.input,
            output=args.output,
            keep_audio=args.keep_audio,
            preset=args.preset,
            crf=crf,
        )
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
