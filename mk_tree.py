#!/data/data/com.termux/files/usr/bin/env python
"""Recreate a directory tree from a ``tree`` listing or a screenshot of one.
Parses the box-drawing markup of the text listing (or OCRs the screenshot with the available engine), infers the depth from the indentation, creates the directories and files under ``--base`` and offers ``--assume-dir``, ``--keep-suffix`` and ``--dry-run``.
Usage: mk_tree INPUT [--base DIR] [-d] [-n] Positional arguments: input Text listing or screenshot image Options: --base Directory to build the tree in (default: current directory) -d, --assume-dir Entries without visible children are created as directories (default: files) -n, --dry-run Print the plan without creating anything --engine OCR engine (default: auto) --keep-suffix Keep tree -F suffixes (* @ = |) in names Examples: $ mk_tree listing.txt --base ./rebuilt --dry-run Command-line script: run ``python mk_tree.py`` or the ``mk_tree`` command.
The module is importable as well."""

import argparse
import itertools
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable

NODE_MARKERS: tuple[str, str, str, str] = ("\u251c\u2500\u2500", "\u2514\u2500\u2500", "|--", "`--")
FOOTER_RE = re.compile(r"^\d+\s+(directories|files|dirs|items)(,\s*\d+\s+(directories|files|dirs|items))?$")
JUNK_LINE_CHARS = set(" \u2502\u251c\u2514\u2500|+-`")
JUNK_TOKEN_RE = re.compile(r"^[\u2502\u251c\u2514\u2500|+\-]+$")
LEAD_JUNK_RE = re.compile(r"^([\u2502\u251c\u2514\u2500|+`\-]{2,}|[\u2502\u251c\u2514\u2500|+`\-]+\s+)")


def strip_leading_junk(left: Any, text: str):
    m = LEAD_JUNK_RE.match(text)
    if not m:
        return left, text
    return left + len(m.group(0)), text[m.end() :]


IMG_EXTS: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def find_node_marker(line: str):
    best, best_len = None, 0
    for m in NODE_MARKERS:
        i = line.find(m)
        if i != -1 and (best is None or i < best):
            best, best_len = i, len(m)
    return best, best_len


def clean_name(raw: str, keep_suffix=False):
    raw = raw.strip()
    raw = re.sub(r"\s+#.*$", "", raw).strip()
    if not raw or raw.startswith("#"):
        return None, False
    explicit = raw.endswith("/")
    raw = raw.rstrip("/")
    if not keep_suffix:
        raw = raw.rstrip("*@=|")
    raw = raw.strip()
    if not raw:
        return None, explicit
    if raw == ".\\":
        raw = "."
    return raw, explicit


def median(values: list[Any]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def cluster_means(values: list[Any], min_gap=None) -> list[float]:
    values = sorted(values)
    if len(values) <= 1:
        return [float(v) for v in values]
    if min_gap is None:
        diffs = [b - a for a, b in itertools.pairwise(values)]
        min_gap = max(4.0, 0.3 * max(diffs))
    clusters, cur = [], [values[0]]
    for v in values[1:]:
        if v - cur[-1] > min_gap:
            clusters.append(cur)
            cur = []
        cur.append(v)
    clusters.append(cur)
    return [sum(c) / len(c) for c in clusters]


def parse_tree_text(text: str, keep_suffix=False, warn=print):
    lines = text.splitlines()
    flat_list = not any(find_node_marker(line)[0] is not None for line in lines)
    candidates = []
    warnings = []
    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        idx, mlen = find_node_marker(line)
        if idx is not None:
            raw = line[idx + mlen :].lstrip("\u2500 ")
            if raw.strip():
                candidates.append((line_no, idx, raw))
            continue
        if FOOTER_RE.fullmatch(stripped):
            continue
        if all(c in JUNK_LINE_CHARS for c in stripped):
            continue
        if flat_list:
            candidates.append((line_no, None, stripped))
        elif not candidates:
            candidates.append((line_no, None, stripped))
        else:
            warnings.append(f"line {line_no}: ignoring unrecognized line {stripped!r}")
    marker_idxs = sorted({idx for _, idx, _ in candidates if idx is not None})
    if marker_idxs:
        col_to_depth = {c: k + 1 for k, c in enumerate(marker_idxs)}
    else:
        col_to_depth = None
    entries = []
    for line_no, idx, raw in candidates:
        name, explicit = clean_name(raw, keep_suffix=keep_suffix)
        if name is None:
            continue
        if idx is None:
            depth = 1 if flat_list else 0
        elif col_to_depth is not None:
            depth = col_to_depth[idx]
        else:
            depth = 1
        entries.append({
            "depth": depth,
            "name": name,
            "is_dir": explicit,
            "explicit_dir": explicit,
            "line_no": line_no,
        })
    for w in warnings:
        warn("warning: " + w)
    return entries


def load_ocr(choice: str):
    if choice in ("auto", "rapidocr"):
        try:
            from rapidocr_onnxruntime import RapidOCR

            engine = RapidOCR()

            def run(img: Any):
                import numpy as np

                tokens = []
                res, _ = engine(np.array(img))
                for box, text, score in res or []:
                    try:
                        conf = float(score)
                    except (TypeError, ValueError):
                        conf = 0.0
                    xs = [float(p[0]) for p in box]
                    ys = [float(p[1]) for p in box]
                    tokens.append((min(xs), min(ys), max(xs) - min(xs), str(text).strip(), conf))
                return tokens

            return "rapidocr", run
        except ImportError:
            if choice == "rapidocr":
                sys.exit("error: rapidocr-onnxruntime is not installed (pip install rapidocr-onnxruntime)")
    if choice in ("auto", "pytesseract"):
        try:
            import pytesseract

            def run(img: Any):
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 6")
                tokens = []
                for i, t in enumerate(data["text"]):
                    t = (t or "").strip()
                    if not t:
                        continue
                    try:
                        conf = float(data["conf"][i])
                    except (TypeError, ValueError):
                        conf = 0.0
                    if conf < 40:
                        continue
                    tokens.append((
                        int(data["left"][i]),
                        int(data["top"][i]),
                        int(data["width"][i]),
                        t,
                        conf,
                    ))
                return tokens

            return "pytesseract", run
        except ImportError:
            if choice == "pytesseract":
                sys.exit("error: pytesseract is not installed (pip install pytesseract)")
    sys.exit(
        "error: no OCR engine available — install one with:\n"
        "    pip install rapidocr-onnxruntime     (recommended, self-contained)\n"
        "  or: pip install pytesseract            (requires the tesseract binary too)"
    )


def detect_marker_xs(img: Any, rows: Iterable[Any], pitch: float, char_width: int):
    width, height = img.size
    px = img.load()
    out = []
    for top, parts in rows:
        left = parts[0][0]
        y0 = max(0, int(top - 0.35 * pitch))
        y1 = min(height, int(top + 0.55 * pitch))
        dark = []
        for x in range(width):
            is_dark = False
            for y in range(y0, y1):
                if px[x, y] < 180:
                    is_dark = True
                    break
            dark.append(is_dark)
        runs, i = [], 0
        while i < width:
            if dark[i]:
                j = i + 1
                while j < width and dark[j]:
                    j += 1
                runs.append((i, j))
                i = j
            else:
                i += 1
        cand = [s for s, e in runs if 1.2 * char_width <= (e - s) <= 6 * char_width and s < left - 0.2 * char_width]
        out.append(min(cand) if cand else None)
    return out


def image_to_entries(path: Path, keep_suffix=False, engine="auto"):
    from PIL import Image, ImageOps, ImageStat

    try:
        img = Image.open(path)
    except Exception as e:
        sys.exit(f"error: cannot open image {path}: {e}")
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    if ImageStat.Stat(img).mean[0] < 128:
        img = ImageOps.invert(img)
    img = ImageOps.autocontrast(img)
    small = min(img.size)
    if small < 800:
        factor = min(800 / small, 4.0)
        img = img.resize((round(img.width * factor), round(img.height * factor)), Image.LANCZOS)
    engine_name, ocr = load_ocr(engine)
    tokens = ocr(img)
    raw_count = len(tokens)
    kept = []
    for left, top, width, text, conf in tokens:
        if conf < 0.4:
            continue
        if JUNK_TOKEN_RE.fullmatch(text):
            continue
        if FOOTER_RE.fullmatch(text):
            continue
        if text.startswith("#"):
            continue
        left, text = strip_leading_junk(left, text)
        if not text.strip():
            continue
        kept.append((left, top, width, text.strip()))
    tokens = kept
    if not tokens:
        sys.exit(f"error: OCR found no readable entries in {path}")
    cell_widths = [w / max(1, len(t)) for _l, _t, w, t, *_ in tokens if len(t) >= 2 and w > 0]
    tokens.sort(key=lambda t: (t[1], t[0]))
    tops = [t[1] for t in tokens]
    top_diffs = [b - a for a, b in itertools.pairwise(tops) if b - a > 0]
    pitch = median(top_diffs) if top_diffs else 40.0
    row_thresh = max(6.0, 0.45 * pitch)
    rows = []
    for left, top, _w, text in tokens:
        if rows and top - rows[-1][0] <= row_thresh:
            rows[-1][1].append((left, text))
        else:
            rows.append((top, [(left, text)]))
    row_lefts, row_names = [], []
    for top, parts in rows:
        parts.sort(key=lambda p: p[0])
        row_lefts.append(parts[0][0])
        row_names.append(re.sub(r"\s*\.\s*", ".", "".join(p[1] for p in parts)))
    if not cell_widths:
        cell_widths = [pitch / 2.2]
    char_width = median(cell_widths)
    marker_xs = detect_marker_xs(img, rows, pitch, char_width)
    col_gap = 0.8 * char_width
    ocr_means = cluster_means(row_lefts, min_gap=col_gap)
    ocr_depth = {m: k + 1 for k, m in enumerate(ocr_means)}
    marker_means = cluster_means([m for m in marker_xs if m is not None], min_gap=col_gap)
    marker_depth = {m: k + 1 for k, m in enumerate(marker_means)}

    def depth_of(i: Any):
        m = marker_xs[i]
        if m is not None and marker_means:
            nearest = min(marker_means, key=lambda x: abs(x - m))
            return marker_depth[nearest]
        nearest = min(ocr_means, key=lambda x: abs(x - row_lefts[i]))
        return ocr_depth[nearest]

    entries = []
    for i, (top, left, raw) in enumerate(zip((r[0] for r in rows), row_lefts, row_names, strict=False)):
        name, explicit = clean_name(raw, keep_suffix=keep_suffix)
        if name is None:
            continue
        entries.append({
            "depth": depth_of(i),
            "name": name,
            "is_dir": explicit,
            "explicit_dir": explicit,
            "line_no": round(top),
        })
    if not entries:
        sys.exit(f"error: OCR found no usable entries in {path}")
    return engine_name, entries, {"tokens": raw_count, "rows": len(rows)}


def finalize(entries: list[Any], assume_dir=False) -> None:
    for i, e in enumerate(entries):
        if e["name"] == ".":
            e["is_dir"] = True
            continue
        if e["explicit_dir"]:
            e["is_dir"] = True
            continue
        has_children = i + 1 < len(entries) and entries[i + 1]["depth"] > e["depth"]
        if has_children or assume_dir:
            e["is_dir"] = True
        else:
            e["is_dir"] = False


def ambiguous_notes(entries: list[Any], assume_dir=False):
    notes = []
    ambiguous = [
        e["name"]
        for i, e in enumerate(entries)
        if e["name"] != "."
        and not e["explicit_dir"]
        and not (i + 1 < len(entries) and entries[i + 1]["depth"] > e["depth"])
    ]
    if ambiguous:
        n = len(ambiguous)
        noun = "entry" if n == 1 else "entries"
        if assume_dir:
            notes.append(
                f"{n} {noun} had no visible children -> created as director{'y' if n == 1 else 'ies'} (--assume-dir)"
            )
        else:
            notes.append(
                f"{n} {noun} had no visible children -> created as "
                f"file{'s' if n != 1 else ''}; re-run with --assume-dir "
                "to make them directories"
            )
    return notes


def _unsafe_name(name: str) -> bool:
    p = Path(name)
    return not name or "\\" in name or p.is_absolute() or any(part in ("", ".", "..") for part in p.parts)


def create_tree(entries: list[Any], base_dir: Path, dry_run=False) -> dict[str, int]:
    base_dir = base_dir.resolve()
    if not dry_run:
        base_dir.mkdir(parents=True, exist_ok=True)
    stack = []
    counts = {"dirs": 0, "files": 0, "existing": 0, "skipped": 0}
    for entry in entries:
        depth = entry["depth"]
        name = entry["name"]
        is_dir = entry["is_dir"]
        prefix = "DRYRUN " if dry_run else ""
        if name == ".":
            if not stack:
                stack.append((0, base_dir))
            continue
        if _unsafe_name(name):
            print(f"{prefix}SKIP  unsafe path: {name!r}", file=sys.stderr)
            counts["skipped"] += 1
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else base_dir
        target = parent / name
        if is_dir:
            try:
                already = not dry_run and target.exists()
                if not dry_run and not already:
                    target.mkdir(parents=True, exist_ok=True)
                rel = target.relative_to(base_dir) if target != base_dir else Path()
                if already:
                    print(f"EXIST {rel}/")
                    counts["existing"] += 1
                else:
                    print(f"{prefix}DIR   {rel}/")
                    counts["dirs"] += 1
            except Exception as e:
                print(f"Failed to create dir {target}: {e}", file=sys.stderr)
                counts["skipped"] += 1
                continue
            stack.append((depth, target))
        else:
            try:
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                if not dry_run and target.exists():
                    print(f"EXIST {target.relative_to(base_dir)}")
                    counts["existing"] += 1
                else:
                    if not dry_run:
                        target.touch()
                    print(f"{prefix}FILE  {target.relative_to(base_dir)}")
                    counts["files"] += 1
            except Exception as e:
                print(f"Failed to create file {target}: {e}", file=sys.stderr)
                counts["skipped"] += 1
    return counts


def detect_mode(path: Path) -> str:
    if path.suffix.lower() in IMG_EXTS:
        return "image"
    try:
        head = path.open("rb").read(16)
        magics = (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF8",
            b"RIFF",
            b"BM",
            b"II*\x00",
            b"MM\x00*",
        )
        if any(head.startswith(m) for m in magics):
            return "image"
    except OSError:
        pass
    return "text"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tree2fs",
        description="Recreate a directory tree from a `tree` listing (text) or a screenshot of one (OCR).",
        epilog=(
            "examples:\n"
            "  tree2fs tree.txt                     # build from a text listing\n"
            "  tree2fs screenshot.png               # build from a screenshot (OCR)\n"
            "  tree2fs tree.txt --base /tmp/out     # build elsewhere\n"
            "  tree2fs tree.txt -d -n               # ambiguous entries as dirs, dry run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="text listing or screenshot image")
    parser.add_argument(
        "--base",
        type=Path,
        default=Path.cwd(),
        help="directory to build the tree in (default: current directory)",
    )
    parser.add_argument(
        "-d",
        "--assume-dir",
        action="store_true",
        help="entries without visible children are created as directories (default: files)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the plan without creating anything",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "rapidocr", "pytesseract"),
        default="auto",
        help="OCR engine (default: auto)",
    )
    parser.add_argument(
        "--keep-suffix",
        action="store_true",
        help="keep tree -F suffixes (* @ = |) in names",
    )
    args = parser.parse_args()
    input_path = args.input
    if not input_path.is_file():
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    if detect_mode(input_path) == "image":
        engine_name, entries, stats = image_to_entries(input_path, keep_suffix=args.keep_suffix, engine=args.engine)
        print(f"OCR ({engine_name}): {stats['rows']} rows from {stats['tokens']} raw tokens")
    else:
        try:
            text = input_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"error: cannot read {input_path}: {e}", file=sys.stderr)
            sys.exit(1)
        entries = parse_tree_text(text, keep_suffix=args.keep_suffix)
        print(f"Text: parsed {len(entries)} entries")
    if not entries:
        print("error: no entries found in input.", file=sys.stderr)
        sys.exit(1)
    finalize(entries, assume_dir=args.assume_dir)
    for note in ambiguous_notes(entries, assume_dir=args.assume_dir):
        print("note: " + note)
    counts = create_tree(entries, args.base, dry_run=args.dry_run)
    what = "Would create" if args.dry_run else "Created"
    print(
        f"{what} {counts['dirs']} dir(s), {counts['files']} file(s)"
        f" ({counts['existing']} already existed, {counts['skipped']} skipped)."
    )


if __name__ == "__main__":
    main()
