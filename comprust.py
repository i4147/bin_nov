#!/data/data/com.termux/files/usr/bin/env python
"""Compress a text file with the LLM-oriented ``Compressor`` prompt helper.
Reads the given file, runs ``compression_prompt.Compressor.compress`` over it and writes the result next to the input with a ``.compressed`` suffix.
Usage: comprust FILE Examples: $ comprust notes.md Runs as a script (``python comprust.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from compression_prompt import Compressor


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    text = fn.read_text()
    c = Compressor()
    result = Compressor.compress(input_text=text)
    outf = fn.with_suffix(".compressed")
    outf.write_text(result.compressed_text)
