#!/data/data/com.termux/files/usr/bin/env python
"""Split a text file into sentences and words with blingfire and print the words.
A tiny driver for ``blingfire_compat.text_to_sentences``/``text_to_words``; the sentence list is computed as well but only the word list is printed.
Usage: bf_nlp FILE Examples: $ bf_nlp article.txt Runs as a script (``python bf_nlp.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from blingfire_compat import text_to_sentences, text_to_words

if __name__ == "__main__":
    fn = Path(sys.argv[1])
    text = fn.read_text()
    sents = text_to_sentences(text)
    words = text_to_words(text)
    print(words)
