#!/data/data/com.termux/files/usr/bin/env python
"""Extract on-screen text from a video using Tesseract OCR."""


import argparse
import os
from pathlib import Path
from queue import Queue
import sys
import threading
from typing import Any, Optional

import cv2
from dh import MAX_WORKERS
from PIL import Image
import pytesseract


_ANSI: dict[str, str] = {"cyan": "\033[36m", "blue": "\033[34m", "reset": "\033[0m"}


def cprint(message: str, color: str = "cyan") -> None:
    prefix = _ANSI.get(color, "")
    sys.stdout.write(f"{prefix}{message}{_ANSI['reset']}\n")
    sys.stdout.flush()


def ocr_frame(
    frame: Any,
    scale: float,
    lang: str,
    psm: int,
    oem: int,
    invert: bool,
):
    resized = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    if invert:
        gray = 255 - gray
    config = f"--oem {oem} --psm {psm}"
    return pytesseract.image_to_string(Image.fromarray(gray), lang=lang, config=config)


def emit(
    frame_num: int,
    text: str,
    out_path: Path,
    min_chars: int,
) -> None:
    if text and len(text.strip()) > min_chars:
        cprint(f"frame {frame_num}-->{text}", "cyan")
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        cprint(f"frame {frame_num}-->no text", "blue")


def worker(
    in_q: "Queue",
    done_q: "Queue",
    out_path: Path,
    opts: argparse.Namespace,
) -> None:
    while True:
        item = in_q.get()
        if item is None:
            break
        frame_num, frame = item
        text = ocr_frame(frame, opts.scale, opts.lang, opts.psm, opts.oem, opts.invert)
        emit(frame_num, text, out_path, opts.min_chars)
        done_q.put((frame_num, text))


def run_threaded(video: Path, out_path: Path, opts: argparse.Namespace) -> None:
    cap = cv2.VideoCapture(str(video))
    in_q: "Queue" = Queue(maxsize=opts.queue_size)
    done_q: "Queue" = Queue()
    workers = [
        threading.Thread(target=worker, args=(in_q, done_q, out_path, opts), daemon=False) for _ in range(MAX_WORKERS)
    ]
    for w in workers:
        w.start()
    count = 0
    frame_num = opts.frame_start
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        in_q.put((frame_num, frame))
        frame_num += 1
        count += 1
    for _ in workers:
        in_q.put(None)
    received = 0
    while received < count:
        done_q.get()
        received += 1
    cap.release()
    for w in workers:
        w.join()


def run_sequential(video: Path, out_path: Path, opts: argparse.Namespace) -> None:
    cap = cv2.VideoCapture(str(video))
    frame_num = opts.frame_start
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        text = ocr_frame(frame, opts.scale, opts.lang, opts.psm, opts.oem, opts.invert)
        emit(frame_num, text, out_path, opts.min_chars)
        frame_num += 1
    cap.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_ocr",
        description="Extract on-screen text from a video using Tesseract OCR.",
    )
    parser.add_argument("input", type=Path, help="Path to the input video.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output text file (default: <input>.txt).",
    )
    parser.add_argument(
        "--mode",
        choices=("threaded", "sequential"),
        default="threaded",
        help="Execution strategy (default: threaded).",
    )
    parser.add_argument(
        "--queue-size",
        type=int,
        default=os.cpu_count() or 1,
        help="Max size of the frame queue (default: CPU count).",
    )
    parser.add_argument(
        "--frame-start",
        type=int,
        default=None,
        help="First frame index (default: 1 threaded, 0 sequential).",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.5,
        help="Resize factor applied before OCR (default: 1.5).",
    )
    parser.add_argument("--lang", default="eng", help="Tesseract language (default: eng).")
    parser.add_argument("--psm", type=int, default=6, help="Tesseract PSM (default: 6).")
    parser.add_argument("--oem", type=int, default=3, help="Tesseract OEM (default: 3).")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=5,
        help="Minimum stripped text length to record (default: 5).",
    )
    parser.add_argument(
        "--no-invert",
        dest="invert",
        action="store_false",
        help="Disable the 255-gray inversion step.",
    )
    parser.set_defaults(invert=True)
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.input.is_file():
        parser.error(f"input video not found: {args.input}")
    if args.frame_start is None:
        args.frame_start = 1 if args.mode == "threaded" else 0
    out_path: Path = args.output if args.output is not None else args.input.with_suffix(".txt")
    if args.mode == "threaded":
        run_threaded(args.input, out_path, args)
    else:
        run_sequential(args.input, out_path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
