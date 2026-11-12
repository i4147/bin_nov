#!/data/data/com.termux/files/usr/bin/env python
"""Summarise a text file with TextRank and save the summary next to it.
Runs ``summa``'s summarizer at a 70 % ratio, writes the result to ``<name>_summsry.txt`` and prints it.
Usage: summar FILE Examples: $ summar article.txt The entry point refers to ``syd.argv`` instead of ``sys.argv``, so running the script directly raises ``NameError`` until that typo is fixed; use :func:`summarize_with_summa` programmatically.
"""

from pathlib import Path
import sys

from summa import summarizer


def summarize_with_summa(text: str):
    return summarizer.summarize(text=text, ratio=0.7)


if __name__ == "__main__":
    fn = Path(sys.argv[1].strip())
    txt = fn.read_text(encoding="utf-8")
    result = summarize_with_summa(txt)
    summary_path = fn.with_stem(fn.stem + "_summsry")
    summary_path.write_text(result, encoding="utf-8")
    print(result)
