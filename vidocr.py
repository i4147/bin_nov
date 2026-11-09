#!/data/data/com.termux/files/usr/bin/env python
"""Extract burned-in subtitles from a video using OCR."""


import argparse
from functools import partial
import multiprocessing as mp
from pathlib import Path
import re
from typing import TYPE_CHECKING, Optional, Sequence

import cv2
from dh import MAX_WORKERS
import numpy as np
import pytesseract


def parse_hms(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        msg = f"Invalid time format: {value}. Expected HH:MM:SS"
        raise ValueError(msg)
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt_time(value: str) -> float:
    h, m, rest = value.split(":")
    s, ms = rest.replace(",", ".").split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def format_srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    ms = round((s - int(s)) * 1000)
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"


def _ocr_worker(
    payload: tuple[float, np.ndarray],
    config: str,
) -> tuple[float, str]:
    t, frame = payload
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        text = pytesseract.image_to_string(binary, config=config).strip()
        if text:
            print(f"[{format_srt_time(t)}] {text}")
        return t, text
    except Exception:
        return t, ""


def _frames_similar(a: np.ndarray, b: np.ndarray, threshold: float) -> bool:
    ra = cv2.resize(a, (64, 32))
    rb = cv2.resize(b, (64, 32))
    diff = cv2.absdiff(ra, rb)
    score = 1.0 - diff.sum() / (diff.size * 255.0)
    return score >= threshold


def extract_frames(
    video_path: str,
    sample_fps: float = 2.0,
    crop_bottom: float = 0.75,
    start: Optional[float] = None,
    end: Optional[float] = None,
    diff_threshold: float = 0.97,
    verbose: bool = False,
) -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        msg = f"Cannot open video: {video_path}"
        raise OSError(msg)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(fps / sample_fps))
    idx = 0
    if start is not None and start > 0:
        idx = int(start * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    frames: list[tuple[float, np.ndarray]] = []
    prev: Optional[np.ndarray] = None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ts = idx / fps
        if end is not None and ts > end:
            break
        if idx % step == 0:
            h = frame.shape[0]
            cropped = frame[int(h * crop_bottom) :].copy()
            if prev is None or not _frames_similar(prev, cropped, diff_threshold):
                frames.append((ts, cropped))
                prev = cropped
        idx += 1
        if verbose:
            print(idx)
    cap.release()
    return frames


def read_srt(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    time_re = re.compile(r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.isdigit():
            i += 1
            continue
        m = time_re.match(line)
        if not m:
            i += 1
            continue
        start = parse_srt_time(m.group(1))
        end = parse_srt_time(m.group(2))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        text = "\n".join(body)
        if text:
            entries.append({"start": start, "end": end, "text": text})
    return entries


def write_srt(path: Path, segments: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}\n")
            f.write(f"{seg['text']}\n\n")


def merge_segments(segments: list[dict], gap: float = 1.0) -> list[dict]:
    if not segments:
        return []
    out: list[dict] = []
    cur = dict(segments[0])
    for seg in segments[1:]:
        same = seg["text"] == cur["text"]
        close = seg["start"] - cur["end"] <= gap
        if same and close:
            cur["end"] = seg["end"]
        else:
            out.append(cur)
            cur = dict(seg)
    out.append(cur)
    return out


def run(
    video: str,
    output: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    resume: bool = False,
    sample_fps: float = 2.0,
    workers: Optional[int] = None,
    lang: str = "fas",
    crop_bottom: float = 0.75,
    diff_threshold: float = 0.97,
    merge_gap: float = 1.0,
    verbose: bool = False,
) -> None:
    if workers is None or workers < 1:
        workers = max(1, mp.cpu_count() - 1)
    output_path = Path(output)
    if resume and output_path.is_file():
        existing = read_srt(output_path)
        if existing and start is None:
            start = max(s["end"] for s in existing)
            print(f"Resuming from {format_srt_time(start)}")
    span = ""
    if start is not None and end is not None:
        span = f" from {format_srt_time(start)} to {format_srt_time(end)}"
    elif start is not None:
        span = f" from {format_srt_time(start)} to end"
    elif end is not None:
        span = f" from start to {format_srt_time(end)}"
    print(f"[1/3] Extracting frames ({sample_fps} fps sample{span})…")
    frames = extract_frames(video, sample_fps, crop_bottom, start, end, diff_threshold, verbose)
    print(f"      {len(frames)} unique frames queued for OCR")
    config = f"--oem 3 --psm 6 -l {lang}"
    print(f"[2/3] Running OCR with {workers} worker(s)…")
    worker = partial(_ocr_worker, config=config)
    with mp.Pool(processes=workers) as pool:
        results = pool.map(worker, frames)
    fresh = [{"start": t, "end": t + 1.0 / sample_fps, "text": text} for t, text in results if text]
    if resume and output_path.is_file():
        existing = read_srt(output_path)
        if existing:
            if start is not None:
                kept = [s for s in existing if s["end"] <= start]
                all_segs = kept + fresh
            else:
                all_segs = existing + fresh
        else:
            all_segs = fresh
    else:
        all_segs = fresh
    all_segs.sort(key=lambda s: s["start"])
    merged = merge_segments(all_segs, merge_gap)
    print(f"[3/3] Writing {len(merged)} subtitle(s) -> {output}")
    write_srt(output_path, merged)
    print("Done.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract burned-in subtitles from a video using OCR.",
    )
    p.add_argument("video", help="Path to the video file")
    p.add_argument(
        "output",
        nargs="?",
        default="extracted_subs.srt",
        help="Output SRT file (default: extracted_subs.srt)",
    )
    p.add_argument("-s", "--start", dest="start_time", help="Start time for extraction (HH:MM:SS)")
    p.add_argument("-e", "--end", dest="end_time", help="End time for extraction (HH:MM:SS)")
    p.add_argument(
        "-r",
        "--resume",
        action="store_true",
        help="Resume from previous run (appends to existing SRT)",
    )
    p.add_argument(
        "--sample-fps",
        type=float,
        default=2.0,
        help="Frames per second to sample (default: 2.0)",
    )
    p.add_argument("--lang", default="fas", help="Tesseract language code (default: fas)")
    p.add_argument(
        "--crop-bottom",
        type=float,
        default=0.75,
        help="Top fraction of frame to ignore (default: 0.75)",
    )
    p.add_argument(
        "--diff-threshold",
        type=float,
        default=0.97,
        help="Frame similarity threshold (default: 0.97)",
    )
    p.add_argument(
        "--merge-gap",
        type=float,
        default=1.0,
        help="Max gap in seconds to merge identical lines (default: 1.0)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print per-frame progress while scanning",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output and re.match(r"\d{1,2}:\d{2}:\d{2}", args.output) and not args.start_time and not args.end_time:
        args.end_time = args.output
        args.output = "extracted_subs.srt"
    start = parse_hms(args.start_time) if args.start_time else None
    end = parse_hms(args.end_time) if args.end_time else None
    run(
        video=args.video,
        output=args.output,
        start=start,
        end=end,
        resume=args.resume,
        sample_fps=args.sample_fps,
        workers=MAX_WORKERS,
        lang=args.lang,
        crop_bottom=args.crop_bottom,
        diff_threshold=args.diff_threshold,
        merge_gap=args.merge_gap,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
