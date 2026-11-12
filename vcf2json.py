#!/data/data/com.termux/files/usr/bin/env python
"""Convert a vCard file to JSON.
Parses the ``BEGIN:VCARD`` blocks, unfolds the continued lines, decodes the quoted-printable values and writes the contacts as a JSON list.
Usage: vcf2json CONTACTS.vcf Examples: $ vcf2json contacts.vcf Runs as a script (``python vcf2json.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import quopri
import sys
from typing import Any


def decode_value(value: str, params: dict[str, Any]):
    encoding = None
    charset = "utf-8"
    for p in params:
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.upper()
            v = v.strip('"')
            if k == "ENCODING":
                encoding = v.upper()
            elif k == "CHARSET":
                charset = v
    if encoding == "QUOTED-PRINTABLE":
        raw = value.encode("latin-1", errors="ignore")
        data = quopri.decodestring(raw)
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")
    return value


def parse_vcard(input_path: Path):
    logical_lines = []
    with input_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if line.startswith((" ", "\t")):
                if logical_lines:
                    logical_lines[-1] += line[1:]
            else:
                logical_lines.append(line)
    cards = []
    current = None
    for line in logical_lines:
        if not line:
            continue
        upper = line.upper()
        if upper == "BEGIN:VCARD":
            current = {}
            continue
        if upper == "END:VCARD":
            if current is not None:
                cards.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        parts = left.split(";")
        name = parts[0].upper()
        params = parts[1:]
        if name == "N":
            decoded = decode_value(value, params)
            comps = [c.strip() for c in decoded.split(";") if c.strip()]
            current["N"] = " ".join(comps) if comps else decoded.strip(";")
        elif name == "FN":
            current["FN"] = decode_value(value, params)
        elif name == "TEL":
            current["TEL"] = decode_value(value, params)
    result = []
    for card in cards:
        result.append({
            "N": card.get("N", ""),
            "FN": card.get("FN", ""),
            "TEL": card.get("TEL", ""),
        })
    return result


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python vcard_to_json.py <input.vcf>")
        sys.exit(1)
    input_path = Path(sys.argv[1])
    if not input_path.is_file():
        print(f"Error: file not found -> {input_path}")
        sys.exit(1)
    output_path = input_path.with_suffix(".json")
    data = parse_vcard(input_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Converted '{input_path}' -> '{output_path}'")


if __name__ == "__main__":
    main()
