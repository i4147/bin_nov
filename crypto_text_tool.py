#!/data/data/com.termux/files/usr/bin/env python
"""File/text ciphers: AES files, Morse code, and a ZIP password tester.
Analysis / deduplication ------------------------ - ``decryptor.py`` and ``encryptor.py`` are the same AES-CBC program (one walks files with the author's ``fastwalk`` package, the other ``Path.glob``; both dangerously rewrote *every* file in the CWD).
They are now explicit ``aes-encrypt`` / ``aes-decrypt`` commands over the files you actually name.
``AES_BLOCK_SIZE=128`` in one original was a bug (AES-CBC IVs are 16 bytes); the on-disk format is unchanged, so old files still decrypt.
Keys are derived from the passphrase string directly (must be 16/24/32 bytes as before; a generated key is printed once when encrypting without ``--key``).
- ``morseconverter.py`` -> ``morse`` (text or files).
- ``zip_crack.py`` (which did not parse: an f-string was split across lines and the dataclass fields were never annotated) -> ``zip-crack``, stdlib-only, sequential-with-optional-process-pool.
AES needs the third-party ``cryptography`` package; import is lazy.
Examples -------- python3 crypto_text_tool.py aes-encrypt secret.txt python3 crypto_text_tool.py aes-decrypt secret.txt.enc --key <KEY> python3 crypto_text_tool.py morse encode "SOS" python3 crypto_text_tool.py morse decode "...
--- ..." python3 crypto_text_tool.py zip-crack secret.zip -w wordlist.txt"""

import argparse
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import islice
import os
from pathlib import Path
import secrets
import string
import sys
import time
import zipfile

AES_BLOCK_SIZE: int = 16
KEY_ALPHABET: str = string.ascii_letters + string.digits
DEFAULT_BATCH: int = 2000

MORSE: dict[str, str] = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    "0": "-----",
    " ": "/",
}
MORSE_REVERSE: dict[str, str] = {code: char for char, code in MORSE.items()}


# ------------------------------------------------------------------- AES
def random_key(length: int = 32) -> str:
    """Generate a random alphanumeric key (32 chars = AES-256)."""
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(length))


def aes_key_bytes(key: str) -> bytes:
    """Validate and encode an AES key string."""
    raw: bytes = key.encode()
    if len(raw) not in (16, 24, 32):
        msg = f"Error: key must be 16, 24 or 32 bytes (got {len(raw)}); omit --key to generate one"
        raise SystemExit(msg)
    return raw


def load_crypto() -> tuple[object, object, object]:
    """Lazy-import (Cipher, padding-module, modes-module) helpers."""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        msg = "Missing optional dependency 'cryptography'. Install: pip install cryptography"
        raise SystemExit(msg) from None
    return (Cipher, algorithms, modes), padding, modes  # type: ignore[return-value]


def aes_encrypt_blob(data: bytes, key: str) -> bytes:
    """Return ``iv || AES-CBC(PKCS7(data))``."""
    (cipher_cls, algorithms, modes), padding, _ = load_crypto()  # type: ignore[misc]
    iv: bytes = os.urandom(AES_BLOCK_SIZE)
    cipher = cipher_cls(algorithms.AES(aes_key_bytes(key)), modes.CBC(iv))  # type: ignore[attr-defined]
    padder = padding.PKCS7(128).padder()  # type: ignore[attr-defined]
    padded: bytes = padder.update(data) + padder.finalize()
    encryptor = cipher.encryptor()
    return iv + encryptor.update(padded) + encryptor.finalize()


def aes_decrypt_blob(blob: bytes, key: str) -> bytes:
    """Inverse of :func:`aes_encrypt_blob`."""
    (cipher_cls, algorithms, modes), padding, _ = load_crypto()  # type: ignore[misc]
    iv, ciphertext = blob[:AES_BLOCK_SIZE], blob[AES_BLOCK_SIZE:]
    cipher = cipher_cls(algorithms.AES(aes_key_bytes(key)), modes.CBC(iv))  # type: ignore[attr-defined]
    decryptor = cipher.decryptor()
    padded: bytes = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()  # type: ignore[attr-defined]
    return unpadder.update(padded) + unpadder.finalize()


def cmd_aes(args: argparse.Namespace) -> int:
    """Encrypt or decrypt each named file (never the whole directory)."""
    key: str = args.key or (random_key() if args.command == "aes-encrypt" else "")
    if not key:
        print("Error: --key is required for decryption.", file=sys.stderr)
        return 2
    if args.key is None and args.command == "aes-encrypt":
        print(f"Generated key (save it!): {key}")
    failed: int = 0
    for path in args.files:
        if not path.is_file():
            print(f"skip (not a file): {path}")
            continue
        try:
            if args.command == "aes-encrypt":
                output: Path = path if args.in_place else path.with_suffix(path.suffix + ".enc")
                output.write_bytes(aes_encrypt_blob(path.read_bytes(), key))
            else:
                output = path if args.in_place else Path(str(path).removesuffix(".enc") or str(path) + ".dec")
                output.write_bytes(aes_decrypt_blob(path.read_bytes(), key))
            print(f"{path} -> {output}")
        except Exception as exc:  # ValueError from bad padding, OSError, ...
            print(f"failed: {path}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


# ----------------------------------------------------------------- Morse
def text_to_morse(text: str) -> str:
    """Encode text as Morse (unknown characters pass through)."""
    return " ".join(MORSE.get(char, char) for char in text.upper())


def morse_to_text(morse: str) -> str:
    """Decode Morse (``/`` = space; unknown codes pass through)."""
    return "".join(MORSE_REVERSE.get(code, code) for code in morse.split(" ") if code).strip()


def cmd_morse(args: argparse.Namespace) -> int:
    """Run the ``morse`` subcommand."""
    source: str = args.text
    if args.input is not None:
        source = args.input.read_text(encoding="utf-8")
    result: str = text_to_morse(source) if args.mode == "encode" else morse_to_text(source)
    if args.output is not None:
        args.output.write_text(result, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(result)
    return 0


# ------------------------------------------------------------- zip crack
def try_batch(job: tuple[Path, list[str]]) -> tuple[str | None, int]:
    """Test a batch of passwords against the first zip member."""
    zip_path, passwords = job
    tested: int = 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            first: str = archive.filelist[0].filename
            for password in passwords:
                tested += 1
                try:
                    with archive.open(first, pwd=password.encode()) as handle:
                        handle.read(1)
                    return password, tested
                except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError):
                    continue
    except OSError:
        pass
    return None, tested


def wordlist_batches(path: Path, batch_size: int) -> Iterator[list[str]]:
    """Yield non-empty stripped passwords in batches."""
    with path.open(encoding="utf-8", errors="ignore") as handle:
        while batch := [line.strip() for line in islice(handle, batch_size) if line.strip()]:
            yield batch


def cmd_zip_crack(args: argparse.Namespace) -> int:
    """Dictionary-attack a password-protected zip file."""
    if not args.zip_file.is_file() or not args.wordlist.is_file():
        print("Error: zip file and wordlist must both exist.", file=sys.stderr)
        return 1
    with zipfile.ZipFile(args.zip_file) as archive:
        if not any(info.flag_bits & 1 for info in archive.infolist()):
            print("Warning: zip does not look password-protected.")
    started: float = time.time()
    tested_total: int = 0
    found: str | None = None
    jobs: list[tuple[Path, list[str]]] = [(args.zip_file, b) for b in wordlist_batches(args.wordlist, args.batch_size)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(try_batch, job) for job in jobs]
        for future in as_completed(futures):
            password, tested = future.result()
            tested_total += tested
            if password is not None:
                found = password
                for other in futures:
                    other.cancel()
                break
    elapsed: float = time.time() - started
    print(f"Tested {tested_total:,} passwords in {elapsed:.1f}s ({tested_total / max(elapsed, 0.001):.0f}/s)")
    if found is not None:
        print(f"SUCCESS: password is {found!r}")
        return 0
    print("FAILED: password not in wordlist.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="AES files, Morse code, ZIP password tester.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("aes-encrypt", "aes-decrypt"):
        child = sub.add_parser(name, help=f"{name} files with AES-CBC")
        child.add_argument("files", nargs="+", type=Path, help="Files to process (explicit list)")
        child.add_argument("--key", default=None, help="16/24/32-byte key (generated for encrypt if omitted)")
        child.add_argument("--in-place", action="store_true", help="Overwrite inputs (default: add/drop .enc)")
        child.set_defaults(handler=cmd_aes)
    mo = sub.add_parser("morse", help="Encode/decode Morse code")
    mo.add_argument("mode", choices=("encode", "decode"))
    mo.add_argument("text", nargs="?", default="", help="Text/Morse string (or use -i FILE)")
    mo.add_argument("-i", "--input", type=Path, default=None, help="Read input from file")
    mo.add_argument("-o", "--output", type=Path, default=None, help="Write result to file")
    mo.set_defaults(handler=cmd_morse)
    zc = sub.add_parser("zip-crack", help="Test a wordlist against a zip password")
    zc.add_argument("zip_file", type=Path)
    zc.add_argument("-w", "--wordlist", type=Path, default=Path("wordlist.txt"))
    zc.add_argument("-j", "--workers", type=int, default=os.cpu_count() or 1)
    zc.add_argument("-b", "--batch-size", type=int, default=DEFAULT_BATCH)
    zc.set_defaults(handler=cmd_zip_crack)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
