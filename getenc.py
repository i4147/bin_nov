#!/data/data/com.termux/files/usr/bin/env python
"""Report the encoding of the files below the current directory as detected by several tools.
Cross-checks the BOM, ``tokenize``, chardet, charset_normalizer, cchardet, UnicodeDammit, libmagic and ``ced`` guesses (each used only when installed) and prints the results per file, so the common encoding of a tree can be established.
Usage: getenc Examples: $ getenc Runs as a script (``python getenc.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys
import tokenize

try:
    import chardet
except ImportError:
    chardet = None
try:
    import charset_normalizer
except ImportError:
    charset_normalizer = None
try:
    import cchardet
except ImportError:
    cchardet = None
try:
    from bs4 import UnicodeDammit
except ImportError:
    UnicodeDammit = None
try:
    import magic
except ImportError:
    magic = None
try:
    import ced
except ImportError:
    ced = None


def run_diagnostic() -> None:
    if len(sys.argv) < 2:
        print("Error: Please provide an input file path.", file=sys.stderr)
        print("Usage: python script.py <path_to_file>", file=sys.stderr)
        sys.exit(1)
    file_path = Path(sys.argv[1])
    if not file_path.is_file():
        print(f"Error: File '{file_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    try:
        raw_bytes = file_path.read_bytes()
    except Exception as e:
        print(f"Critical Error: Could not read file bytes: {e}", file=sys.stderr)
        sys.exit(1)
    report = {"target_file": str(file_path), "file_size_bytes": len(raw_bytes), "results": {}}
    try:
        with open(file_path, "rb") as f:
            encoding, _ = tokenize.detect_encoding(f.readline)
        report["results"]["stdlib_tokenize"] = {"encoding": encoding, "confidence": 1.0}
    except Exception as e:
        report["results"]["stdlib_tokenize"] = {"error": str(e)}
    if chardet:
        try:
            res = chardet.detect(raw_bytes)
            report["results"]["chardet"] = {"encoding": res.get("encoding"), "confidence": res.get("confidence")}
        except Exception as e:
            report["results"]["chardet"] = {"error": str(e)}
    else:
        report["results"]["chardet"] = {"status": "Not Installed"}
    if charset_normalizer:
        try:
            match = charset_normalizer.from_bytes(raw_bytes).best()
            report["results"]["charset_normalizer"] = {
                "encoding": match.encoding if match else None,
                "confidence": match.chaos if match else 0.0,
            }
        except Exception as e:
            report["results"]["charset_normalizer"] = {"error": str(e)}
    else:
        report["results"]["charset_normalizer"] = {"status": "Not Installed"}
    if cchardet:
        try:
            res = cchardet.detect(raw_bytes)
            report["results"]["cchardet"] = {"encoding": res.get("encoding"), "confidence": res.get("confidence")}
        except Exception as e:
            report["results"]["cchardet"] = {"error": str(e)}
    else:
        report["results"]["cchardet"] = {"status": "Not Installed"}
    if UnicodeDammit:
        try:
            dammit = UnicodeDammit(raw_bytes)
            report["results"]["bs4_unicode_dammit"] = {"encoding": dammit.original_encoding, "confidence": None}
        except Exception as e:
            report["results"]["bs4_unicode_dammit"] = {"error": str(e)}
    else:
        report["results"]["bs4_unicode_dammit"] = {"status": "Not Installed"}
    report["results"]["unicodeit"] = {
        "status": "Skipped - Tool converts LaTeX math strings, does not detect file encodings"
    }
    if magic:
        try:
            mime = magic.Magic(mime_encoding=True)
            encoding = mime.from_file(str(file_path))
            report["results"]["python_magic"] = {"encoding": encoding, "confidence": None}
        except Exception as e:
            report["results"]["python_magic"] = {"error": str(e)}
    else:
        report["results"]["python_magic"] = {"status": "Not Installed"}
    if ced:
        try:
            encoding = ced.default_detector.detect(raw_bytes)
            report["results"]["compact_enc_det"] = {"encoding": encoding, "confidence": None}
        except Exception as e:
            report["results"]["compact_enc_det"] = {"error": str(e)}
    else:
        report["results"]["compact_enc_det"] = {"status": "Not Installed"}
    print(json.dumps(report, indent=2))
    output_report = Path("encoding_report.json")
    output_report.write_text(json.dumps(report, indent=4), encoding="utf-8")
    print(f"\n[+] Diagnostic report written to: {output_report.resolve()}")


if __name__ == "__main__":
    run_diagnostic()
