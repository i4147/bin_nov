#!/data/data/com.termux/files/usr/bin/env python
"""Merge all CSS files of the current directory into ``merged.css``.
Calls ``cleancss -O2`` with duplicate-rule removal over ``*.css`` and writes the combined, minified stylesheet to ``merged.css``.
Usage: mergecss Examples: $ mergecss Runs as a script (``python mergecss.py``) in the directory it should act on; the module can also be imported."""

from dh import runcmd


if __name__ == "__main__":
    cmd = ["cleancss", "-O2", "removeDuplicateRules:on", "*.css", "-o", "merged.css"]
    runcmd(cmd, show_output=True)
