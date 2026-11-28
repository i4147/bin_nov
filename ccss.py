#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations
from functools import partial
from pathlib import Path
import sys

from dh import cprint, fsz, get_files, gsz, mpf, runcmd


def process_file(path: Path, *, mode: str) -> bool | None:
    path = Path(path)
    before = gsz(path)
    if not path.exists():
        return False

    if mode == "c":
        if len(path.read_text().splitlines()) == 1:
            return False
        cmd = [
            "cleancss",
            "-O2",
            "all:off;removeDuplicateRules:on",
            str(path),
            "-o",
            str(path),
        ]
        no_change_color = "white"
        error_color = "red"
        success_return: bool | None = True
    elif mode == "b":
        cmd = ["cleancss", "--format", "beautify", str(path), "-o", str(path)]
        no_change_color = (245, 245, 245)
        error_color = "yellow"
        success_return = None
    else:
        msg = f"Unsupported mode: {mode!r}"
        raise ValueError(msg)

    print(f"{path.name}", end=" ")
    res, _, _err = runcmd(cmd, show_output=True)
    if not res:
        after = gsz(path)
        diffsize = before - after
        if not diffsize:
            cprint("[NO CHANGE]", color=no_change_color)
            if mode == "b":
                return None
        else:
            ratio = (after / before * 40) if mode == "c" else (diffsize / before * 40)
            cprint(f"[OK] - {fsz(diffsize)} {abs(ratio):.1f}%", color="cyan")
        return success_return

    cprint("[ERROR]", color=error_color)
    return False if mode == "c" else None


def parse_args(argv: list[str]) -> tuple[str, list[Path]]:
    mode: str | None = None
    paths: list[Path] = []
    for arg in argv:
        if arg in ("-b", "-c") and mode is None:
            mode = arg.lstrip("-")
        else:
            paths.append(Path(arg))
    return mode or "c", paths


def main() -> None:
    mode, paths = parse_args(sys.argv[1:])

    cwd = Path.cwd()
    before = gsz(cwd)
    files = paths or get_files(cwd, ext=[".css", ".min.css"])
    _ = mpf(partial(process_file, mode=mode), files)
    diff_size = before - gsz(cwd)
    cprint(f"space freed : {fsz(diff_size)}", color="green")


if __name__ == "__main__":
    raise SystemExit(main())
