#!/data/data/com.termux/files/usr/bin/env python
"""Human-readable UUIDs and hashes.
Turns a digest into a memorable word phrase and back (``--humanize``, ``--dehumanize``), generates the human-readable UUIDs (v3, v4, v5), verifies a phrase against a hex digest, reports the entropy bits for a word count and hashes text with the chosen algorithm.
Usage: hhash [--uuid|--humanize HEX|--verify HEX PHRASE|--hash TEXT] [--algorithm sha256] Options: --uuid Generate a human-readable UUID --uuid5 Generate a human-readable UUIDv5 --uuid3 Generate a human-readable UUIDv3 --humanize Humanize a hex digest --humanize-bytes Humanize UTF-8 text --verify Verify a phrase against a hex digest --dehumanize Convert a phrase back to hex --entropy Show entropy bits for given word count --hash Hash text and humanize --algorithm Hash algorithm (default: sha256) ...
plus 8 more; run ``hhash --help`` for the full list.
Examples: $ hhash --uuid $ hhash --hash 'hello' --algorithm sha512 Command-line script: run ``python hhash.py`` or the ``hhash`` command.
The module is importable as well."""

import argparse
from functools import reduce
import hashlib
import json
import operator
import sys
from typing import Any, Optional, Sequence
import uuid as uuidlib


DEFAULT_WORDLIST: tuple[str, ...] = (
    "ack",
    "alabama",
    "alanine",
    "alaska",
    "alpha",
    "angel",
    "apart",
    "april",
    "arizona",
    "arkansas",
    "artist",
    "asparagus",
    "aspen",
    "august",
    "autumn",
    "avocado",
    "bacon",
    "bakerloo",
    "batman",
    "beer",
    "berlin",
    "beryllium",
    "black",
    "blossom",
    "blue",
    "bluebird",
    "bravo",
    "bulldog",
    "burger",
    "butter",
    "california",
    "carbon",
    "cardinal",
    "carolina",
    "carpet",
    "cat",
    "ceiling",
    "charlie",
    "chicken",
    "coffee",
    "cola",
    "cold",
    "colorado",
    "comet",
    "connecticut",
    "crazy",
    "cup",
    "dakota",
    "december",
    "delaware",
    "delta",
    "diet",
    "don",
    "double",
    "early",
    "earth",
    "east",
    "echo",
    "edward",
    "eight",
    "eighteen",
    "eleven",
    "emma",
    "enemy",
    "equal",
    "failed",
    "fanta",
    "fifteen",
    "fillet",
    "finch",
    "fish",
    "five",
    "fix",
    "floor",
    "florida",
    "football",
    "four",
    "fourteen",
    "foxtrot",
    "freddie",
    "friend",
    "fruit",
    "gee",
    "georgia",
    "glucose",
    "golf",
    "green",
    "grey",
    "hamper",
    "happy",
    "harry",
    "hawaii",
    "helium",
    "high",
    "hot",
    "hotel",
    "hydrogen",
    "idaho",
    "illinois",
    "india",
    "indigo",
    "ink",
    "iowa",
    "island",
    "item",
    "jersey",
    "jig",
    "johnny",
    "juliet",
    "july",
    "jupiter",
    "kansas",
    "kentucky",
    "kilo",
    "king",
    "kitten",
    "lactose",
    "lake",
    "lamp",
    "lemon",
    "leopard",
    "lima",
    "lion",
    "lithium",
    "london",
    "louisiana",
    "low",
    "magazine",
    "magnesium",
    "maine",
    "mango",
    "march",
    "mars",
    "maryland",
    "massachusetts",
    "may",
    "mexico",
    "michigan",
    "mike",
    "minnesota",
    "mirror",
    "mississippi",
    "missouri",
    "mobile",
    "mockingbird",
    "monkey",
    "montana",
    "moon",
    "mountain",
    "muppet",
    "music",
    "nebraska",
    "neptune",
    "network",
    "nevada",
    "nine",
    "nineteen",
    "nitrogen",
    "north",
    "november",
    "nuts",
    "october",
    "ohio",
    "oklahoma",
    "one",
    "orange",
    "oranges",
    "oregon",
    "oscar",
    "oven",
    "oxygen",
    "papa",
    "paris",
    "pasta",
    "pennsylvania",
    "pip",
    "pizza",
    "pluto",
    "potato",
    "princess",
    "purple",
    "quebec",
    "queen",
    "quiet",
    "red",
    "river",
    "robert",
    "robin",
    "romeo",
    "rugby",
    "sad",
    "salami",
    "saturn",
    "september",
    "seven",
    "seventeen",
    "shade",
    "sierra",
    "single",
    "sink",
    "six",
    "sixteen",
    "skylark",
    "snake",
    "social",
    "sodium",
    "solar",
    "south",
    "spaghetti",
    "speaker",
    "spring",
    "stairway",
    "steak",
    "stream",
    "summer",
    "sweet",
    "table",
    "tango",
    "ten",
    "tennessee",
    "tennis",
    "texas",
    "thirteen",
    "three",
    "timing",
    "triple",
    "twelve",
    "twenty",
    "two",
    "uncle",
    "undress",
    "uniform",
    "uranus",
    "utah",
    "vegan",
    "venus",
    "vermont",
    "victor",
    "video",
    "violet",
    "virginia",
    "washington",
    "west",
    "whiskey",
    "white",
    "william",
    "winner",
    "winter",
    "wisconsin",
    "wolfram",
    "wyoming",
    "xray",
    "yankee",
    "yellow",
    "zebra",
    "zulu",
)


class HumanHasher:
    def __init__(self, wordlist: Sequence[str] = DEFAULT_WORDLIST) -> None:
        if len(wordlist) != 256:
            msg = "Wordlist must have exactly 256 items"
            raise ValueError(msg)
        if len(set(wordlist)) != 256:
            msg = "Wordlist must contain 256 unique words"
            raise ValueError(msg)
        self.wordlist: tuple[str, ...] = tuple(wordlist)

    @classmethod
    def from_file(cls, path: str) -> "HumanHasher":
        with open(path, encoding="utf-8") as f:
            words = [line.strip() for line in f if line.strip()]
        return cls(words)

    def humanize(self, hexdigest: str, words: int = 4, separator: str = "-") -> str:
        hexdigest = hexdigest.replace("-", "").lower()
        if len(hexdigest) % 2 != 0:
            msg = "Hex digest must have even length"
            raise ValueError(msg)
        byte_values: list[int] = [int(hexdigest[i : i + 2], 16) for i in range(0, len(hexdigest), 2)]
        compressed = self.compress(byte_values, words)
        return separator.join(self.wordlist[b] for b in compressed)

    def humanize_bytes(self, data: bytes, words: int = 4, separator: str = "-") -> str:
        return self.humanize(data.hex(), words, separator)

    def humanize_uuid(self, uuid_obj: uuidlib.UUID, words: int = 4, separator: str = "-") -> str:
        return self.humanize(uuid_obj.hex, words, separator)

    def verify(self, hexdigest: str, phrase: str, words: int = 4, separator: str = "-") -> bool:
        return self.humanize(hexdigest, words, separator) == phrase

    def entropy_bits(self, words: int) -> int:
        return words * 8

    def dehumanize(self, phrase: str, separator: str = "-") -> str:
        parts = phrase.split(separator)
        indices = [self.wordlist.index(w) for w in parts]
        return "".join(f"{b:02x}" for b in indices)

    @staticmethod
    def compress(byte_values: Sequence[int], target: int) -> list[int]:
        length = len(byte_values)
        if target > length:
            msg = "Fewer input bytes than requested output"
            raise ValueError(msg)
        seg_size = length // target
        segments = [list(byte_values[i * seg_size : (i + 1) * seg_size]) for i in range(target)]
        segments[-1].extend(byte_values[target * seg_size :])
        checksum = lambda b: reduce(operator.xor, b, 0)
        return [checksum(seg) for seg in segments]

    def uuid(self, words: int = 4, separator: str = "-") -> tuple[str, str]:
        digest = uuidlib.uuid4().hex
        return self.humanize(digest, words, separator), digest

    def uuid_obj(self, words: int = 4, separator: str = "-") -> tuple[uuidlib.UUID, str, str]:
        u = uuidlib.uuid4()
        digest = u.hex
        return u, self.humanize(digest, words, separator), digest

    def uuid5(self, namespace: uuidlib.UUID, name: str, words: int = 4, separator: str = "-") -> tuple[str, str]:
        u = uuidlib.uuid5(namespace, name)
        digest = u.hex
        return self.humanize(digest, words, separator), digest

    def uuid3(self, namespace: uuidlib.UUID, name: str, words: int = 4, separator: str = "-") -> tuple[str, str]:
        u = uuidlib.uuid3(namespace, name)
        digest = u.hex
        return self.humanize(digest, words, separator), digest

    def hash(
        self, data: str | bytes, algorithm: str = "sha256", words: int = 4, separator: str = "-"
    ) -> tuple[str, str]:
        if isinstance(data, str):
            data = data.encode("utf-8")
        h = hashlib.new(algorithm, data)
        digest = h.hexdigest()
        return self.humanize(digest, words, separator), digest


DEFAULT_HASHER = HumanHasher()
uuid = DEFAULT_HASHER.uuid
uuid_obj = DEFAULT_HASHER.uuid_obj
humanize = DEFAULT_HASHER.humanize
humanize_bytes = DEFAULT_HASHER.humanize_bytes
humanize_uuid = DEFAULT_HASHER.humanize_uuid
verify = DEFAULT_HASHER.verify
entropy_bits = DEFAULT_HASHER.entropy_bits
dehumanize = DEFAULT_HASHER.dehumanize
uuid5 = DEFAULT_HASHER.uuid5
uuid3 = DEFAULT_HASHER.uuid3
hash = DEFAULT_HASHER.hash


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_binary_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _resolve_namespace(ns_str: str) -> uuidlib.UUID:
    try:
        return uuidlib.UUID(ns_str)
    except ValueError:
        namespace = getattr(uuidlib, ns_str.upper(), None)
        if namespace is None:
            msg = f"Invalid namespace: {ns_str}"
            raise ValueError(msg)
        return namespace


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-readable UUID/hash tool")
    parser.add_argument("--uuid", action="store_true", help="Generate a human-readable UUID")
    parser.add_argument("--uuid5", nargs=2, metavar=("NAMESPACE", "NAME"), help="Generate a human-readable UUIDv5")
    parser.add_argument("--uuid3", nargs=2, metavar=("NAMESPACE", "NAME"), help="Generate a human-readable UUIDv3")
    parser.add_argument("--humanize", metavar="HEX", help="Humanize a hex digest")
    parser.add_argument("--humanize-bytes", metavar="TEXT", help="Humanize UTF-8 text")
    parser.add_argument("--verify", nargs="+", metavar="ARG", help="Verify a phrase against a hex digest")
    parser.add_argument("--dehumanize", metavar="PHRASE", help="Convert a phrase back to hex")
    parser.add_argument("--entropy", type=int, metavar="WORDS", help="Show entropy bits for given word count")
    parser.add_argument("--hash", metavar="TEXT", help="Hash text and humanize")
    parser.add_argument("--algorithm", default="sha256", help="Hash algorithm (default: sha256)")
    parser.add_argument("-f", "--file", metavar="PATH", help="Read input from file")
    parser.add_argument("--words", type=int, default=4, help="Number of words (default: 4)")
    parser.add_argument("--separator", default="-", help="Word separator (default: -)")
    parser.add_argument("--wordlist-file", help="Path to custom wordlist file")
    parser.add_argument("--count", type=int, default=1, help="Number of outputs")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--upper", action="store_true", help="Uppercase output")
    parser.add_argument("--title", action="store_true", help="Title case output")
    args = parser.parse_args()
    hasher = HumanHasher.from_file(args.wordlist_file) if args.wordlist_file else DEFAULT_HASHER

    def format_phrase(phrase: str) -> str:
        if args.upper:
            phrase = phrase.upper()
        elif args.title:
            phrase = phrase.title()
        return phrase

    file_text: Optional[str] = None
    file_bytes: Optional[bytes] = None
    if args.file is not None:
        try:
            file_bytes = _read_binary_file(args.file)
            try:
                file_text = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                file_text = None
        except OSError as e:
            print(f"error: cannot read file {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
    if args.file is not None and not args.hash and not args.humanize and not args.humanize_bytes and not args.verify:
        args.hash = file_text if file_text is not None else ""
    results: list[dict[str, Any]] = []
    for _ in range(args.count):
        if args.uuid:
            phrase, digest = hasher.uuid(words=args.words, separator=args.separator)
            results.append({"phrase": format_phrase(phrase), "digest": digest})
        elif args.uuid5:
            ns_str, name = args.uuid5
            namespace = _resolve_namespace(ns_str)
            phrase, digest = hasher.uuid5(namespace, name, words=args.words, separator=args.separator)
            results.append({"phrase": format_phrase(phrase), "digest": digest})
        elif args.uuid3:
            ns_str, name = args.uuid3
            namespace = _resolve_namespace(ns_str)
            phrase, digest = hasher.uuid3(namespace, name, words=args.words, separator=args.separator)
            results.append({"phrase": format_phrase(phrase), "digest": digest})
        elif args.humanize:
            hex_input = args.humanize
            if args.file is not None:
                if file_text is None:
                    print("error: file is not valid UTF-8 text", file=sys.stderr)
                    sys.exit(1)
                hex_input = file_text.strip()
            phrase = hasher.humanize(hex_input, words=args.words, separator=args.separator)
            results.append({"phrase": format_phrase(phrase), "digest": hex_input})
        elif args.humanize_bytes:
            if args.file is not None:
                data = file_bytes if file_bytes is not None else b""
            else:
                data = args.humanize_bytes.encode("utf-8")
            phrase = hasher.humanize_bytes(data, words=args.words, separator=args.separator)
            results.append({"phrase": format_phrase(phrase), "digest": data.hex()})
        elif args.verify:
            if args.file is not None:
                if file_text is None:
                    print("error: file is not valid UTF-8 text", file=sys.stderr)
                    sys.exit(1)
                if len(args.verify) != 1:
                    print("error: --verify with --file expects exactly one PHRASE argument", file=sys.stderr)
                    sys.exit(1)
                hexdigest = file_text.strip()
                phrase = args.verify[0]
            else:
                if len(args.verify) != 2:
                    print("error: --verify expects HEX and PHRASE, or use --file with PHRASE", file=sys.stderr)
                    sys.exit(1)
                hexdigest, phrase = args.verify
            ok = hasher.verify(hexdigest, phrase, words=args.words, separator=args.separator)
            results.append({"verified": ok})
        elif args.dehumanize:
            digest = hasher.dehumanize(args.dehumanize, separator=args.separator)
            results.append({"digest": digest})
        elif args.entropy is not None:
            bits = hasher.entropy_bits(args.entropy)
            results.append({"entropy_bits": bits})
        elif args.hash is not None:
            if args.file is not None:
                if file_bytes is None:
                    print("error: file could not be read", file=sys.stderr)
                    sys.exit(1)
                data: str | bytes = file_bytes
            else:
                data = args.hash
            phrase, digest = hasher.hash(
                data,
                algorithm=args.algorithm,
                words=args.words,
                separator=args.separator,
            )
            results.append({"phrase": format_phrase(phrase), "digest": digest})
        else:
            parser.print_help()
            return
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for res in results:
            if "phrase" in res:
                print(res["phrase"])
            elif "verified" in res:
                print("verified" if res["verified"] else "not verified")
            elif "digest" in res:
                print(res["digest"])
            elif "entropy_bits" in res:
                print(res["entropy_bits"])


if __name__ == "__main__":
    main()
