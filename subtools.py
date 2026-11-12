#!/data/data/com.termux/files/usr/bin/env python
"""Unified subtitle toolkit: rename, folderize, convert and translate subtitle files."""

import argparse
from collections import Counter
import multiprocessing as mp
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Optional

from dh import MAX_WORKERS

_ANSI: dict[str, str] = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def colored(text: str, color: Optional[str] = None, attrs: Optional[Iterable[str]] = None) -> str:
    prefix = ""
    if color:
        prefix += _ANSI.get(color, "")
    if attrs:
        for a in attrs:
            prefix += _ANSI.get(a, "")
    if not prefix:
        return text
    return f"{prefix}{text}{_ANSI['reset']}"


SRT_EXT: set[str] = {".srt"}
TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}")
TAG_RE = re.compile(r"<[^>]+>|{\w+}")


def iter_srt(directory: Path, recursive: bool = False) -> list[Path]:
    if recursive:
        return [p for p in directory.rglob("*") if p.suffix.lower() in SRT_EXT]
    return [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in SRT_EXT]


def read_srt_dialogue(path: Path) -> tuple[list[str], int]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.readlines()
    lines: list[str] = []
    skipped = 0
    for line in raw:
        s = line.strip()
        if not s or "-->" in s or s.isdigit():
            skipped += 1
            continue
        lines.append(s)
    return lines, skipped


_EPISODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"S\d{2}E(\d{2})", re.IGNORECASE),
    re.compile(r"(\d{1,2})x(\d{2})", re.IGNORECASE),
]
_PREFIX_RE = re.compile(r"^\s*[\d\s\.-]{6,}", re.IGNORECASE)


def _extract_episode(name: str) -> Optional[str]:
    for pattern in _EPISODE_PATTERNS:
        m = pattern.search(name)
        if m:
            return m.group(m.lastindex)
    return None


def _new_subtitle_name(name: str) -> Optional[str]:
    stripped = _PREFIX_RE.sub("", name)
    ep = _extract_episode(stripped)
    if not ep:
        return None
    return f"E{ep.zfill(2)}"


def cmd_rename(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    files = iter_srt(root, args.recursive)
    if not files:
        print("No subtitle files found")
        return 0
    print(colored("\nPreview:", "cyan", attrs=["bold"]))
    for f in files:
        new_stem = _new_subtitle_name(f.name)
        if not new_stem:
            print(colored("SKIP:", "yellow"), f.name)
            continue
        new_name = new_stem + f.suffix
        target = f.with_name(new_name)
        print(colored("OLD:", "red"), f.name, colored("->NEW:", "green"), new_name)
        if args.write:
            if target.exists():
                print(colored("  EXISTS,skipped", "yellow"))
            else:
                f.rename(target)
    if not args.write:
        print(colored("\nDry-run only. Use -w to apply.", "yellow"))
    return 0


_LINGUA_DETECTOR = None


def _lingua_init() -> None:
    from lingua import LanguageDetectorBuilder

    global _LINGUA_DETECTOR
    _LINGUA_DETECTOR = LanguageDetectorBuilder.from_all_languages().build()


def _lingua_detect_batch(texts: list[str]) -> list[tuple[str, Optional[str], Optional[str]]]:
    out: list[tuple[str, Optional[str], Optional[str]]] = []
    for text in texts:
        try:
            lang = _LINGUA_DETECTOR.detect_language_of(text)
            if lang:
                out.append((text, lang.name, lang.iso_code_639_1.name))
            else:
                out.append((text, None, None))
        except Exception:
            out.append((text, None, None))
    return out


def _folderize_lingua(path: Path, pool: Any, batch_size: int) -> Optional[tuple[str, str]]:
    try:
        lines, skipped = read_srt_dialogue(path)
        if not lines:
            return None
        batches = [lines[i : i + batch_size] for i in range(0, len(lines), batch_size)]
        results = pool.map(_lingua_detect_batch, batches)
        total = 0
        votes: Counter = Counter()
        iso_map: dict[str, str] = {}
        for batch in results:
            for text, name, iso in batch:
                total += 1
                if name:
                    votes[name] += 1
                    iso_map[name] = iso
                    preview = text[:50] + ("..." if len(text) > 50 else "")
                    print(f'      Line {total}: {name} - "{preview}"')
        if votes:
            top, _ = votes.most_common(1)[0]
            iso = iso_map[top]
            print("\n   📊 Statistics:")
            print(f"      Total lines processed: {total}")
            print(f"      Lines skipped: {skipped}")
            print("      Language votes:")
            for lang, count in votes.most_common():
                pct = count / total * 100
                print(f"         {lang}: {count} votes ({pct:.1f}%)")
            return top, iso
    except Exception as e:
        print(f"  ⚠ Error reading {path.name}: {e}")
    return None


def _folderize_cld2(path: Path) -> Optional[tuple[str, str]]:
    import pycld2 as cld2

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")
        clean = "\n".join(line for line in lines if line.strip() and not line.isdigit() and "-->" not in line)
        if not clean.strip():
            return None
        _, _, details = cld2.detect(clean)
        if not details or not details[0]:
            return None
        lang, code, _, _ = details[0]
        if code == "un" or lang.lower() == "unknown":
            return None
        return lang, code
    except Exception as e:
        print(f"  ⚠ Error reading {path.name}: {e}")
    return None


def cmd_folderize(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    print(f"🔍 Scanning directory: {directory.absolute()}\n")
    files = iter_srt(directory)
    if not files:
        print("❌ No .srt files found in the directory.")
        return 0
    print(f"📊 Found {len(files)} SRT file(s)")
    buckets: dict[str, list[Path]] = {}
    if args.engine == "lingua":
        workers = MAX_WORKERS or os.cpu_count() or 1
        print(f"⚙️  Using {workers} worker processes (batch size={args.batch_size})\n")
        print("=" * 80)
        with mp.Pool(initializer=_lingua_init, processes=workers) as pool:
            for path in files:
                print(f"\n📄 Processing: {path.name}")
                print("─" * 80)
                result = _folderize_lingua(path, pool, args.batch_size)
                if result:
                    lang, iso = result
                    print(f"\n   ✅ FINAL RESULT: {lang} ({iso})")
                    buckets.setdefault(f"{lang}_{iso}".lower(), []).append(path)
                else:
                    print("\n   ⚠ Could not detect language")
                print("=" * 80)
    else:
        print("=" * 70)
        for path in files:
            print(f"\n📄 Processing: {path.name}")
            result = _folderize_cld2(path)
            if result:
                lang, code = result
                print(f"   ✓ Detected language: {lang} ({code})")
                buckets.setdefault(f"{lang}_{code}".lower(), []).append(path)
            else:
                print("   ⚠ Could not detect language")
        print("\n" + "=" * 70)
    print("\n📁 Creating folders and moving files...\n")
    moved = 0
    for key, paths in sorted(buckets.items()):
        target_dir = directory / key
        target_dir.mkdir(exist_ok=True)
        print(f"📂 {key.upper()} ({len(paths)} file(s))")
        for p in paths:
            new_path = target_dir / p.name
            p.rename(new_path)
            print(f"   ➜ {p.name}")
            moved += 1
    print(f"\n✅ Complete! Moved {moved} file(s) into {len(buckets)} language folder(s).")
    return 0


def _srt_to_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if TIMESTAMP_RE.match(line):
            continue
        line = TAG_RE.sub("", line)
        out.append(line)
    return "\n".join(out)


def cmd_srt2txt(args: argparse.Namespace) -> int:
    src = Path(args.input)
    dst = Path(args.output) if args.output else src.with_suffix(".txt")
    dst.write_text(_srt_to_text(src), encoding="utf-8")
    print(f"✔ Converted: {src} → {dst}")
    return 0


def _vtt_to_srt(content: str) -> str:
    lines = content.splitlines()
    out: list[str] = []
    i = 1 if lines and lines[0].strip() == "WEBVTT" else 0
    index = 1
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if "-->" in line:
            ts = line.replace(".", ",")
            out.append(str(index))
            out.append(ts)
            index += 1
            i += 1
            while i < len(lines) and lines[i].strip():
                out.append(lines[i])
                i += 1
            out.append("")
        else:
            i += 1
    return "\n".join(out)


def cmd_vtt2srt(args: argparse.Namespace) -> int:
    src = Path(args.input)
    dst = Path(args.output) if args.output else src.with_suffix(".srt")
    content = src.read_text(encoding="utf-8")
    dst.write_text(_vtt_to_srt(content), encoding="utf-8")
    print("File saved.")
    return 0


def _translate_srt(input_path: Path, source: str, target: str, max_batch_chars: int, delay: float) -> None:
    from deep_translator import GoogleTranslator
    import pysrt

    subs = pysrt.open(str(input_path), encoding="utf-8")
    translator = GoogleTranslator(source=source, target=target)
    batches: list[list] = []
    current: list = []
    size = 0
    print("Preparing translation batches...")
    for sub in subs:
        if size + len(sub.text) > max_batch_chars and current:
            batches.append(current)
            current, size = [], 0
        current.append(sub)
        size += len(sub.text)
    if current:
        batches.append(current)
    print(f"Translating {len(batches)} batches...")
    for i, batch in enumerate(batches):
        try:
            joined = "|||".join(s.text for s in batch)
            translated = translator.translate(joined)
            parts = translated.split("|||")
            for j, sub in enumerate(batch):
                sub.text = parts[j] if j < len(parts) else sub.text
            print(f"Processed batch {i + 1}/{len(batches)}")
            time.sleep(delay)
        except Exception as e:
            print(f"Error in batch {i}: {e}")
    out = f"translated_{input_path.name}"
    subs.save(out, encoding="utf-8")
    print(f"Translation complete! Saved to {out}")


def cmd_translate(args: argparse.Namespace) -> int:
    _translate_srt(Path(args.input), args.source, args.target, args.max_batch_chars, args.delay)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified subtitle toolkit (rename / folderize / convert / translate).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_rename = sub.add_parser("rename", help="Rename SRT files to E##.")
    p_rename.add_argument("directory", nargs="?", default=".")
    p_rename.add_argument("-r", "--recursive", action="store_true")
    p_rename.add_argument("-w", "--write", action="store_true")
    p_rename.set_defaults(func=cmd_rename)
    p_folder = sub.add_parser("folderize", help="Group SRT files by detected language.")
    p_folder.add_argument("directory", nargs="?", default=".")
    p_folder.add_argument("--engine", choices=["lingua", "cld2"], default="lingua")
    p_folder.add_argument(
        "-b", "--batch-size", type=int, default=8, help="(lingua only) lines per detection batch (default: 8)"
    )
    p_folder.set_defaults(func=cmd_folderize)
    p_s2t = sub.add_parser("srt2txt", help="Convert .srt to plain text.")
    p_s2t.add_argument("input")
    p_s2t.add_argument("output", nargs="?")
    p_s2t.set_defaults(func=cmd_srt2txt)
    p_v2s = sub.add_parser("vtt2srt", help="Convert .vtt to .srt.")
    p_v2s.add_argument("input")
    p_v2s.add_argument("output", nargs="?")
    p_v2s.set_defaults(func=cmd_vtt2srt)
    p_tr = sub.add_parser("translate", help="Translate an SRT file.")
    p_tr.add_argument("-i", "--input", required=True)
    p_tr.add_argument("-s", "--source", required=True)
    p_tr.add_argument("-t", "--target", required=True)
    p_tr.add_argument(
        "--max-batch-chars", type=int, default=2000, help="max characters per translation batch (default: 2000)"
    )
    p_tr.add_argument("--delay", type=float, default=1.0, help="seconds to sleep between batches (default: 1.0)")
    p_tr.set_defaults(func=cmd_translate)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
