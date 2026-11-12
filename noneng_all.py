#!/data/data/com.termux/files/usr/bin/env python
"""Detect the language of every file below a directory.
Runs pycld2, gcld3, cld3, langdetect, py3langid, fast-langdetect or lingua (``-b``) on each file and prints the language and the confidence, with ``--only-non-english`` for the filtered list.
Usage: noneng_all [ROOT] [-b BACKEND] [--only-non-english] Positional arguments: root Root directory to scan (default: .) Options: -b, --backend Language detection backend (default: pycld2) --only-non-english Only print files detected as non-English Examples: $ noneng_all ~/docs -b lingua --only-non-english Command-line script: run ``python noneng_all.py`` or the ``noneng_all`` command.
The module is importable as well."""

import argparse
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable

DetectionResult = tuple[str, float]


def _detect_pycld2(text: str) -> DetectionResult:
    import pycld2 as cld2

    _, _, details = cld2.detect(text)
    if not details:
        return "unknown", 0.0
    # details[i] = (name, code, percent_int, score)
    _name, code, percent, _score = details[0]
    return code, percent / 100.0


_gcld3_detector = None


def _detect_gcld3(text: str) -> DetectionResult:
    global _gcld3_detector
    if _gcld3_detector is None:
        import gcld3

        _gcld3_detector = gcld3.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=1000)
    result = _gcld3_detector.FindLanguage(text=text)
    return result.language, float(result.probability)


def _detect_cld3(text: str) -> DetectionResult:
    import cld3

    result = cld3.get_language(text)
    if result is None:
        return "unknown", 0.0
    return result.language, float(result.probability)


def _detect_langdetect(text: str) -> DetectionResult:
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
    results = detect_langs(text)
    if not results:
        return "unknown", 0.0
    top = results[0]
    return top.lang, float(top.prob)


def _detect_pylangid(text: str) -> DetectionResult:
    import langid

    rankings = langid.rank(text)
    if not rankings:
        return "unknown", 0.0
    top_lang, top_score = rankings[0]
    top_k = rankings[:10]
    max_s = top_k[0][1]
    total = sum(math.exp(s - max_s) for _, s in top_k)
    prob = math.exp(top_score - max_s) / total if total else 0.0
    return top_lang, float(prob)


def _detect_fast_langdetect(text: str) -> DetectionResult:
    from fast_langdetect import detect

    result = detect(text)
    if isinstance(result, dict):
        return result.get("lang", "unknown").lower(), float(result.get("score", 0.0))
    if isinstance(result, list) and result:
        return result[0].get("lang", "unknown").lower(), float(result[0].get("score", 0.0))
    return "unknown", 0.0


_lingua_detector = None


def _detect_lingua(text: str) -> DetectionResult:
    global _lingua_detector
    if _lingua_detector is None:
        from lingua import LanguageDetectorBuilder

        _lingua_detector = LanguageDetectorBuilder.from_all_languages().build()
    confidences = _lingua_detector.compute_language_confidence_values(text)
    if not confidences:
        return "unknown", 0.0
    top_lang, top_conf = confidences[0]
    return top_lang.iso_code_639_1.name.lower(), float(top_conf)


BACKENDS: dict[str, Callable[[str], DetectionResult]] = {
    "pycld2": _detect_pycld2,
    "gcld3": _detect_gcld3,
    "cld3": _detect_cld3,
    "langdetect": _detect_langdetect,
    "pylangid": _detect_pylangid,
    "fast_langdetect": _detect_fast_langdetect,
    "lingua": _detect_lingua,
}

INSTALL_HINT: dict[str, str] = {
    "pycld2": "pycld2",
    "gcld3": "gcld3",
    "cld3": "cld3",
    "langdetect": "langdetect",
    "pylangid": "py3langid",
    "fast_langdetect": "fast-langdetect",
    "lingua": "lingua-language-detector",
}


def detect_language(text: str, backend: str) -> DetectionResult:
    try:
        return BACKENDS[backend](text)
    except Exception as e:
        return f"error", float("nan")


def scan_and_detect(root_dir: str, backend: str, only_non_english: bool) -> None:
    print(f"Backend : {backend}", flush=True)
    print(f"Scanning: {Path(root_dir).absolute()}\n", flush=True)
    print(f"{'File Path':<50} | {'Lang':<10} | {'Conf':<6} | Snippet", flush=True)
    print("-" * 120, flush=True)

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file in files:
            if file.startswith("."):
                continue

            file_path = Path(root).joinpath(file)
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
            except Exception:
                continue
            if not content:
                continue

            lang, prob = detect_language(content, backend)

            if only_non_english and lang == "en":
                continue

            snippet = content[:40].replace("\n", " ") + ("..." if len(content) > 40 else "")
            conf_str = "  n/a" if math.isnan(prob) else f"{prob:>5.0%}"
            print(f"{file_path:<50} | {lang:<10} | {conf_str:<6} | {snippet}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a directory and detect the language of each file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", nargs="?", default=".", help="Root directory to scan (default: .)")
    parser.add_argument(
        "-b", "--backend", choices=list(BACKENDS), default="pycld2", help="Language detection backend (default: pycld2)"
    )
    parser.add_argument("--only-non-english", action="store_true", help="Only print files detected as non-English")
    args = parser.parse_args()

    try:
        detect_language("hello world", args.backend)
    except ImportError as e:
        pkg = INSTALL_HINT.get(args.backend, args.backend)
        print(f"ERROR: backend '{args.backend}' is not available: {e}", file=sys.stderr)
        print(f"       Install with:  pip install {pkg}", file=sys.stderr)
        sys.exit(1)

    scan_and_detect(args.root, args.backend, args.only_non_english)


if __name__ == "__main__":
    main()
