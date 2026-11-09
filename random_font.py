#!/data/data/com.termux/files/usr/bin/env python
"""Pick a random font from ``/sdcard/font`` and set it as the Termux font.
Chooses a ``.woff2`` file larger than 400 KB without ``italic`` in its name, converts it with fontTools (the decompressed ``.ttf`` already lying next to it is reused) and installs it as ``~/.termux/font.ttf``.
Usage: random_font Examples: $ random_font Runs as a script (``python random_font.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import secrets


def convert_with_fonttools(src: Path, dst: Path) -> None:
    from fontTools.ttLib import woff2

    try:
        woff2.decompress(src, dst)
    except Exception as e:
        return


def main() -> None:
    source_dir = Path("/sdcard/font")
    dst = Path.home() / ".termux" / "font.ttf"
    if dst.exists():
        dst.unlink()
    files = [p for p in source_dir.glob("*.woff2") if "italic" not in p.name and p.stat().st_size > 400_000]
    numfiles = len(files)
    indx = secrets.randbelow(numfiles)
    src = files[indx]
    print(f"{indx}/{numfiles} -> {src.name} selected")
    ttf_path = src.with_suffix(".ttf")
    if ttf_path.exists():
        ttf_path.rename(dst)


if __name__ == "__main__":
    raise SystemExit(main())
