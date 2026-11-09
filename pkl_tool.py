#!/data/data/com.termux/files/usr/bin/env python
"""Convert, inspect or dump pickle files.
The ``to-json`` subcommand serialises the payload (bytes as base64, the non-JSON types as strings), ``inspect`` describes the structure and the types with a ``--max-repr`` preview and ``to-raw`` writes a bytes payload verbatim.
Usage: pkl_tool {to-json,inspect,to-raw} FILE.pkl [-o OUT] [--stdout] Positional arguments: files ...
file Subcommands: to-json Convert .pkl to .json inspect Describe a pickle's payload to-raw Write a bytes payload verbatim Options: -o, --output Single-file output --indent --stdout --max-repr --force-repr Non-bytes: write repr() as text Examples: $ pkl_tool inspect model.pkl Unpickling executes code; only open files you trust."""

import argparse
import base64
from collections.abc import Sequence
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import pickle
import sys


JSON_NATIVE: tuple[type, ...] = (str, int, float, bool, type(None))


def warn_unpickle(path: Path) -> None:
    print(f"warning: unpickling {path} executes embedded code - trusted files only!", file=sys.stderr)


def load_pickle(path: Path) -> object:
    warn_unpickle(path)
    with path.open("rb") as handle:
        return pickle.load(handle)


def serialize(obj: object) -> object:
    if isinstance(obj, JSON_NATIVE):
        return obj
    if isinstance(obj, dict):
        return {stringify_key(k): serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(item) for item in obj]
    if isinstance(obj, (set, frozenset)):
        return {"__type__": type(obj).__name__, "items": [serialize(i) for i in obj]}
    if isinstance(obj, (bytes, bytearray)):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(bytes(obj)).decode(),
            "length": len(obj),
        }
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return {"__type__": type(obj).__name__, "iso": obj.isoformat()}
    if isinstance(obj, Path):
        return {"__type__": "Path", "value": str(obj)}
    if isinstance(obj, Decimal):
        return {"__type__": "Decimal", "value": str(obj)}
    if hasattr(obj, "__dict__"):
        return {"__type__": type(obj).__name__, "attrs": {str(k): serialize(v) for k, v in vars(obj).items()}}
    return {"__type__": type(obj).__name__, "repr": repr(obj)}


def stringify_key(key: object) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, (int, float, bool)) or key is None:
        return json.dumps(key)
    return f"{type(key).__name__}:{key!r}"


def cmd_to_json(args: argparse.Namespace) -> int:
    for path in args.files:
        try:
            data: object = load_pickle(path)
        except Exception as exc:
            print(f"failed: {path}: {exc}", file=sys.stderr)
            return 1
        payload: object = serialize(data)
        text: str = json.dumps(payload, indent=args.indent, ensure_ascii=False) + "\n"
        if args.stdout:
            print(text, end="")
        else:
            dest: Path = args.output if args.output and len(args.files) == 1 else path.with_suffix(".json")
            dest.write_text(text, encoding="utf-8")
            print(f"{path} -> {dest}")
    return 0


def describe(obj: object, depth: int = 0, max_depth: int = 2) -> str:
    if isinstance(obj, dict):
        keys: list[object] = list(obj.keys())[:5]
        return f"dict(len={len(obj)}, keys={keys!r})"
    if isinstance(obj, (list, tuple, set, frozenset)):
        return f"{type(obj).__name__}(len={len(obj)})"
    if isinstance(obj, (bytes, bytearray)):
        return f"bytes(len={len(obj)})"
    if isinstance(obj, str):
        return f"str(len={len(obj)})"
    return type(obj).__name__


def cmd_inspect(args: argparse.Namespace) -> int:
    obj: object = load_pickle(args.file)
    print(f"file:   {args.file} ({args.file.stat().st_size} bytes on disk)")
    print(f"type:   {type(obj).__module__}.{type(obj).__qualname__}")
    print(f"shape:  {describe(obj)}")
    text: str = repr(obj)
    if len(text) > args.max_repr:
        text = text[: args.max_repr] + f"... [truncated, {len(text)} chars total]"
    print(f"repr:   {text}")
    return 0


def cmd_to_raw(args: argparse.Namespace) -> int:
    obj: object = load_pickle(args.file)
    dest: Path = args.output or args.file.with_suffix(".raw")
    if isinstance(obj, (bytes, bytearray)):
        dest.write_bytes(bytes(obj))
        print(f"Wrote {len(obj)} raw byte(s) to {dest}")
    elif args.force_repr:
        dest.write_text(repr(obj), encoding="utf-8")
        print(f"Wrote repr to {dest}")
    else:
        print(f"Payload is {describe(obj)}, not bytes. Use inspect/to-json, or --force-repr.", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pickle -> JSON / inspect / raw (trusted files only!).")
    sub = parser.add_subparsers(dest="command", required=True)
    tj = sub.add_parser("to-json", help="Convert .pkl to .json")
    tj.add_argument("files", nargs="+", type=Path)
    tj.add_argument("-o", "--output", type=Path, default=None, help="Single-file output")
    tj.add_argument("--indent", type=int, default=2)
    tj.add_argument("--stdout", action="store_true")
    tj.set_defaults(handler=cmd_to_json)
    ins = sub.add_parser("inspect", help="Describe a pickle's payload")
    ins.add_argument("file", type=Path)
    ins.add_argument("--max-repr", type=int, default=500)
    ins.set_defaults(handler=cmd_inspect)
    raw = sub.add_parser("to-raw", help="Write a bytes payload verbatim")
    raw.add_argument("file", type=Path)
    raw.add_argument("-o", "--output", type=Path, default=None, help="Default: <name>.raw")
    raw.add_argument("--force-repr", action="store_true", help="Non-bytes: write repr() as text")
    raw.set_defaults(handler=cmd_to_raw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
