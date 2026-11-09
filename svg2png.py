#!/data/data/com.termux/files/usr/bin/env python
"""Convert an SVG file to PNG with CairoSVG.
Writes the ``.png`` next to the source and, with ``-r``, deletes the original SVG.
Usage: svg2png FILE.svg [-r] Examples: $ svg2png logo.svg Runs as a script (``python svg2png.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

import cairosvg


def main() -> int:
    svg = Path(sys.argv[1])
    png = svg.with_suffix(".png")
    cairosvg.svg2png(url=str(svg), write_to=str(png))
    print(f"Saved: {png}")
    if "-r" in sys.argv[2:]:
        svg.unlink()
        print(f"Removed: {svg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
