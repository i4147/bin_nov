#!/data/data/com.termux/files/usr/bin/env python
"""JSON and text conversion toolkit.
Subcommands: ``custom2json``/``ss2json`` parse the custom delimited dumps, ``freeze2json`` parses pip freeze, ``jsonl2dict``/``jsonl2json`` convert the JSONL files (per object or per directory), ``lowerkeys`` lowercases the keys, ``merge-json``/``mergejson`` combine the documents, ``mime2json`` wraps a MIME file and ``sortdict``/``tojson`` normalise the output.
Usage: jsontools {custom2json,freeze2json,jsonl2dict,jsonl2json,lowerkeys,merge-json,mime2json,sortdict,tojson} [options] Positional arguments: file output inputs ...
directory csv delimiter Subcommands: custom2json freeze2json jsonl2dict jsonl2json lowerkeys merge-json mergejson mime2json sortdict ss2json tojson Options: --input --output --key -o, --output-dir --indent --ensure-ascii --skip-invalid --overwrite -o, --output --score-column ...
plus 1 more; run ``jsontools --help`` for the full list.
Examples: $ jsontools jsonl2json data.jsonl -o out/ $ jsontools lowerkeys data.json Command-line script: run ``python jsontools.py`` or the ``jsontools`` command.
The module is importable as well."""

import argparse
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import random
import re
import sys
import tempfile
from typing import Any, Iterator, Optional, Sequence

from dh import MAX_WORKERS

_CUSTOM_RULE_RE = re.compile(r"^(?:(\d+?)>)?(\d+)=")


def _custom_hex(b: bytes) -> str:
    return b.hex().upper()


def _custom_parse_rule(line: str) -> Optional[dict[str, Any]]:
    m = _CUSTOM_RULE_RE.match(line)
    if not m:
        return None
    rule_index = int(m.group(1)) if m.group(1) else None
    offset = int(m.group(2))
    tail = line[m.end() :]
    try:
        raw = tail.encode("latin-1")
    except UnicodeEncodeError:
        raw = tail.encode("latin-1", errors="replace")
    result: dict[str, Any] = {
        "rule_index": rule_index,
        "offset": offset,
        "value_bytes": raw,
        "hex": _custom_hex(raw),
    }
    return result


def _custom_load(path: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    section: Optional[str] = None
    lines = path.read_bytes().splitlines()
    for raw in lines:
        line = raw.decode("latin-1", errors="replace").rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            out.setdefault(section, [])
            continue
        if line.startswith(("#", "!")):
            continue
        if section is None:
            print(f"Warning: rule outside section: {line!r}", file=sys.stderr)
            continue
        parsed = _custom_parse_rule(line)
        if parsed:
            entry = {
                "offset": parsed["offset"],
                "value_hex": parsed["hex"],
                "length": len(parsed["value_bytes"]),
            }
            if parsed["rule_index"] is not None:
                entry["rule_index"] = parsed["rule_index"]
            out[section].append(entry)
        else:
            print(f"Warning: Failed to parse rule: {line!r}", file=sys.stderr)
    return out


def cmd_custom2json(args: argparse.Namespace) -> int:
    data = _custom_load(Path(args.file))
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"✅ Written to {args.output}")
    else:
        print(text)
    return 0


def cmd_freeze2json(args: argparse.Namespace) -> int:
    src = Path(args.input)
    dst = Path(args.output)
    result: dict[str, str] = {}
    with src.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "==" in line:
                name, ver = line.split("==", 1)
                result[name] = ver
    with dst.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)
    print(f"Saved {len(result)} packages to {dst}")
    return 0


def _jsonl_load_list(path: Path) -> list[Any]:
    out: list[Any] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"Skipping line due to JSON decode error: {exc}")
    return out


def _jsonl_load_dict(path: Path, key: str) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                if key in obj:
                    out[obj[key]] = obj
                else:
                    print(f"Skipping line: Key field {key} not found.")
            except json.JSONDecodeError as exc:
                print(f"Skipping line due to JSON decode error: {exc}")
    return out


def cmd_jsonl2dict(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if args.key:
        data: Any = _jsonl_load_dict(src, args.key)
    else:
        data = _jsonl_load_list(src)
    print(data)
    if args.output:
        dst = Path(args.output)
    else:
        dst = src.with_suffix(".json")
    with dst.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return 0


_JSONL_SUFFIXES: set[str] = {".jsonl", ".ndjson"}


def _is_jsonl(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _JSONL_SUFFIXES


def _discover_jsonl(inputs: Sequence[Path]) -> Iterator[Path]:
    seen: set = set()
    for p in inputs:
        try:
            path = p.resolve(strict=True)
        except FileNotFoundError:
            print(f"warning: input does not exist: {p}", file=sys.stderr)
            continue
        except OSError as exc:
            print(f"warning: cannot access {p}: {exc}", file=sys.stderr)
            continue
        if _is_jsonl(path):
            if path not in seen:
                seen.add(path)
                yield path
            continue
        if path.is_dir():
            try:
                for child in path.rglob("*"):
                    if not _is_jsonl(child):
                        continue
                    resolved = child.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield resolved
            except OSError as exc:
                print(f"warning: cannot traverse {path}: {exc}", file=sys.stderr)
            continue
        print(
            f"warning: skipping unsupported input (expected file or directory): {path}",
            file=sys.stderr,
        )


def _jsonl2json_destination(
    source: Path,
    output_dir: Optional[Path],
    roots: Sequence[Path],
) -> Path:
    name = source.with_suffix(".json").name
    if output_dir is None:
        return source.with_name(name)
    matching: list[Path] = []
    for root in roots:
        if root.is_dir():
            try:
                source.relative_to(root)
            except ValueError:
                continue
            matching.append(root)
    if matching:
        deepest = max(matching, key=lambda item: len(item.parts))
        rel = source.relative_to(deepest).parent
        return output_dir / rel / name
    return output_dir / name


def _jsonl_dumps(obj: Any, indent: Optional[int], ensure_ascii: bool) -> str:
    return json.dumps(
        obj,
        indent=indent,
        ensure_ascii=ensure_ascii,
        separators=None if indent is not None else (",", ":"),
    )


def _convert_one(job: argparse.Namespace) -> dict[str, Any]:
    source: Path = job.source
    destination: Path = job.destination
    if destination.exists() and not job.overwrite:
        return {
            "source": source,
            "destination": destination,
            "records": 0,
            "empty": 0,
            "invalid": 0,
            "error": f"destination exists (use --overwrite): {destination}",
        }
    tmp_path: Optional[Path] = None
    records = empty = invalid = 0
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            text=True,
        )
        tmp_path = Path(tmp_name)
        with (
            os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out,
            source.open("r", encoding="utf-8-sig", newline=None) as src,
        ):
            out.write("[")
            for lineno, line in enumerate(src, start=1):
                stripped = line.strip()
                if not stripped:
                    empty += 1
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if job.skip_invalid:
                        invalid += 1
                        continue
                    msg = f"{source}:{lineno}: invalid JSON: {exc.msg}"
                    raise ValueError(msg) from exc
                if records:
                    out.write(",")
                if job.indent is not None:
                    out.write("\n")
                out.write(_jsonl_dumps(obj, job.indent, job.ensure_ascii))
                records += 1
            if job.indent is not None and records:
                out.write("\n")
            out.write("]\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, destination)
        tmp_path = None
        return {
            "source": source,
            "destination": destination,
            "records": records,
            "empty": empty,
            "invalid": invalid,
            "error": None,
        }
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "source": source,
            "destination": destination,
            "records": records,
            "empty": empty,
            "invalid": invalid,
            "error": str(exc),
        }
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def cmd_jsonl2json(args: argparse.Namespace) -> int:
    if args.indent is not None and args.indent < 0:
        print("error: --indent must be zero or greater", file=sys.stderr)
        return 2
    inputs = [Path(p) for p in args.inputs] if args.inputs else [Path.cwd()]
    roots: list[Path] = []
    for p in inputs:
        with contextlib.suppress(FileNotFoundError, OSError):
            roots.append(p.resolve(strict=True))
    output_dir = Path(args.output_dir) if args.output_dir else None

    def _jobs() -> Iterator[argparse.Namespace]:
        seen_dest: set = set()
        for src in _discover_jsonl(inputs):
            dst = _jsonl2json_destination(src, output_dir, roots).resolve()
            if dst in seen_dest:
                print(
                    f"warning: skipping {src}; output collision at {dst}",
                    file=sys.stderr,
                )
                continue
            seen_dest.add(dst)
            ns = argparse.Namespace(
                source=src,
                destination=dst,
                indent=args.indent,
                ensure_ascii=args.ensure_ascii,
                skip_invalid=args.skip_invalid,
                overwrite=args.overwrite,
            )
            yield ns

    total = 0
    written = 0
    invalid = 0
    failures = 0
    with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
        for res in pool.imap_unordered(_convert_one, _jobs(), chunksize=1):
            total += 1
            written += res["records"]
            invalid += res["invalid"]
            if res["error"]:
                failures += 1
                print(
                    f"FAILED  {res['source']} -> {res['destination']}\n        {res['error']}",
                    file=sys.stderr,
                )
            else:
                extra = f", skipped invalid lines: {res['invalid']}" if res["invalid"] else ""
                print(f"OK      {res['source']} -> {res['destination']} ({res['records']} records{extra})")
    print(
        f"\nFinished: {total} file(s), {written} record(s), {invalid} invalid line(s) skipped, {failures} failure(s).",
        file=sys.stderr,
    )
    return 1 if failures else 0


def cmd_lowerkeys(args: argparse.Namespace) -> int:
    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    lowered = {str(k).lower(): v for k, v in data.items()}
    path.write_text(
        json.dumps(lowered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Successfully updated {path}")
    return 0


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _load_json_as_list(path: Path) -> list[Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    except Exception:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]


def _collect_json_files(paths: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for entry in paths:
        p = Path(entry)
        if p.is_file() and p.suffix == ".json":
            files.append(p)
        elif p.is_dir():
            files.extend(p.rglob("*.json"))
        else:
            print(f"no json file: {entry}")
    return files


def cmd_merge_json(args: argparse.Namespace) -> int:
    paths = args.inputs or ["."]
    files = _collect_json_files(paths)
    if not files:
        print("There is no data to write to the output file.")
        return 0
    with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
        chunks = pool.map(_load_json_as_list, files)
    merged: list[Any] = []
    for chunk in chunks:
        merged.extend(chunk)
    if merged:
        out = Path(args.output)
        if out.exists():
            out = _unique_path(out)
        try:
            out.write_text(
                json.dumps(merged, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"error: {exc}")
            return 1
    else:
        print("There is no data to write to the output file.")
    return 0


def _deep_merge(left: Any, right: Any) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    merged = left.copy()
    for k, v in right.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def cmd_mergejson(args: argparse.Namespace) -> int:
    inputs = args.inputs
    if len(inputs) < 2:
        print("Error: Please provide at least two input files to merge.")
        return 1
    acc: Any = None
    first = inputs[0]
    for path_str in inputs:
        path = Path(path_str)
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"Error: File '{path}' not found.")
            return 1
        except json.JSONDecodeError:
            print(f"Error: File '{path}' contains invalid JSON.")
            return 1
        if acc is None:
            acc = obj
            continue
        if type(acc) is not type(obj):
            print(
                f"Error: Type mismatch. '{first}' is a {type(acc).__name__}, "
                f"but '{path}' is a {type(obj).__name__}. Cannot merge."
            )
            return 1
        if isinstance(acc, list):
            acc.extend(obj)
        elif isinstance(acc, dict):
            acc = _deep_merge(acc, obj)
        else:
            print(f"Error: Unsupported top-level JSON type: {type(obj).__name__}")
            return 1
    try:
        Path(args.output).write_text(
            json.dumps(acc, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Successfully merged {len(inputs)} files into '{args.output}'.")
    except OSError as exc:
        print(f"Error writing to output file: {exc}")
        return 1
    return 0


def _mime_extract(node: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        if "mime-type" in node:
            info = node["mime-type"]
            if isinstance(info, dict):
                mime = info.get("@type")
                glob = info.get("glob")
                globs: list[Any] = []
                if isinstance(glob, dict):
                    globs = [glob]
                elif isinstance(glob, list):
                    globs = glob
                for g in globs:
                    if isinstance(g, dict):
                        pattern = g.get("@pattern", "")
                        if pattern.startswith("*.") and len(pattern) > 2:
                            ext = "." + pattern[2:]
                            if mime:
                                out.append((mime, ext))
        for v in node.values():
            out.extend(_mime_extract(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_mime_extract(item))
    return out


def cmd_mime2json(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    output = Path(args.output)
    bucket: dict[str, set] = {}
    for path in root.rglob("*.json"):
        if path.resolve() == output.resolve():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for mime, ext in _mime_extract(data):
            bucket.setdefault(mime, set()).add(ext)
    result = {k: sorted(v) for k, v in sorted(bucket.items())}
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Saved {len(result)} MIME entries to {output}")
    return 0


def _sortdict_key(line: str) -> str:
    if ":" in line:
        value = line.split(":", 1)[1].strip()
        print(value)
        return value
    if "=" in line:
        value = line.split("=", 1)[1].strip()
        print(value)
        return value
    return line


def cmd_sortdict(args: argparse.Namespace) -> int:
    path = Path(args.file)
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    filtered = [ln for ln in lines if ":" in ln or "=" in ln]
    filtered.sort(key=_sortdict_key)
    with path.open("w", encoding="utf-8") as f:
        f.writelines(filtered)
    return 0


def cmd_ss2json(args: argparse.Namespace) -> int:
    import pandas as pd

    src = Path(args.csv)
    df = pd.read_csv(str(src))
    sorted_df = df.sort_values(by=args.score_column, ascending=False)
    dst = src.with_suffix(".json")
    sorted_df.to_json(str(dst))
    return 0


def _tojson_parse(path: Path, delimiter: str) -> dict[str, int]:
    result: dict[str, int] = {}
    seen_keys: set = set()
    seen_values: set = set()
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if delimiter not in line:
                print(
                    f"Warning: Line {lineno} doesn't contain delimiter '{delimiter}': {line!r}",
                    file=sys.stderr,
                )
                continue
            key, raw_value = line.split(delimiter, 1)
            key = key.strip()
            value = int(raw_value.lstrip().strip())
            if key in seen_keys:
                print(f"repeated key: {key}")
            else:
                seen_keys.add(key)
            if value in seen_values:
                while True:
                    value = random.randint(0, 22704)
                    if value not in seen_values:
                        break
                    print("repeated random")
            seen_values.add(value)
            result[key] = value
    return result


def cmd_tojson(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if not src.is_file():
        print(f"Error: File '{src}' not found.", file=sys.stderr)
        return 1
    try:
        data = _tojson_parse(src, args.delimiter)
    except Exception as exc:
        print(f"Error reading file: {exc}", file=sys.stderr)
        return 1
    words_path = Path(args.words)
    with words_path.open("w", encoding="utf-8") as f:
        for key in data:
            f.write(f"{key}\n")
    dst = src.with_suffix(".json")
    with dst.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merged JSON/text conversion toolkit",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("custom2json")
    p.add_argument("file")
    p.add_argument("output", nargs="?", default=None)
    p.set_defaults(func=cmd_custom2json)
    p = sub.add_parser("freeze2json")
    p.add_argument("--input", default="pip.freeze")
    p.add_argument("--output", default="packages.json")
    p.set_defaults(func=cmd_freeze2json)
    p = sub.add_parser("jsonl2dict")
    p.add_argument("file")
    p.add_argument("--key", default=None)
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_jsonl2dict)
    p = sub.add_parser("jsonl2json")
    p.add_argument("inputs", nargs="*")
    p.add_argument("-o", "--output-dir", default=None)
    p.add_argument("--indent", type=int, default=None)
    p.add_argument("--ensure-ascii", action="store_true")
    p.add_argument("--skip-invalid", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_jsonl2json)
    p = sub.add_parser("lowerkeys")
    p.add_argument("file")
    p.set_defaults(func=cmd_lowerkeys)
    p = sub.add_parser("merge-json")
    p.add_argument("inputs", nargs="*")
    p.add_argument("-o", "--output", default="merged.json")
    p.set_defaults(func=cmd_merge_json)
    p = sub.add_parser("mergejson")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_mergejson)
    p = sub.add_parser("mime2json")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("--output", default="mime_to_ext.json")
    p.set_defaults(func=cmd_mime2json)
    p = sub.add_parser("sortdict")
    p.add_argument("file")
    p.set_defaults(func=cmd_sortdict)
    p = sub.add_parser("ss2json")
    p.add_argument("csv")
    p.add_argument("--score-column", default="score")
    p.set_defaults(func=cmd_ss2json)
    p = sub.add_parser("tojson")
    p.add_argument("file")
    p.add_argument("delimiter")
    p.add_argument("--words", default="words")
    p.set_defaults(func=cmd_tojson)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
