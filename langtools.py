#!/data/data/com.termux/files/usr/bin/env python
"""Language-detection and filtering toolkit.
Subcommands: ``move-chinese`` moves the files containing Chinese characters, ``filter-lines`` strips the non-English lines out of one file, ``find-files`` reports the non-English files with pycld2 and ``find-lines`` collects the non-English lines of a tree into a TSV (the strict mode adds gcld3 plus an NLTK word ratio).
Usage: langtools {move-chinese,filter-lines,find-files,find-lines} [options] Positional arguments: directory Directory to scan (default: current dir).
file File to inspect line-by-line.
Subcommands: move-chinese Move files containing Chinese characters.
filter-lines Filter non-English lines out of one file.
find-files Recursively find non-English files (pycld2).
find-lines Find non-English lines across many files, save TSV.
Options: --target -m, --move Extract non-English lines to noneng.txt and edit source in place.
--strict Use gcld3 + NLTK word-ratio (strict).
-t, --threshold Strict mode: minimum ratio of NLTK English words (default 0.5).
-o, --out --min-bytes Minimum bytes to consider a file (default 100).
--max-bytes Maximum bytes to read from each file (default 10000).
-a, --all Report all files, including English ones.
-np, --no-progress Don't print per-file progress.
-o, --output Write the report to this file instead of stdout.
...
plus 9 more; run ``langtools --help`` for the full list.
Examples: $ langtools find-files docs/ $ langtools filter-lines -m page.txt Command-line script: run ``python langtools.py`` or the ``langtools`` command.
The module is importable as well."""

import argparse
from collections import Counter, defaultdict
import csv
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Iterator, Optional


try:
    import pycld2
except ImportError:
    pycld2 = None
try:
    import gcld3
except ImportError:
    gcld3 = None
CHINESE_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0x2CEB0, 0x2EBEF),
)
ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "gbk", "cp1252")
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".toml",
    ".py",
    ".pyi",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sh",
    ".bash",
    ".bat",
    ".ps1",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".pl",
    ".lua",
    ".sql",
}
DEFAULT_MOVE_TARGET = "chinese_files"
DEFAULT_NONENG_FILE = "noneng.txt"
PROGRESS = True


def contains_chinese(text: str) -> bool:
    for ch in text:
        d = ord(ch)
        for lo, hi in CHINESE_RANGES:
            if lo <= d <= hi:
                return True
    return False


def read_text_any_encoding(path: Path) -> str:
    for enc in ENCODINGS:
        try:
            return path.read_text(encoding=enc, errors="strict")
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def safe_text_from_bytes(data: bytes) -> str:
    for enc in ENCODINGS + ("latin-1",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}__{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


def is_probably_text_bytes(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    good = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return good / len(sample) > 0.7


def read_file_bytes(path: Path, max_bytes: Optional[int] = None) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(max_bytes) if max_bytes else fh.read()


def iter_files(
    root: Path,
    *,
    recursive: bool = True,
    exts: Optional[set[str]] = None,
    skip_hidden: bool = True,
) -> Iterator[Path]:
    if not recursive:
        for entry in root.iterdir():
            if skip_hidden and entry.name.startswith("."):
                continue
            if entry.is_file() and _matches_ext(entry, exts):
                yield entry
        return
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        base = Path(dirpath)
        for fn in filenames:
            if skip_hidden and fn.startswith("."):
                continue
            p = base / fn
            if _matches_ext(p, exts):
                yield p


def _matches_ext(p: Path, exts: Optional[set[str]]) -> bool:
    if not exts:
        return True
    return p.suffix.lower().lstrip(".") in exts


def _require_pycld2():
    if pycld2 is None:
        print(
            "Error: pycld2 is not installed. Install with:\n"
            "  pip install pycld2\n"
            "On Termux you might need:  pkg install clang && pip install pycld2",
            file=sys.stderr,
        )
        sys.exit(1)
    return pycld2


def _require_gcld3():
    if gcld3 is None:
        print(
            "Error: gcld3 is not installed. Install with: pip install gcld3",
            file=sys.stderr,
        )
        sys.exit(1)
    return gcld3


def detect_with_pycld2(text: str, min_bytes: int = 20):
    if not text or len(text) < min_bytes:
        return (None, None, 0)
    try:
        _ok, _details, details = _require_pycld2().detect(text)
        if details:
            name, code, conf, _ = details[0]
            return (name, code, conf)
    except Exception:
        pass
    return (None, None, 0)


def cmd_move_chinese(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    target_dir = (root / args.target).resolve()
    target_dir.mkdir(exist_ok=True)
    print(f"📂 Source: {root}\n📁 Target: {target_dir}")
    moved = 0
    for f in root.iterdir():
        if not f.is_file():
            continue
        if f.resolve().is_relative_to(target_dir):
            continue
        try:
            text = read_text_any_encoding(f)
        except Exception as e:
            print(f"Skipped (read error): {f.name} ({e})")
            continue
        if contains_chinese(text):
            dest = unique_path(target_dir, f.name)
            shutil.move(str(f), str(dest))
            print(f"Moved: {dest.relative_to(root)}")
            moved += 1
    print(f'\n✅ Moved {moved} file(s) into "{target_dir.name}/".')
    return 0


def _write_extraction(src: Path, kept: list[str], removed: list[str], out_name: str = DEFAULT_NONENG_FILE) -> None:
    if not removed:
        print("ℹ️  No non-English lines found to extract. Base file left unchanged.")
        return
    out = Path(out_name)
    out.write_text("\n".join(removed) + "\n", encoding="utf-8")
    print(f"💾 Extracted lines written safely to: {out.resolve()}")
    src.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print("🔄 Original file updated in-place (non-English elements removed).")


def cmd_filter_lines(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"❌ Error: The file '{path}' does not exist or is not a file.")
        return 1
    if args.strict:
        return _filter_strict(path, args.move, args.threshold, args.out)
    return _filter_simple(path, args.move, args.out)


def _filter_simple(path: Path, move: bool, out_name: str) -> int:
    gcld3_mod = _require_gcld3()
    ident = gcld3_mod.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=1000)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return 1
    kept, removed = [], []
    print(f"🔍 Analyzing {len(lines)} lines from '{path.name}'...")
    print("-" * 40)
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            kept.append(raw)
            continue
        res = ident.FindLanguage(stripped)
        if res.language == "en" and res.is_reliable:
            kept.append(raw)
        else:
            removed.append(raw)
            tag = f"[{res.language.upper()} (Prob: {res.probability:.2f})]" if res.language != "und" else "[UNKNOWN]"
            print(f"Line {i} {tag}: {stripped}")
    print("-" * 40)
    print(f"📊 Summary: Found {len(removed)} non-English lines.")
    if move:
        _write_extraction(path, kept, removed, out_name)
    return 0


def _filter_strict(path: Path, move: bool, threshold: float, out_name: str) -> int:
    gcld3_mod = _require_gcld3()
    try:
        from nltk.corpus import words as nltk_words
    except ImportError:
        print(
            "Error: nltk is required for --strict. Install with:\n"
            "  pip install nltk && python -m nltk.downloader words",
            file=sys.stderr,
        )
        return 1
    ident = gcld3_mod.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=1000)
    print("🧠 Loading NLTK English vocabulary corpus...")
    vocab = {w.lower() for w in nltk_words.words()}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return 1
    kept, removed = [], []
    print(f"🔍 Strictly scanning {len(lines)} lines from '{path.name}'...")
    print("-" * 40)
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            kept.append(raw)
            continue
        res = ident.FindLanguage(stripped)
        cld_en = res.language == "en" and res.is_reliable
        tokens = re.findall(r"\b[a-zA-Z]+\b", stripped.lower())
        if not tokens:
            ok = cld_en
            tag = f"CLD3:{res.language.upper()}(No Words)"
        else:
            ratio = sum(1 for t in tokens if t in vocab) / len(tokens)
            if cld_en and ratio >= threshold:
                kept.append(raw)
                continue
            ok = False
            tag = f"NLTK Ratio: {ratio:.2f}"
            if res.language != "en":
                tag += f" | CLD3: {res.language.upper()}"
        if ok:
            kept.append(raw)
        else:
            removed.append(raw)
            print(f"Line {i} [{tag}]: {stripped}")
    print("-" * 40)
    print(f"📊 Strict Filter Summary: Identified {len(removed)} non-English lines.")
    if move:
        _write_extraction(path, kept, removed, out_name)
    return 0


def cmd_find_files(args: argparse.Namespace) -> int:
    _require_pycld2()
    root = Path(args.directory).resolve()
    if not root.exists():
        print(f"Error: Directory '{root}' does not exist")
        return 1
    if args.detailed:
        stats = _scan_detailed(root, args.min_bytes, args.max_bytes)
        _report_detailed(stats, show_files=args.verbose or args.list_languages)
    else:
        stats = _scan_simple(root, args.min_bytes, args.max_bytes, show_progress=not args.no_progress)
        _report_simple(stats, only_non_english=not args.all)
    return 0


def _scan_simple(root: Path, min_bytes: int, max_bytes: int, show_progress: bool) -> dict:
    stats = {
        "total_files": 0,
        "skipped_binary": 0,
        "skipped_small": 0,
        "skipped_error": 0,
        "non_english": [],
        "languages": Counter(),
    }
    print(f"🔍 Scanning directory: {root}")
    print("-" * 40)
    for f in iter_files(root, recursive=True, skip_hidden=True):
        stats["total_files"] += 1
        if show_progress:
            print(f"\n{f} [Files: {stats['total_files']}]", end="", flush=True)
        try:
            data = read_file_bytes(f, max_bytes)
        except Exception:
            stats["skipped_binary"] += 1
            continue
        if not is_probably_text_bytes(data):
            stats["skipped_binary"] += 1
            continue
        text = safe_text_from_bytes(data)
        if len(text) < min_bytes:
            stats["skipped_small"] += 1
            continue
        name, code, conf = detect_with_pycld2(text, min_bytes=1)
        if code is None:
            stats["skipped_error"] += 1
            continue
        stats["languages"][name] += 1
        if code != "en":
            stats["non_english"].append({
                "file": f,
                "language": name,
                "code": code,
                "reliable": conf >= 70,
                "confidence": conf,
            })
    if show_progress:
        print()
    return stats


def _report_simple(stats: dict, only_non_english: bool) -> None:
    print("\n" + "=" * 40)
    print("\n📊 SCAN RESULTS")
    print("-" * 40)
    print(f"📁 Total files processed: {stats['total_files']}")
    print(f"⏭️  Skipped binary files: {stats['skipped_binary']}")
    print(f"📏 Skipped small files: {stats['skipped_small']}")
    print(f"❌ Skipped (errors): {stats['skipped_error']}")
    if only_non_english:
        print(f"🌍 Non-English files found: {len(stats['non_english'])}")
    else:
        print(f"🌍 Total text files analyzed: {sum(stats['languages'].values())}")
    if stats["languages"]:
        print("\n📈 Language Distribution:")
        for lang, n in stats["languages"].most_common():
            print(f"  • {lang}: {n} files")
    if stats["non_english"]:
        print(f"\n📝 Non-English Files ({len(stats['non_english'])}):")
        print("-" * 40)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in stats["non_english"]:
            grouped[row["language"]].append(row)
        for lang, rows in sorted(grouped.items()):
            print(f"\n  [{lang}] - {len(rows)} files:")
            for row in rows[:10]:
                mark = "✓" if row["reliable"] else "?"
                conf = row["confidence"] or 0
                tag = f"[{mark} {conf}%]" if conf else "[?]"
                print(f"    {tag} {row['file']}")
            if len(rows) > 10:
                print(f"    ... and {len(rows) - 10} more")
    else:
        print("\n✅ No non-English files found!")


def _scan_detailed(root: Path, min_bytes: int, max_bytes: int) -> dict:
    stats = {
        "total_files": 0,
        "checked_files": 0,
        "skipped_small": 0,
        "skipped_binary": 0,
        "skipped_encoding": 0,
        "non_english": defaultdict(list),
        "english": [],
        "undetermined": [],
        "language_stats": Counter(),
        "directory_stats": defaultdict(lambda: {"total": 0, "non_english": 0}),
    }
    print(f"🔍 Scanning directory: {root}")
    print("-" * 40)
    for f in iter_files(root, recursive=True, skip_hidden=True):
        if f.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if f.stat().st_size > 1_048_576:
            stats["skipped_binary"] += 1
            continue
        stats["total_files"] += 1
        rel_dir = str(f.parent.relative_to(root))
        stats["directory_stats"][rel_dir]["total"] += 1
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = None
            for enc in ("latin-1", "cp1252", "iso-8859-1"):
                try:
                    text = f.read_text(encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
        except Exception:
            text = None
        if text is None:
            stats["skipped_encoding"] += 1
            continue
        head = "\n".join(text.split("\n")[:50])
        head = head[:5000]
        if len(head) < max(min_bytes, 20):
            stats["skipped_small"] += 1
            continue
        name, code, conf = detect_with_pycld2(head, min_bytes=1)
        if code is None:
            stats["undetermined"].append(f)
            continue
        stats["checked_files"] += 1
        stats["language_stats"][name] += 1
        if code in ("en", "en_US", "en_GB") and conf >= 70:
            stats["english"].append(f)
        else:
            stats["non_english"][name].append(f)
            stats["directory_stats"][rel_dir]["non_english"] += 1
    return stats


def _report_detailed(stats: dict, show_files: bool) -> None:
    print("\n" + "=" * 40)
    print("📊 LANGUAGE DETECTION RESULTS")
    print("-" * 40)
    total = stats["total_files"]
    checked = stats["checked_files"]
    non_en = sum(len(v) for v in stats["non_english"].values())
    eng = len(stats["english"])
    undet = len(stats["undetermined"])
    pct = (checked / total * 100) if total else 0
    print(f"\n📁 Files scanned: {total}")
    print(f"   ├─ Successfully analyzed: {checked} ({pct:.1f}%)")
    print(f"   ├─ Skipped (too small): {stats['skipped_small']}")
    print(f"   ├─ Skipped (binary/large): {stats['skipped_binary']}")
    print(f"   └─ Skipped (encoding issues): {stats['skipped_encoding']}")
    print("\n🌍 Language breakdown:")
    print(f"   ├─ 🇺🇸 English files: {eng}")
    for lang, files in sorted(stats["non_english"].items(), key=lambda x: len(x[1]), reverse=True):
        pct_l = (len(files) / checked * 100) if checked else 0
        print(f"   ├─ 🌐 {lang.upper()}: {len(files)} files ({pct_l:.1f}%)")
    if undet:
        print(f"   └─ ❓ Undetermined: {undet}")
    dirs = [(d, s) for d, s in stats["directory_stats"].items() if s["non_english"] > 0]
    if dirs:
        print("\n📂 Directories with most non-English files:")
        dirs.sort(key=lambda x: x[1]["non_english"], reverse=True)
        for d, s in dirs[:10]:
            pct_d = s["non_english"] / s["total"] * 100
            label = d if d != "." else "(root)"
            print(f"   ├─ {label}:")
            print(f"   │   {s['non_english']}/{s['total']} files ({pct_d:.1f}% non-English)")
    if show_files and stats["non_english"]:
        print("\n📄 Non-English files by language:")
        for lang, files in sorted(stats["non_english"].items()):
            if not files:
                continue
            print(f"\n   🌐 {lang.upper()} ({len(files)} files):")
            for f in files[:20]:
                print(f"      └─ {f}")
            if len(files) > 20:
                print(f"      └─ ... and {len(files) - 20} more")
    print("\n" + "=" * 40)
    print("🎯 RECOMMENDATION")
    print("-" * 40)
    if non_en == 0:
        print("✅ All files appear to be in English! No translation needed.")
    else:
        print(f"📢 Found {non_en} non-English files that may need translation.")


def cmd_find_lines(args: argparse.Namespace) -> int:
    _require_pycld2()
    root = Path(args.root)
    exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()} if args.ext else None
    out_path = Path(args.out)
    scanned = 0
    hits = 0
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["file", "line_no", "lang", "confidence", "text"])
        for fp in iter_files(
            root,
            recursive=not args.no_recursive,
            exts=exts,
            skip_hidden=not args.no_skip_hidden,
        ):
            try:
                data = read_file_bytes(fp, args.max_bytes)
            except Exception:
                continue
            if not is_probably_text_bytes(data):
                continue
            scanned += 1
            text = safe_text_from_bytes(data)
            for lineno, raw in enumerate(text.splitlines(), start=1):
                stripped = raw.strip()
                if len(stripped) < 3:
                    continue
                _name, code, conf = detect_with_pycld2(stripped, min_bytes=3)
                if code is None:
                    continue
                code = code.lower()
                score = conf / 100.0
                if code not in ("en", "und") and score >= args.min:
                    flat = raw.replace("\r", " ").replace("\n", " ").replace("\t", " ")
                    writer.writerow([str(fp), str(lineno), code, f"{score:.3f}", flat])
                    hits += 1
    print(f"Scanned files: {scanned}; non-English lines found: {hits}; results saved to {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="langtool.py",
        description="Unified language-detection / filtering toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    mc = sub.add_parser("move-chinese", help="Move files containing Chinese characters.")
    mc.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current dir).",
    )
    mc.add_argument(
        "--target",
        default=DEFAULT_MOVE_TARGET,
        help=f"Target folder name (default: {DEFAULT_MOVE_TARGET}).",
    )
    mc.set_defaults(func=cmd_move_chinese)
    fl = sub.add_parser("filter-lines", help="Filter non-English lines out of one file.")
    fl.add_argument("file", help="File to inspect line-by-line.")
    fl.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Extract non-English lines to noneng.txt and edit source in place.",
    )
    fl.add_argument("--strict", action="store_true", help="Use gcld3 + NLTK word-ratio (strict).")
    fl.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=0.5,
        help="Strict mode: minimum ratio of NLTK English words (default 0.5).",
    )
    fl.add_argument(
        "-o",
        "--out",
        default=DEFAULT_NONENG_FILE,
        help=f"Output filename for extracted lines (default {DEFAULT_NONENG_FILE}).",
    )
    fl.set_defaults(func=cmd_filter_lines)
    ff = sub.add_parser("find-files", help="Recursively find non-English files (pycld2).")
    ff.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current dir).",
    )
    ff.add_argument(
        "--min-bytes",
        type=int,
        default=100,
        help="Minimum bytes to consider a file (default 100).",
    )
    ff.add_argument(
        "--max-bytes",
        type=int,
        default=10_000,
        help="Maximum bytes to read from each file (default 10000).",
    )
    ff.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Report all files, including English ones.",
    )
    ff.add_argument(
        "-np",
        "--no-progress",
        action="store_true",
        help="Don't print per-file progress.",
    )
    ff.add_argument("-o", "--output", help="Write the report to this file instead of stdout.")
    ff.add_argument(
        "--detailed",
        action="store_true",
        help="Use the detailed (directory stats) report style.",
    )
    ff.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Detailed mode: show individual file paths.",
    )
    ff.add_argument(
        "-l",
        "--list-languages",
        action="store_true",
        help="Detailed mode: same as --verbose (list all detected files).",
    )
    ff.set_defaults(func=cmd_find_files)
    fnd = sub.add_parser("find-lines", help="Find non-English lines across many files, save TSV.")
    fnd.add_argument("-r", "--root", default=".", help="Root directory to scan (default: .).")
    fnd.add_argument(
        "-e",
        "--ext",
        default="",
        help="Comma-separated extensions to include (default: all text).",
    )
    fnd.add_argument(
        "-m",
        "--min",
        type=float,
        default=0.6,
        help="Minimum confidence to report 0..1 (default 0.6).",
    )
    fnd.add_argument(
        "-o",
        "--out",
        default=DEFAULT_NONENG_FILE,
        help=f"Output TSV filename (default {DEFAULT_NONENG_FILE}).",
    )
    fnd.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into subdirectories.",
    )
    fnd.add_argument(
        "--skip-hidden",
        dest="skip_hidden",
        action="store_true",
        default=True,
        help="Skip hidden files/dirs (default True).",
    )
    fnd.add_argument(
        "--no-skip-hidden",
        dest="skip_hidden",
        action="store_false",
        help="Include hidden files/dirs.",
    )
    fnd.add_argument(
        "--max-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="Maximum bytes read from each file (default 2 MiB).",
    )
    fnd.set_defaults(func=cmd_find_lines)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "find-lines":
        args.no_skip_hidden = not args.skip_hidden
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
