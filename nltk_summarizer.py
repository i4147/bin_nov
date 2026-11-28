#!/data/data/com.termux/files/usr/bin/env python
"""Summarize text files with NLTK frequency-based sentence scoring.

Scores sentences by stemmed word frequency minus stopwords and writes <stem>_summary<suffix> with the top-N sentences; downloads the required NLTK data on first run.
"""

import heapq
from collections import defaultdict
from pathlib import Path

import nltk
from loguru import logger
from nltk.corpus import stopwords
from nltk.probability import FreqDist
from nltk.stem import PorterStemmer
from nltk.tokenize import sent_tokenize, word_tokenize

nltk.download("punkt_tab", quiet=True)
nltk.download("stopwords", quiet=True)


def _score_sentences(text: str, top_n: int = 5) -> list[str]:
    sentences = sent_tokenize(text)
    if len(sentences) <= top_n:
        return sentences

    stop_words = set(stopwords.words("english"))
    ps = PorterStemmer()
    word_freq: FreqDist = FreqDist()

    for sent in sentences:
        words = word_tokenize(sent.lower())
        for w in words:
            if w.isalnum() and w not in stop_words:
                word_freq[ps.stem(w)] += 1

    max_freq = max(word_freq.values(), default=1)

    sent_scores: dict[int, float] = {}
    for i, sent in enumerate(sentences):
        words = word_tokenize(sent.lower())
        score = 0.0
        for w in words:
            if w.isalnum() and w not in stop_words:
                score += word_freq[ps.stem(w)] / max_freq
        sent_scores[i] = score

    top_indices = heapq.nlargest(top_n, sent_scores, key=sent_scores.get)
    return [sentences[i] for i in sorted(top_indices)]


def summarize_file(input_path: str | Path, top_n: int = 5) -> Path:
    src = Path(input_path)
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    text = src.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        logger.warning("Input file is empty, writing empty summary")
        summary_lines: list[str] = []
    else:
        summary_lines = _score_sentences(text, top_n)

    out_path = src.with_name(f"{src.stem}_summary{src.suffix}")
    out_path.write_text("\n".join(summary_lines), encoding="utf-8")
    logger.info(f"Summary saved to {out_path}")
    return out_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        logger.error("Usage: python summarize.py <input_file> [top_n]")
        raise SystemExit(1)

    input_file = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    summarize_file(input_file, n)
