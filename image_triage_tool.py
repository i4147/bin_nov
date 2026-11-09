#!/data/data/com.termux/files/usr/bin/env python
"""Image triage: brightness, face presence, NSFW sorting, face boxing.
Analysis / deduplication ------------------------ - ``bw.py`` (mostly-dark/bright classification) -> ``brightness`` (its "ratio" printout multiplied nothing by 100; fixed).
- ``noface.py`` (Haar cascades, copies faceless images to ``/sdcard/DCIM/noface``, Termux cascade dir) -> ``faces`` with the cascade resolution fixed to prefer OpenCV's bundled cascades.
- ``isnude.py`` (``nude`` lib -> ``nude/``) and ``isporn.py`` (``NudeNet`` -> ``safe/``/``sexy/``/``porn/``) -> ``nsfw --detector nude|nudenet``; both now default to report-only and need ``--move`` to relocate files.
- ``boxfaces.py`` (draw face boxes on a video) -> ``box-faces``.
Dependencies per subcommand: Pillow (brightness), opencv-python (faces/box-faces/isnude sizing), ``nude`` or ``nudenet`` (nsfw).
Examples -------- python3 image_triage_tool.py brightness photo.jpg python3 image_triage_tool.py faces ~/Pictures --move-to ~/noface python3 image_triage_tool.py nsfw ~/Pictures --detector nudenet python3 image_triage_tool.py box-faces in.mp4 out.mp4"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys


IMAGE_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"})
CASCADE_NAMES: tuple[str, ...] = (
    "haarcascade_frontalface_default.xml",
    "haarcascade_frontalface_alt.xml",
    "haarcascade_frontalface_alt2.xml",
    "haarcascade_frontalface_alt_tree.xml",
    "frontalface_default.xml",
    "frontalface_alt.xml",
)
SEXY_LABELS: frozenset[str] = frozenset({
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
    "BELLY",
    "ARMPITS",
})
PORN_LABELS: frozenset[str] = frozenset({
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
})


def need(module_hint: str) -> SystemExit:
    """Exit message for a missing optional dependency."""
    return SystemExit(f"Missing optional dependency: pip install {module_hint}")


def find_images(root: Path, recursive: bool = True) -> list[Path]:
    """Collect image files from a file or directory argument."""
    if root.is_file():
        return [root]
    candidates = root.rglob("*") if recursive else root.iterdir()
    return sorted(p for p in candidates if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


# -------------------------------------------------------------- brightness
def analyze_brightness(path: Path, dark_threshold: int, ratio_threshold: float) -> tuple[str, float]:
    """Return (Mostly Dark|Mostly Bright|Mixed, dark-ratio 0..1)."""
    try:
        from PIL import Image
    except ImportError:
        msg = "Pillow"
        raise need(msg) from None
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        pixels = list(rgb.getdata())
    dark: int = sum(1 for r, g, b in pixels if 0.299 * r + 0.587 * g + 0.114 * b < dark_threshold)
    ratio: float = dark / max(len(pixels), 1)
    if ratio > ratio_threshold:
        return "Mostly Dark", ratio
    if ratio < 1 - ratio_threshold:
        return "Mostly Bright", ratio
    return "Mixed", ratio


def cmd_brightness(args: argparse.Namespace) -> int:
    """Classify each image by its dark-pixel ratio."""
    images: list[Path] = find_images(args.target)
    if not images:
        print("No images found.")
        return 0
    for path in images:
        label, ratio = analyze_brightness(path, args.dark_threshold, args.ratio_threshold)
        print(f"{path.name}: {label} (dark pixel ratio: {ratio * 100:.1f}%)")
        if args.sort_dir is not None:
            dest_dir: Path = args.sort_dir / label.lower().replace(" ", "-")
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest_dir / path.name)
    return 0


# ------------------------------------------------------------------- faces
def load_cascade(extra_dir: Path | None) -> object:
    """Load the best available Haar frontal-face cascade."""
    try:
        import cv2
    except ImportError:
        msg = "opencv-python"
        raise need(msg) from None
    search_dirs: list[Path] = []
    if extra_dir is not None:
        search_dirs.append(extra_dir)
    search_dirs.append(Path(str(cv2.data.haarcascades)))  # type: ignore[attr-defined]
    search_dirs.append(Path.home() / ".local" / "share" / "opencv4" / "haarcascades")
    for directory in search_dirs:
        for name in CASCADE_NAMES:
            candidate: Path = directory / name
            if candidate.is_file():
                cascade = cv2.CascadeClassifier(str(candidate))  # type: ignore[attr-defined]
                if not cascade.empty():
                    return cascade
    msg = "No Haar cascade XML found (try --cascade-dir)."
    raise SystemExit(msg)


def count_faces(path: Path, cascade: object, max_dimension: int) -> int | None:
    """Number of faces in an image (None = unreadable)."""
    import cv2

    image = cv2.imread(str(path))  # type: ignore[attr-defined]
    if image is None:
        return None
    height, width = image.shape[:2]
    scale: float = min(1.0, max_dimension / max(width, height)) if max(width, height) else 1.0
    if scale < 1.0:
        image = cv2.resize(image, (int(width * scale), int(height * scale)))  # type: ignore[attr-defined]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # type: ignore[attr-defined]
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))  # type: ignore[attr-defined]
    return len(faces)


def cmd_faces(args: argparse.Namespace) -> int:
    """Report face counts; optionally copy faceless images aside."""
    cascade: object = load_cascade(args.cascade_dir)
    images: list[Path] = find_images(args.target)
    noface: int = 0
    for path in images:
        count: int | None = count_faces(path, cascade, args.max_dimension)
        if count is None:
            print(f"{path}: unreadable")
            continue
        print(f"{path}: {count} face(s)")
        if count == 0:
            noface += 1
            if args.move_to is not None:
                args.move_to.mkdir(parents=True, exist_ok=True)
                if args.move:
                    shutil.move(str(path), str(args.move_to / path.name))
                else:
                    shutil.copy2(path, args.move_to / path.name)
    print(f"# {noface}/{len(images)} image(s) without faces", file=sys.stderr)
    return 0


def cmd_box_faces(args: argparse.Namespace) -> int:
    """Draw rectangles around faces in every frame of a video."""
    try:
        import cv2
    except ImportError:
        msg = "opencv-python"
        raise need(msg) from None
    if not args.input.is_file():
        print(f"Error: input not found: {args.input}", file=sys.stderr)
        return 1
    cascade: object = load_cascade(args.cascade_dir)
    capture = cv2.VideoCapture(str(args.input))  # type: ignore[attr-defined]
    if not capture.isOpened():
        print(f"Error: cannot open {args.input}", file=sys.stderr)
        return 1
    width: int = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))  # type: ignore[attr-defined]
    height: int = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))  # type: ignore[attr-defined]
    fps: float = float(capture.get(cv2.CAP_PROP_FPS)) or 25.0  # type: ignore[attr-defined]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(args.output),
        cv2.VideoWriter_fourcc(*args.fourcc),  # type: ignore[attr-defined]
        fps,
        (width, height),
    )
    frames: int = 0
    total_faces: int = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)  # type: ignore[attr-defined]
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))  # type: ignore[attr-defined]
        for x, y, w, h in faces:
            total_faces += 1
            cv2.rectangle(frame, (int(x), int(y)), (int(x + w), int(y + h)), (255, 0, 0), 2)  # type: ignore[attr-defined]
        writer.write(frame)
    capture.release()
    writer.release()
    print(f"Processed {frames} frame(s), {total_faces} face box(es) -> {args.output}")
    return 0


# -------------------------------------------------------------------- nsfw
def check_with_nude(path: Path, resize: bool) -> bool:
    """True when the ``nude`` library flags an image."""
    try:
        import nude as nude_lib
    except ImportError:
        msg = "nude"
        raise need(msg) from None
    detector = nude_lib.Nude(str(path))  # type: ignore[attr-defined]
    if resize:
        try:
            import cv2

            image = cv2.imread(str(path))  # type: ignore[attr-defined]
            if image is not None and max(image.shape[:2]) > 800:
                detector.resize(maxheight=800, maxwidth=800)
        except ImportError:
            pass
    detector.parse()
    return bool(detector.result)


def nudenet_detector() -> object:
    """Construct a NudeNet detector (cached as a function attribute)."""
    cached: object | None = getattr(nudenet_detector, "_cached", None)
    if cached is not None:
        return cached
    try:
        from nudenet import NudeDetector
    except ImportError:
        msg = "nudenet"
        raise need(msg) from None
    detector: object = NudeDetector()
    nudenet_detector._cached = detector
    return detector


def classify_nudenet(path: Path, threshold: float) -> str:
    """Return safe|sexy|porn for an image using NudeNet."""
    detector: object = nudenet_detector()
    detections: object = detector.detect(str(path))  # type: ignore[attr-defined]
    labels: set[str] = {
        str(item.get("class", ""))
        for item in detections  # type: ignore[union-attr]
        if float(item.get("score", 0)) >= threshold
    }
    if labels & PORN_LABELS:
        return "porn"
    if labels & SEXY_LABELS:
        return "sexy"
    return "safe"


def cmd_nsfw(args: argparse.Namespace) -> int:
    """Classify images as safe/sexy/porn (nudenet) or nude/not (nude)."""
    images: list[Path] = find_images(args.target)
    out_root: Path = args.out_dir
    counts: dict[str, int] = {}
    for path in images:
        if args.detector == "nude":
            label: str = "nude" if check_with_nude(path, args.resize) else "safe"
        else:
            label = classify_nudenet(path, args.threshold)
        counts[label] = counts.get(label, 0) + 1
        print(f"{path.name}: {label}")
        if args.move and label != "safe":
            dest_dir: Path = out_root / label
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest_dir / path.name))
    print(f"# {counts}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Image brightness/face/NSFW triage.")
    sub = parser.add_subparsers(dest="command", required=True)
    br = sub.add_parser("brightness", help="Mostly-dark vs mostly-bright")
    br.add_argument("target", type=Path, help="Image file or directory")
    br.add_argument("--dark-threshold", type=int, default=50, help="Luma below = dark (default: 50)")
    br.add_argument("--ratio-threshold", type=float, default=0.6, help="Dark ratio cutoff (default: 0.6)")
    br.add_argument("--sort-dir", type=Path, default=None, help="Also copy into label subdirs here")
    br.set_defaults(handler=cmd_brightness)
    fa = sub.add_parser("faces", help="Count faces; collect the faceless ones")
    fa.add_argument("target", type=Path)
    fa.add_argument("--move-to", type=Path, default=None, help="Copy no-face images here")
    fa.add_argument("--move", action="store_true", help="Move instead of copy (with --move-to)")
    fa.add_argument("--cascade-dir", type=Path, default=None)
    fa.add_argument("--max-dimension", type=int, default=640, help="Downscale cap (default: 640)")
    fa.set_defaults(handler=cmd_faces)
    bx = sub.add_parser("box-faces", help="Draw face boxes on a video")
    bx.add_argument("input", type=Path)
    bx.add_argument("output", nargs="?", type=Path, default=Path("out.mp4"))
    bx.add_argument("--fourcc", default="MJPG")
    bx.add_argument("--cascade-dir", type=Path, default=None)
    bx.set_defaults(handler=cmd_box_faces)
    ns = sub.add_parser("nsfw", help="NSFW classification/sorting")
    ns.add_argument("target", type=Path)
    ns.add_argument("--detector", choices=("nudenet", "nude"), default="nudenet")
    ns.add_argument("--threshold", type=float, default=0.5, help="NudeNet score cutoff")
    ns.add_argument("-r", "--resize", action="store_true", help="nude: shrink >800px first")
    ns.add_argument("--move", action="store_true", help="Move flagged files into label dirs")
    ns.add_argument("--out-dir", type=Path, default=Path.cwd())
    ns.set_defaults(handler=cmd_nsfw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
