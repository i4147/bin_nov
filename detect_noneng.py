#!/data/data/com.termux/files/usr/bin/env python
"""Detect the Chinese, Persian, Japanese and Korean text in files.
Matches the CJK, kana, hangul and Arabic script ranges in the files below a directory (10 MB limit), reports the hits and with ``-c`` copies each file into ``~/tmp/langs/<lang>``; it is a dry run by default.
Usage: detect_noneng [-c] [directory] Positional arguments: directory Root directory to scan (default: current dir) Options: -c, --copy Copy matched files into ~/tmp/langs/<lang>/ directories Examples: $ detect_noneng -c ~/downloads Command-line script: run ``python detect_noneng.py`` or the ``detect_noneng`` command.
The module is importable as well."""

import argparse
from concurrent.futures import as_completed
from pathlib import Path
import re
import shutil

from dh import SKIP_DIRS
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

try:
    from concurrent.futures import InterpreterPoolExecutor
except ImportError:
    from concurrent.futures import ProcessPoolExecutor as InterpreterPoolExecutor
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
MAX_BYTES: int = 10 * 1024 * 1024


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return False
    sample = data[:8192]
    printable = sum(1 for b in sample if b >= 32 or b in (9, 10, 13))
    return printable / len(sample) > 0.90


def detect(path_str: str) -> tuple[str, list[str]] | None:
    path = Path(path_str)
    try:
        if path.is_symlink():
            return None
        size = path.stat().st_size
        if size == 0 or size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not _looks_like_text(data):
        return None
    text = data.decode("utf-8", errors="ignore")
    has_kana = KANA_RE.search(text) is not None
    has_cjk = CJK_RE.search(text) is not None
    langs: list[str] = []
    if has_kana:
        langs.append("japanese")
    if has_cjk and not has_kana:
        langs.append("chinese")
    if HANGUL_RE.search(text):
        langs.append("korean")
    if ARABIC_RE.search(text):
        langs.append("persian")
    return (path_str, langs) if langs else None


def _walk(root: Path) -> list[str]:
    out: list[str] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in SKIP_DIRS:
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    out.append(str(entry))
            except OSError:
                continue
    return out


def _unique_dest(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        candidate = dest_dir / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recursively detect Chinese/Persian/Japanese/Korean text in files. Dry run by default.",
    )
    parser.add_argument(
        "-c",
        "--copy",
        action="store_true",
        help="copy matched files into ~/tmp/langs/<lang>/ directories",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="root directory to scan (default: current dir)",
    )
    args = parser.parse_args()
    console = Console()
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]error:[/] not a directory: {root}", stderr=True)
        return 2
    with console.status(f"[bold cyan]Walking[/] {root} …") as status:
        files = _walk(root)
        status.update(f"[bold cyan]Walking done[/] — {len(files)} file(s)")
    hits: list[tuple[str, list[str]]] = []
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[found]} hits"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    with progress:
        task = progress.add_task("Scanning", total=len(files), found=0)
        with InterpreterPoolExecutor() as ex:
            futures = {ex.submit(detect, f): f for f in files}
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                except Exception:
                    result = None
                if result is not None:
                    hits.append(result)
                progress.update(
                    task,
                    advance=1,
                    found=len(hits),
                )
    dest_root = Path.home() / "tmp" / "langs"
    if hits:
        console.rule("[bold green]Matches")
        for path_str, langs in sorted(hits):
            src = Path(path_str)
            try:
                rel = src.relative_to(root)
            except ValueError:
                rel = src
            console.print(f"{rel} [bold magenta]→[/] {', '.join(langs)}")
        if args.copy:
            with console.status("[bold cyan]Copying …"):
                for path_str, langs in hits:
                    src = Path(path_str)
                    for lang in langs:
                        dest_dir = dest_root / lang
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = _unique_dest(dest_dir, src.name)
                        shutil.copy2(src, dest)
    verb = "copied" if args.copy else "would copy (dry run)"
    console.print(
        f"\n[bold]Scanned[/] {len(files)} file(s); "
        f"[bold green]{len(hits)}[/] match(es). "
        f"[italic]{verb}[/] → {dest_root}/<lang>/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
