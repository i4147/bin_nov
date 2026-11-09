#!/data/data/com.termux/files/usr/bin/env python
"""Peel encoded strings recursively and rank the plausible plain-text results.
Applies base64, base32, base16, base58, hex, URL, ROT13/ROT47, Atbash, binary, HTML-entity, unicode-escape, gzip/zlib/bz2/lzma, and single-byte XOR decoders in a bounded search tree, scores each candidate by English letter frequency and prints the most readable chains (optionally as JSON).
Usage: dcode STRING | --input FILE [--json] [--max-depth N] Positional arguments: string String to be decoded Options: -i, --input Read input from file -o, --output Write output to file --json Output results in JSON format -q, --quiet Quiet mode --no-color Disable color output --max-depth Maximum recursion depth -s, --sensitive For sensitive data Examples: $ dcode 'SGVsbG8gd29ybGQ=' $ dcode -i payload.txt --json Command-line script: run ``python dcode.py`` or the ``dcode`` command.
The module is importable as well."""

import argparse
import base64
import binascii
import bz2
import gzip
import json
import lzma
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
import urllib.parse
import urllib.request
import zlib


ENGLISH_FREQS: dict[str, float] = {
    "E": 12.02,
    "T": 9.10,
    "A": 8.12,
    "O": 7.68,
    "I": 7.31,
    "N": 6.95,
    "S": 6.28,
    "R": 6.02,
    "H": 5.92,
    "D": 4.32,
    "L": 3.98,
    "U": 2.88,
    "C": 2.71,
    "M": 2.61,
    "F": 2.30,
    "Y": 2.11,
    "W": 2.09,
    "G": 2.03,
    "P": 1.82,
    "B": 1.49,
    "V": 1.11,
    "K": 0.69,
    "X": 0.17,
    "Q": 0.11,
    "J": 0.10,
    "Z": 0.07,
}


def calculate_score(text: str) -> float:
    if not text:
        return 0.0
    upper = text.upper()
    total = sum(1 for c in upper if c.isalpha())
    if total == 0:
        return 0.0
    chi_sq = 0.0
    for char, freq in ENGLISH_FREQS.items():
        observed = upper.count(char)
        expected = total * (freq / 100.0)
        chi_sq += ((observed - expected) ** 2) / (expected if expected > 0 else 1)
    printable_ratio = sum(1 for c in text if 32 <= ord(c) < 127 or c in "\n\r\t") / len(text)
    return printable_ratio * 100.0 - chi_sq * 0.01


class DecodeNode:
    def __init__(self, text: str, method: str, depth: int, score: float, path: list[str]) -> None:
        self.text = text
        self.method = method
        self.depth = depth
        self.score = score
        self.path = path


DECODERS: list[tuple[str, Callable[[str], list[str]]]] = []


def register_decoder(name: str) -> Callable[..., Any]:
    def decorator(func: Callable[[str], list[str]]) -> Callable[[str], list[str]]:
        DECODERS.append((name, func))
        return func

    return decorator


@register_decoder("base64")
def decode_base64(s: str) -> list[str]:
    res = []
    clean = s.strip().encode("utf-8")
    try:
        padded = clean + b"=" * (-len(clean) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if decoded and decoded != s:
            res.append(decoded)
    except Exception:
        pass
    try:
        decoded_url = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
        if decoded_url and decoded_url != s and decoded_url not in res:
            res.append(decoded_url)
    except Exception:
        pass
    return res


@register_decoder("base32")
def decode_base32(s: str) -> list[str]:
    res = []
    clean = s.strip().encode("utf-8")
    try:
        padded = clean + b"=" * (-len(clean) % 8)
        decoded = base64.b32decode(padded, casefold=True).decode("utf-8", errors="ignore")
        if decoded and decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("base16")
def decode_base16(s: str) -> list[str]:
    res = []
    clean = s.strip().encode("utf-8")
    try:
        decoded = base64.b16decode(clean, casefold=True).decode("utf-8", errors="ignore")
        if decoded and decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("base58")
def decode_base58(s: str) -> list[str]:
    res = []
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    try:
        n = 0
        for char in s.strip():
            if char not in alphabet:
                raise ValueError
            n = n * 58 + alphabet.index(char)
        hx = f"{n:x}"
        if len(hx) % 2 != 0:
            hx = "0" + hx
        decoded = binascii.unhexlify(hx).decode("utf-8", errors="ignore")
        if decoded and decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("hex")
def decode_hex(s: str) -> list[str]:
    res = []
    clean = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(clean) >= 2 and len(clean) % 2 == 0:
        try:
            decoded = binascii.unhexlify(clean).decode("utf-8", errors="ignore")
            if decoded and decoded != s:
                res.append(decoded)
        except Exception:
            pass
    return res


@register_decoder("url")
def decode_url(s: str) -> list[str]:
    res = []
    try:
        decoded = urllib.parse.unquote(s)
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("rot13")
def decode_rot13(s: str) -> list[str]:
    res = []
    try:
        trans = str.maketrans(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM",
        )
        decoded = s.translate(trans)
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("rot47")
def decode_rot47(s: str) -> list[str]:
    res = []
    try:
        chars = []
        for c in s:
            o = ord(c)
            if 33 <= o <= 126:
                chars.append(chr(33 + ((o - 33 + 47) % 94)))
            else:
                chars.append(c)
        decoded = "".join(chars)
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("atbash")
def decode_atbash(s: str) -> list[str]:
    res = []
    try:
        trans = str.maketrans(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "zyxwvutsrqponmlkjihgfedcbaZYXWVUTSRQPONMLKJIHGFEDCBA",
        )
        decoded = s.translate(trans)
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("binary")
def decode_binary(s: str) -> list[str]:
    res = []
    clean = s.strip().replace(" ", "")
    if re.match(r"^[01]+$", clean) and len(clean) % 8 == 0:
        try:
            byte_array = int(clean, 2).to_bytes((len(clean) + 7) // 8, byteorder="big")
            decoded = byte_array.decode("utf-8", errors="ignore")
            if decoded and decoded != s:
                res.append(decoded)
        except Exception:
            pass
    return res


@register_decoder("html_entities")
def decode_html(s: str) -> list[str]:
    res = []
    try:
        import html

        decoded = html.unescape(s)
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("unicode_escape")
def decode_unicode(s: str) -> list[str]:
    res = []
    try:
        decoded = s.encode().decode("unicode-escape")
        if decoded != s:
            res.append(decoded)
    except Exception:
        pass
    return res


@register_decoder("gzip_zlib")
def decode_compression(s: str) -> list[str]:
    res = []
    try:
        raw = s.encode("latin-1")
        for algo in [zlib.decompress, gzip.decompress, bz2.decompress, lzma.decompress]:
            try:
                decoded = algo(raw).decode("utf-8", errors="ignore")
                if decoded and decoded != s:
                    res.append(decoded)
            except Exception:
                pass
    except Exception:
        pass
    return res


@register_decoder("xor_single")
def decode_xor_single(s: str) -> list[str]:
    res = []
    try:
        raw = s.encode("latin-1")
        for key in range(256):
            xored = bytes([b ^ key for b in raw])
            try:
                decoded = xored.decode("utf-8", errors="strict")
                if decoded and decoded != s:
                    res.append(decoded)
            except Exception:
                pass
    except Exception:
        pass
    return res


def search_pipeline(initial_string: str, max_depth: int, quiet: bool, no_color: bool) -> list[DecodeNode]:
    visited: set[str] = {initial_string}
    queue: list[DecodeNode] = [DecodeNode(initial_string, "input", 0, calculate_score(initial_string), [])]
    results: list[DecodeNode] = []
    while queue:
        queue.sort(key=lambda x: x.score, reverse=True)
        current = queue.pop(0)
        results.append(current)
        if current.depth >= max_depth:
            continue
        for name, dec_func in DECODERS:
            try:
                outputs = dec_func(current.text)
                for out in outputs:
                    if out not in visited and len(out) > 0:
                        visited.add(out)
                        score = calculate_score(out)
                        new_path = current.path + [name]
                        node = DecodeNode(out, name, current.depth + 1, score, new_path)
                        queue.append(node)
            except Exception:
                pass
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("string", nargs="?", help="String to be decoded")
    parser.add_argument("-i", "--input", help="Read input from file")
    parser.add_argument("-o", "--output", help="Write output to file")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    parser.add_argument("--max-depth", type=int, default=2, help="Maximum recursion depth")
    parser.add_argument("-s", "--sensitive", action="store_true", help="For sensitive data")
    args = parser.parse_args()
    input_str = ""
    if args.string:
        input_str = args.string
    elif args.input:
        if Path(args.input).exists():
            with open(args.input, "r", encoding="utf-8", errors="ignore") as f:
                input_str = f.read()
    elif not sys.stdin.isatty():
        input_str = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)
    input_str = input_str.strip()
    if not input_str:
        sys.exit(0)
    results = search_pipeline(input_str, args.max_depth, args.quiet, args.no_color)
    output_data = []
    for r in results:
        output_data.append({"text": r.text, "method": r.method, "depth": r.depth, "score": r.score, "path": r.path})
    if args.json:
        out_json = json.dumps(output_data, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out_json)
        else:
            print(out_json)
    else:
        lines = []
        for r in results:
            path_str = " -> ".join(r.path) if r.path else "original"
            line = f"[{r.score:.2f}] ({path_str}): {r.text}"
            lines.append(line)
            if not args.quiet:
                print(line)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))


if __name__ == "__main__":
    main()
