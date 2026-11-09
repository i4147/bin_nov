#!/data/data/com.termux/files/usr/bin/env python
"""Render man pages to HTML with ``mandoc``.
Finds the man pages below the current directory (sections 1, 3, 4, 5, 7, 8 and the ``.3*``/``.n`` variants, gzipped or not), converts each one with ``mandoc -T html``, writes ``<name>.html`` and deletes the original.
Files are processed in parallel.
Usage: man_doc [files ...] Examples: $ man_doc /usr/share/man/man1/ Runs as a script (``python man_doc.py``) in the directory it should act on; the module can also be imported."""

from collections import deque
import gzip
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from dh import cprint, mpf, runcmd


def get_files(path: str | Path, ext: list[str] | None = None) -> list[Path]:
    path = Path(path)
    skip_dirs = {".git", "__pycache__"}
    queue = deque([path])
    files = []
    while queue:
        current = queue.popleft()
        try:
            entries = current.iterdir()
        except (PermissionError, OSError):
            continue
        for item in entries:
            if item.is_symlink():
                continue
            if item.is_dir() and item.name not in skip_dirs:
                queue.append(item)
            elif item.is_file() and (
                ext is None
                or item.suffix in ext
                or (
                    item.suffixes[-2:] == [".1", ".gz"]
                    or item.suffixes[-2:] == [".3", ".gz"]
                    or item.suffixes[-2:] == [".4", ".gz"]
                    or (item.suffixes[-2:] == [".5", ".gz"])
                    or (item.suffixes[-2:] == [".6", ".gz"])
                    or (item.suffixes[-2:] == [".7", ".gz"])
                    or (item.suffixes[-2:] == [".8", ".gz"])
                    or (item.suffixes[-2:] == [".3am", ".gz"])
                    or (item.suffixes[-2:] == [".3form", ".gz"])
                    or (item.suffixes[-2:] == [".3menu", ".gz"])
                    or (item.suffixes[-2:] == [".3ncurses", ".gz"])
                    or (item.suffixes[-2:] == [".3readline", ".gz"])
                    or (item.suffixes[-2:] == [".3t", ".gz"])
                    or (item.suffixes[-2:] == [".n", ".gz"])
                )
            ):
                files.append(item)
    return files


def safe_run(path: Path) -> bool:
    path = Path(path)
    is_gzipped = path.suffix == ".gz"
    if is_gzipped:
        with NamedTemporaryFile(mode="w", suffix=path.stem, delete=False) as tmp:
            with gzip.open(path, "rt", encoding="utf8") as gz:
                tmp.write(gz.read())
            tmp_path = tmp.name
    else:
        tmp_path = str(path)
    try:
        cmd = ["mandoc", "-T", "html", tmp_path]
        res, txt, err = runcmd(cmd, show_output=False)
        if res != 0:
            print(f"Error running mandoc: {err}", file=sys.stderr)
            return False
        if is_gzipped:
            outpath = path.with_suffix(".html")
        else:
            outpath = path.with_suffix(".html")
        outpath.write_text(txt, encoding="utf8")
        path.unlink()
        return True
    finally:
        if is_gzipped and Path(tmp_path).exists():
            Path(tmp_path).unlink()


def process_file(path: Path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    print(f"{path.name}", end=" ")
    res = safe_run(path)
    if res:
        cprint("[✓] ", "cyan")
        return True
    cprint("[ERROR]", "red")
    return False


def main() -> None:
    args = sys.argv[1:]
    cwd = Path.cwd()
    base_exts = [
        ".1",
        ".3",
        ".3am",
        ".3pm",
        ".3form",
        ".3menu",
        ".3ncurses",
        ".3readline",
        ".3t",
        ".4",
        ".5",
        ".7",
        ".8",
        ".n",
    ]
    all_exts = base_exts + [f"{ext}.gz" for ext in base_exts]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=all_exts)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
