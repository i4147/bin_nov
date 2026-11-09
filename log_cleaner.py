#!/data/data/com.termux/files/usr/bin/env python
"""Clean terminal transcripts/logs and shell histories; trim audio tails.
Analysis / deduplication ------------------------ - ``cleaner.py`` strips real ANSI/control sequences from one transcript; ``cliner.py`` (mmap + pool) and ``cliner2.py`` (plain) strip *literal* ``^[[…m``-style garbage from every ``*.log`` - the two cliners are duplicates, now one ``logs`` command; the real-ANSI variant is ``transcript`` (both behaviours selected by command).
- ``cleanhistory.py`` (drop ``cd "`printf`` lines + set-dedup, order destroyed) and ``histdup.py`` (order-preserving dedup) -> one ``history`` command (order-preserving; original drop-pattern kept).
- ``mchist.py`` (Midnight Commander ``[cmdline]`` history appended to ``.bash_history``, Termux paths) -> ``mc-import``.
- ``last5minutes.py`` is misfiled here: it trims the tail of an audio file with moviepy (and used 230 s for "5 minutes").
It is ``trim-audio --minutes 5`` (300 s; adds an ffmpeg fallback).
Examples -------- python3 log_clean_tool.py transcript session.log python3 log_clean_tool.py logs ~/logs --dry-run python3 log_clean_tool.py history python3 log_clean_tool.py mc-import python3 log_clean_tool.py trim-audio lecture.mp3 --minutes 5"""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import re
import shutil
import subprocess
import sys


ANSI_RE: re.Pattern[str] = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|[()][AB012])")
CONTROL_RE: re.Pattern[str] = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")
LITERAL_PATTERNS: tuple[str, ...] = (
    r"\^\[",
    r"\[[\dA-Z;]+m",
    r"\[\d+[A-Z]",
    r"\[[\dA-Z;]+",
    r"\^M",
    r"\(B",
    r"\(0",
    r"\x1b\[[0-9;]*[A-Za-z]",
    r"\x1b\([0-9AB]",
    r"\r",
    r"\x0f",
    r"\x0e",
)
DEFAULT_DROP: str = 'cd "`printf'
MC_HISTORY: Path = Path.home() / ".local" / "share" / "mc" / "history"
BASH_HISTORY: Path = Path.home() / ".bash_history"


def clean_transcript_text(content: str) -> str:
    """Strip real ANSI escapes, CRs, backspaces and control chars."""
    content = ANSI_RE.sub("", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    while "\x08" in content:  # apply backspaces like a terminal would
        content = re.sub(r".\x08", "", content, count=0)
    content = CONTROL_RE.sub("", content.replace("\x00", ""))
    content = re.sub(r"\n{3,}", "\n\n", content)
    lines: list[str] = [line.rstrip() for line in content.splitlines()]
    return "\n".join(lines).rstrip("\n") + "\n"


def clean_literal_line(line: str) -> str:
    """Strip literal ``^[[…``-style escape *text* from one log line."""
    cleaned: str = line
    for pattern in LITERAL_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    return re.sub(r" {2,}", " ", cleaned)


def clean_log_file(job: tuple[Path, bool]) -> tuple[Path, bool]:
    """Literal-clean one .log file; return (path, changed)."""
    path, dry_run = job
    try:
        original: str = path.read_text(encoding="utf-8", errors="ignore")
        updated: str = "".join(clean_literal_line(ln) for ln in original.splitlines(keepends=True))
        if updated != original and not dry_run:
            path.write_text(updated, encoding="utf-8")
        return path, updated != original
    except OSError:
        return path, False


def cmd_transcript(args: argparse.Namespace) -> int:
    """Clean ANSI/control sequences from transcript file(s)."""
    for path in args.files:
        original: str = path.read_text(encoding="utf-8", errors="replace")
        cleaned: str = clean_transcript_text(original)
        if args.stdout:
            print(cleaned, end="")
        elif args.output is not None:
            args.output.write_text(cleaned, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            path.write_text(cleaned, encoding="utf-8")
            print(f"Cleaned: {path}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Literal-clean every ``*.log`` under a root."""
    files: list[Path] = sorted(args.root.rglob("*.log")) if args.root.is_dir() else [args.root]
    if not files:
        print("No .log files found.")
        return 0
    jobs: list[tuple[Path, bool]] = [(p, args.dry_run) for p in files]
    changed: int = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for path, did_change in pool.map(clean_log_file, jobs):
            changed += 1 if did_change else 0
            print(f"{'would clean' if args.dry_run else 'cleaned'}: {path}" if did_change else f"ok: {path}")
    print(f"Done: {changed}/{len(files)} file(s) changed.")
    return 0


def dedup_preserving(lines: Sequence[str]) -> list[str]:
    """Unique lines in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key: str = line.rstrip("\n")
        if key not in seen:
            seen.add(key)
            out.append(line if line.endswith("\n") else line + "\n")
    return out


def cmd_history(args: argparse.Namespace) -> int:
    """Deduplicate a history file, optionally dropping junk lines."""
    target: Path = args.file.expanduser()
    if not target.is_file():
        print(f"{target} does not exist.")
        return 0
    lines: list[str] = target.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    print(f"history length: {len(lines)}")
    drops: list[str] = list(args.drop) + ([] if args.keep_cd_printf else [DEFAULT_DROP])
    kept: list[str] = [ln for ln in lines if not any(token in ln for token in drops)]
    unique: list[str] = dedup_preserving(kept)
    removed: int = len(lines) - len(unique)
    if args.dry_run:
        print(f"Would remove {removed} line(s) ({len(lines) - len(kept)} junk, rest duplicates).")
        return 0
    if args.backup:
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
    target.write_text("".join(unique), encoding="utf-8")
    print(f"{removed} line(s) removed; {len(unique)} remain.")
    return 0


def cmd_mc_import(args: argparse.Namespace) -> int:
    """Append Midnight Commander's [cmdline] history to bash history."""
    if not args.source.is_file():
        print(f"MC history not found: {args.source}", file=sys.stderr)
        return 1
    capture: bool = False
    commands: list[str] = []
    for raw in args.source.read_text(encoding="utf-8", errors="ignore").splitlines():
        line: str = raw.strip()
        if line == "[cmdline]":
            capture = True
            continue
        if capture:
            if not line or line.startswith("["):
                break
            commands.append(line.split("=", 1)[-1].strip())
    unique: list[str] = dedup_preserving([cmd + "\n" for cmd in commands if cmd])
    existing: set[str] = set()
    if args.dest.is_file() and not args.allow_dupes:
        existing = {ln.rstrip("\n") for ln in args.dest.read_text(errors="ignore").splitlines()}
    fresh: list[str] = [ln for ln in unique if ln.rstrip("\n") not in existing]
    if args.dry_run:
        print("".join(fresh), end="")
        print(f"# would append {len(fresh)} command(s)", file=sys.stderr)
        return 0
    with args.dest.open("a", encoding="utf-8") as sink:
        sink.writelines(fresh)
    print(f"Appended {len(fresh)} command(s) to {args.dest}")
    return 0


def cmd_trim_audio(args: argparse.Namespace) -> int:
    """Keep only the last N minutes of an audio file."""
    output: Path = args.output or Path(f"last_{args.minutes:g}_minutes.mp3")
    seconds: float = args.minutes * 60
    ffmpeg: str | None = shutil.which("ffmpeg")
    ffprobe: str | None = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(args.input),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration: float = float(probe.stdout.strip())
        start: float = max(0.0, duration - seconds)
        subprocess.run(
            [ffmpeg, "-y", "-ss", str(start), "-i", str(args.input), "-b:a", args.bitrate, str(output)],
            check=True,
            capture_output=True,
        )
        print(f"Wrote {output} (last {args.minutes:g} min of {duration / 60:.1f} min)")
        return 0
    try:
        from moviepy import AudioFileClip
    except ImportError:
        print("Error: need ffmpeg/ffprobe or moviepy (pip install moviepy).", file=sys.stderr)
        return 1
    clip: object = AudioFileClip(str(args.input))
    start_at: float = max(0.0, float(clip.duration) - seconds)  # type: ignore[attr-defined]
    tail: object = clip.subclipped(start_at, clip.duration)  # type: ignore[attr-defined]
    tail.write_audiofile(str(output), bitrate=args.bitrate, fps=44100)  # type: ignore[attr-defined]
    print(f"Wrote {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Transcript/log/history cleaning.")
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("transcript", help="Strip real ANSI escapes from a transcript")
    tr.add_argument("files", nargs="+", type=Path)
    tr.add_argument("-o", "--output", type=Path, default=None, help="Output (single file)")
    tr.add_argument("--stdout", action="store_true")
    tr.set_defaults(handler=cmd_transcript)
    lo = sub.add_parser("logs", help="Strip literal escape text from *.log files")
    lo.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    lo.add_argument("-n", "--dry-run", action="store_true")
    lo.add_argument("-j", "--workers", type=int, default=4)
    lo.set_defaults(handler=cmd_logs)
    hi = sub.add_parser("history", help="Deduplicate + de-junk a shell history file")
    hi.add_argument("--file", type=Path, default=BASH_HISTORY)
    hi.add_argument("--drop", nargs="*", default=[], metavar="TEXT", help="Drop lines containing TEXT")
    hi.add_argument("--keep-cd-printf", action="store_true", help=f"Keep the original junk pattern {DEFAULT_DROP!r}")
    hi.add_argument("--backup", action="store_true")
    hi.add_argument("-n", "--dry-run", action="store_true")
    hi.set_defaults(handler=cmd_history)
    mc = sub.add_parser("mc-import", help="MC [cmdline] history -> bash history")
    mc.add_argument("--source", type=Path, default=MC_HISTORY)
    mc.add_argument("--dest", type=Path, default=BASH_HISTORY)
    mc.add_argument("--allow-dupes", action="store_true")
    mc.add_argument("-n", "--dry-run", action="store_true")
    mc.set_defaults(handler=cmd_mc_import)
    ta = sub.add_parser("trim-audio", help="Keep the last N minutes of audio")
    ta.add_argument("input", type=Path)
    ta.add_argument("-o", "--output", type=Path, default=None)
    ta.add_argument("--minutes", type=float, default=5.0)
    ta.add_argument("--bitrate", default="320k")
    ta.set_defaults(handler=cmd_trim_audio)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
