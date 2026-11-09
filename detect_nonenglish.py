#!/data/data/com.termux/files/usr/bin/env python
"""Find the non-English content of text files with a chosen language-detection backend.
``--backend`` picks gcld3, pycld2, langdetect, lingua or fast-langdetect, the files are filtered by extension, excluded directories and size, and the result goes to a text or JSON report (``-l`` adds the per-line findings) with a confidence threshold.
Usage: detect_nonenglish [-b BACKEND] [-l] [-o REPORT] [paths ...] Positional arguments: paths ...
Files and/or directories to scan.
Options: --backend, -b Language-detection backend to use.
-e, --extensions Additional file extensions to include (e.g.
'.foo .bar').
--exclude-dirs Additional directory names to skip (e.g.
'vendor tmp').
-o, --output Output report path.
Extension decides the format (.txt/.json).
-f, --format Force output format (overrides the one inferred from --output).
-l, --detailed Also scan and report each non-English line within files.
--min-confidence --max-size-mb Maximum file size in MB to analyse (default: 10).
--list-backends List available backends and exit.
Examples: $ detect_nonenglish -b langdetect -l -o report.json docs/ Command-line script: run ``python detect_nonenglish.py`` or the ``detect_nonenglish`` command.
The module is importable as well."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from dh import MAX_WORKERS, SKIP_DIRS


DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt",
    ".csv",
    ".tsv",
    ".log",
    ".md",
    ".rst",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".html",
    ".css",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".env",
    ".properties",
    ".sh",
    ".bash",
    ".bat",
    ".ps1",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".pl",
    ".r",
    ".sql",
    ".swift",
    ".kt",
    ".scala",
    ".lua",
    ".tex",
    ".bib",
    ".gitignore",
    ".dockerfile",
})
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MIN_CONFIDENCE = 0.5
SAMPLE_CHARS = 10_000
MIN_LINE_LEN = 3


@dataclass
class LineFinding:
    line_num: int
    text: str
    lang: str
    confidence: float


@dataclass
class FileResult:
    path: str
    language: Optional[str] = None
    confidence: float = 0.0
    non_english_lines: list[LineFinding] = field(default_factory=list)
    error: Optional[str] = None


class Backend:
    name: str = "base"

    def detect(self, text: str) -> tuple[str, float, bool]:
        raise NotImplementedError


class Gcld3Backend(Backend):
    name = "gcld3"

    def __init__(self) -> None:
        import gcld3

        self._det = gcld3.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=10_000)

    def detect(self, text: str) -> tuple[str, float, bool]:
        if not text or not text.strip():
            return "und", 0.0, False
        try:
            r = self._det.FindLanguage(text=text[:10_000])
        except Exception:
            return "und", 0.0, False
        return (
            (r.language or "und"),
            float(r.probability or 0.0),
            bool(r.is_reliable),
        )


class Pycld2Backend(Backend):
    name = "pycld2"

    def __init__(self) -> None:
        import pycld2 as cld2

        self._cld2 = cld2

    def detect(self, text: str) -> tuple[str, float, bool]:
        if not text or not text.strip():
            return "un", 0.0, False
        try:
            reliable, _, details = self._cld2.detect(text)
        except Exception:
            return "un", 0.0, False
        if not details:
            return "un", 0.0, False
        _name, code, _lang, conf = details[0]
        return (code or "un").lower(), float(conf or 0.0) / 100.0, bool(reliable)


class LangdetectBackend(Backend):
    name = "langdetect"

    def __init__(self) -> None:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        self._detect_langs = detect_langs

    def detect(self, text: str) -> tuple[str, float, bool]:
        if not text or len(text.strip()) < 3:
            return "und", 0.0, False
        try:
            langs = self._detect_langs(text)
        except Exception:
            return "und", 0.0, False
        if not langs:
            return "und", 0.0, False
        top = langs[0]
        return top.lang, float(top.prob), True


class LinguaBackend(Backend):
    name = "lingua"

    def __init__(self) -> None:
        from lingua import LanguageDetectorBuilder

        self._det = LanguageDetectorBuilder.from_all_languages().build()

    def detect(self, text: str) -> tuple[str, float, bool]:
        if not text or len(text.strip()) < 3:
            return "und", 0.0, False
        try:
            values = self._det.compute_language_confidence_values(text)
        except Exception:
            return "und", 0.0, False
        if not values:
            return "und", 0.0, False
        top = values[0]
        iso = getattr(top.language, "iso_code_639_1", None)
        code = iso.name.lower() if iso is not None else "und"
        return code, float(top.value), True


class FastLangdetectBackend(Backend):
    name = "fast_langdetect"

    def __init__(self) -> None:
        from fast_langdetect import detect

        self._detect = detect

    def detect(self, text: str) -> tuple[str, float, bool]:
        if not text or not text.strip():
            return "und", 0.0, False
        try:
            r = self._detect(text)
        except Exception:
            return "und", 0.0, False
        if isinstance(r, list):
            if not r:
                return "und", 0.0, False
            r = r[0]
        if not isinstance(r, dict):
            return "und", 0.0, False
        return (
            str(r.get("lang", "und")).lower(),
            float(r.get("score", 0.0)),
            True,
        )


BACKEND_REGISTRY: dict[str, type[Backend]] = {
    Gcld3Backend.name: Gcld3Backend,
    Pycld2Backend.name: Pycld2Backend,
    LangdetectBackend.name: LangdetectBackend,
    LinguaBackend.name: LinguaBackend,
    FastLangdetectBackend.name: FastLangdetectBackend,
}
_BACKEND_CACHE: dict[str, Backend] = {}


def _get_backend(name: str) -> Backend:
    backend = _BACKEND_CACHE.get(name)
    if backend is None:
        backend = BACKEND_REGISTRY[name]()
        _BACKEND_CACHE[name] = backend
    return backend


def _backend_status(name: str) -> str:
    try:
        BACKEND_REGISTRY[name]()
        return "available"
    except ImportError:
        return "not installed"
    except Exception as exc:
        return f"error: {exc}"


def _is_finding(lang: str, confidence: float, min_confidence: float) -> bool:
    if not lang:
        return False
    base = lang.lower().split("-", 1)[0]
    if base in ("en", "und", "un", "unknown"):
        return False
    return confidence >= min_confidence


def _read_text(path: Path) -> Optional[str]:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def _process_file(task: tuple[str, str, bool, float, int]) -> FileResult:
    path_str, backend_name, detailed, min_conf, max_bytes = task
    path = Path(path_str)
    result = FileResult(path=path_str)
    try:
        if path.stat().st_size > max_bytes:
            result.error = f"file too large (> {max_bytes // (1024 * 1024)} MB)"
            return result
    except OSError as exc:
        result.error = f"cannot access file: {exc}"
        return result
    text = _read_text(path)
    if text is None:
        result.error = "cannot decode file"
        return result
    backend = _get_backend(backend_name)
    sample = text[:SAMPLE_CHARS]
    if len(sample.strip()) < MIN_LINE_LEN:
        return result
    lang, conf, _reliable = backend.detect(sample)
    file_is_non_english = _is_finding(lang, conf, min_conf)
    if not detailed:
        if file_is_non_english:
            result.language = lang
            result.confidence = conf
        return result
    findings: list[LineFinding] = []
    for idx, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if len(stripped) < MIN_LINE_LEN:
            continue
        l_lang, l_conf, _ = backend.detect(stripped)
        if _is_finding(l_lang, l_conf, min_conf):
            findings.append(
                LineFinding(
                    line_num=idx,
                    text=stripped if len(stripped) <= 240 else stripped[:240] + "…",
                    lang=l_lang,
                    confidence=l_conf,
                )
            )
    if findings:
        dominant = Counter(f.lang for f in findings).most_common(1)[0][0]
        result.language = dominant
        result.confidence = max(f.confidence for f in findings)
        result.non_english_lines = findings
    elif file_is_non_english:
        result.language = lang
        result.confidence = conf
    return result


def _discover_files(
    paths: Sequence[str],
    extensions: set[str],
    exclude_dirs: set[str],
) -> list[Path]:
    found: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in extensions:
                found.add(p.resolve())
            continue
        if not p.is_dir():
            print(f"warning: skipping non-existent path: {p}", file=sys.stderr)
            continue
        for candidate in p.rglob("*"):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate.suffix.lower() not in extensions:
                continue
            if any(part in exclude_dirs for part in candidate.parts):
                continue
            found.add(candidate.resolve())
    return sorted(found)


def _build_report(
    backend_name: str,
    paths: Sequence[str],
    files_scanned: int,
    results: list[FileResult],
    errors: list[FileResult],
    min_confidence: float,
    detailed: bool,
) -> dict:
    files_with_findings = sum(1 for r in results if r.language)
    return {
        "backend": backend_name,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paths": [str(p) for p in paths],
        "files_scanned": files_scanned,
        "files_with_findings": files_with_findings,
        "min_confidence": min_confidence,
        "detailed": detailed,
        "results": [asdict(r) for r in results],
        "errors": [{"path": r.path, "error": r.error} for r in errors],
    }


def _write_text_report(output_path: Path, report: dict) -> None:
    bar = "=" * 60
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(f"{bar}\n")
        fh.write("Non-English Text Detection Report\n")
        fh.write(f"{bar}\n")
        fh.write(f"Backend            : {report['backend']}\n")
        fh.write(f"Generated          : {report['generated']}\n")
        fh.write(f"Paths              : {', '.join(report['paths'])}\n")
        fh.write(f"Files scanned      : {report['files_scanned']}\n")
        fh.write(f"Files with findings: {report['files_with_findings']}\n")
        fh.write(f"Min confidence     : {report['min_confidence']}\n")
        fh.write(f"Detailed mode      : {report['detailed']}\n")
        fh.write(f"{bar}\n\n")
        results = report["results"]
        if not results:
            fh.write("No non-English content detected.\n\n")
        else:
            for r in results:
                fh.write(f"{'-' * 60}\n")
                fh.write(f"File: {r['path']}\n")
                fh.write(f"  Language  : {r.get('language') or '?'}\n")
                fh.write(f"  Confidence: {r.get('confidence', 0.0):.3f}\n")
                findings = r.get("non_english_lines") or []
                if findings:
                    fh.write(f"  Non-English lines: {len(findings)}\n")
                    for line in findings:
                        fh.write(f"    L{line['line_num']} [{line['lang']}] ({line['confidence']:.3f})\n")
                        fh.write(f"      {line['text']}\n")
                fh.write("\n")
        errors = report["errors"]
        if errors:
            fh.write(f"{bar}\n")
            fh.write(f"Errors encountered: {len(errors)}\n")
            fh.write(f"{bar}\n")
            for e in errors:
                fh.write(f"  {e['path']}: {e['error']}\n")


def _write_json_report(output_path: Path, report: dict) -> None:
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="detect_nonenglish.py",
        description="Find non-English content in text files using a chosen backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  detect_nonenglish.py --backend langdetect ./src\n"
            "  detect_nonenglish.py --backend lingua -l -o report.json ./src\n"
            "  detect_nonenglish.py --backend gcld3 file1.py file2.md\n"
        ),
    )
    parser.add_argument(
        "--backend",
        "-b",
        choices=sorted(BACKEND_REGISTRY),
        help="Language-detection backend to use.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files and/or directories to scan.",
    )
    parser.add_argument(
        "-e",
        "--extensions",
        nargs="+",
        default=[],
        help="Additional file extensions to include (e.g. '.foo .bar').",
    )
    parser.add_argument(
        "--exclude-dirs",
        nargs="+",
        default=[],
        help="Additional directory names to skip (e.g. 'vendor tmp').",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output report path. Extension decides the format (.txt/.json).",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["text", "json"],
        default=None,
        help="Force output format (overrides the one inferred from --output).",
    )
    parser.add_argument(
        "-l",
        "--detailed",
        action="store_true",
        help="Also scan and report each non-English line within files.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help=f"Minimum confidence (0.0-1.0) for a detection to be reported (default: {DEFAULT_MIN_CONFIDENCE}).",
    )
    parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_BYTES // (1024 * 1024),
        help="Maximum file size in MB to analyse (default: 10).",
    )
    parser.add_argument(
        "--list-backends",
        action="store_true",
        help="List available backends and exit.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.list_backends:
        print("Available backends:")
        for name in sorted(BACKEND_REGISTRY):
            print(f"  {name:<16} {_backend_status(name)}")
        return 0
    if not args.backend:
        parser.error("--backend is required (use --list-backends to see options).")
    if not args.paths:
        parser.error("at least one file or directory path is required.")
    if not 0.0 <= args.min_confidence <= 1.0:
        parser.error("--min-confidence must be between 0.0 and 1.0.")
    try:
        _get_backend(args.backend)
    except Exception as exc:
        print(
            f"error: cannot initialize backend {args.backend!r}: {exc}",
            file=sys.stderr,
        )
        return 2
    extensions = set(DEFAULT_EXTENSIONS)
    extensions.update(ext.lower() for ext in args.extensions)
    exclude_dirs = set(SKIP_DIRS) | set(args.exclude_dirs)
    print(f"Scanning with backend: {args.backend}", file=sys.stderr)
    files = _discover_files(args.paths, extensions, exclude_dirs)
    print(f"Found {len(files)} candidate files.", file=sys.stderr)
    max_bytes = args.max_size_mb * 1024 * 1024
    tasks = [(str(f), args.backend, args.detailed, args.min_confidence, max_bytes) for f in files]
    results: list[FileResult] = []
    errors: list[FileResult] = []
    if tasks:
        workers = max(1, min(MAX_WORKERS, len(tasks)))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_file, t): t for t in tasks}
            total = len(futures)
            done = 0
            for future in as_completed(futures):
                done += 1
                if done % 25 == 0 or done == total:
                    print(
                        f"\rProgress: {done}/{total} files processed",
                        file=sys.stderr,
                        end="",
                        flush=True,
                    )
                try:
                    r = future.result()
                except Exception as exc:
                    path = futures[future][0]
                    r = FileResult(path=path, error=f"worker crashed: {exc}")
                if r.error:
                    errors.append(r)
                elif r.language or r.non_english_lines:
                    results.append(r)
            print(file=sys.stderr)
    results.sort(key=lambda r: r.path)
    errors.sort(key=lambda r: r.path)
    report = _build_report(
        backend_name=args.backend,
        paths=args.paths,
        files_scanned=len(files),
        results=results,
        errors=errors,
        min_confidence=args.min_confidence,
        detailed=args.detailed,
    )
    output = args.output
    fmt = args.format
    if fmt is None:
        if output and output.lower().endswith(".json"):
            fmt = "json"
        else:
            fmt = "text"
    if output is None:
        output = "noneng.json" if fmt == "json" else "noneng.txt"
    output_path = Path(output)
    if fmt == "json":
        _write_json_report(output_path, report)
    else:
        _write_text_report(output_path, report)
    bar = "=" * 60
    print(bar)
    print(f"Backend          : {args.backend}")
    print(f"Files scanned    : {report['files_scanned']}")
    print(f"Files with hits  : {report['files_with_findings']}")
    print(f"Errors           : {len(report['errors'])}")
    print(f"Report written to: {output_path.resolve()}")
    print(bar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
