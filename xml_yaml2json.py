#!/data/data/com.termux/files/usr/bin/env python
"""Convert XML, YAML and TOML configuration files to JSON."""

import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
from typing import Any, Optional

from dh import MAX_WORKERS


try:
    import toml
except ImportError:
    toml = None
try:
    import xmltodict
except ImportError:
    xmltodict = None
try:
    from defusedxml.ElementTree import parse as defused_parse
except ImportError:
    defused_parse = None
try:
    import yaml
except ImportError:
    yaml = None


def write_json_file(
    data: Any,
    output_path: Path,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys)


def get_files_in_dir(directory: Path, extensions: list[str]) -> list[Path]:
    files = []
    for ext in extensions:
        files.extend(directory.rglob(f"*{ext}"))
    return files


def process_toml(filepath: Path) -> None:
    if toml is None:
        print("Error: 'toml' package is required. Run 'pip install toml'", file=sys.stderr)
        sys.exit(1)
    try:
        with open(filepath, encoding="utf-8") as f:
            data = toml.load(f)
        out_path = filepath.with_suffix(".json")
        write_json_file(data, out_path)
        print(f"{filepath} -> {out_path}")
    except FileNotFoundError:
        print(f"Error: The file '{filepath}' was not found.", file=sys.stderr)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)


def _element_to_dict_recursive(element: Any) -> dict[str, Any]:
    node_dict: dict[str, Any] = {element.tag: {} if element.attrib else None}
    children = list(element)
    if children:
        child_accumulator: dict[str, Any] = {}
        for child in children:
            child_parsed = _element_to_dict_recursive(child)
            for k, v in child_parsed.items():
                if k in child_accumulator:
                    if not isinstance(child_accumulator[k], list):
                        child_accumulator[k] = [child_accumulator[k]]
                    child_accumulator[k].append(v)
                else:
                    child_accumulator[k] = v
        node_dict = {element.tag: child_accumulator}
    if element.attrib:
        if node_dict[element.tag] is None:
            node_dict[element.tag] = {}
        node_dict[element.tag].update({"@attributes": element.attrib})
    if element.text and element.text.strip():
        if node_dict[element.tag] is None:
            node_dict[element.tag] = element.text.strip()
        elif isinstance(node_dict[element.tag], dict):
            node_dict[element.tag]["#text"] = element.text.strip()
    return node_dict


def process_xml_file(filepath: Path, engine: str, delete_source: bool) -> None:
    out_path = filepath.with_suffix(".json")
    try:
        if engine == "xmltodict":
            if xmltodict is None:
                msg = "xmltodict is not installed."
                raise ImportError(msg)
            xml_text = filepath.read_text(encoding="utf-8", errors="ignore")
            data = xmltodict.parse(xml_text)
            write_json_file(data, out_path)
            print(f"{out_path} created.", file=sys.stdout)
            if delete_source and filepath.suffix.lower() == ".xml":
                filepath.unlink()
        elif engine == "defusedxml":
            if defused_parse is None:
                msg = "defusedxml is not installed."
                raise ImportError(msg)
            tree = defused_parse(str(filepath))
            root = tree.getroot()
            data = _element_to_dict_recursive(root)
            write_json_file(data, out_path)
            print(f"Successfully converted '{filepath}' to '{out_path}'", file=sys.stdout)
    except OSError as e:
        print(f"error {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error parsing XML file '{filepath}': {e}", file=sys.stderr)


def convert_yaml_to_json_str(
    yaml_str: str,
    indent: Optional[int] = None,
    compact: bool = False,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    strict: bool = False,
) -> str:
    try:
        if strict:
            loader = yaml.SafeLoader
            data = yaml.load(yaml_str, Loader=loader)
        else:
            data = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        msg = f"YAML parsing error: {e}"
        raise yaml.YAMLError(msg) from e
    try:
        json.dumps(data, ensure_ascii=ensure_ascii, allow_nan=False)
    except (TypeError, ValueError) as e:
        msg = f"Data cannot be serialized to JSON: {e}"
        raise ValueError(msg) from e
    separators = (",", ":") if compact else None
    actual_indent = None if compact else indent
    try:
        return json.dumps(
            data,
            indent=actual_indent,
            separators=separators,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
            allow_nan=False,
        )
    except (TypeError, ValueError) as e:
        msg = f"JSON serialization error: {e}"
        raise ValueError(msg) from e


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert configuration and data files to JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    toml_parser = subparsers.add_parser("toml", help="Convert TOML files to JSON")
    toml_parser.add_argument("inputs", nargs="+", type=Path, help="Input TOML file(s)")
    xml_parser = subparsers.add_parser("xml", help="Convert XML files to JSON")
    xml_parser.add_argument("inputs", nargs="*", type=Path, help="Input XML file(s) or directories")
    xml_parser.add_argument(
        "--engine",
        choices=["xmltodict", "defusedxml"],
        default="xmltodict",
        help="XML parsing engine (xmltodict for batch/standard, defusedxml for strict manual parsing)",
    )
    xml_parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete the source .xml file after successful conversion (original xml2json.py behavior)",
    )
    xml_parser.add_argument(
        "--exts",
        nargs="+",
        default=[".xml", ".svg"],
        help="Extensions to process if scanning a directory (default: .xml .svg)",
    )
    yaml_parser = subparsers.add_parser("yaml", help="Convert YAML files/streams to JSON")
    yaml_in_out = yaml_parser.add_argument_group("Input/Output")
    yaml_in_out.add_argument(
        "input",
        nargs="?",
        type=argparse.FileType("r", encoding="utf-8"),
        default=sys.stdin,
        help="Input YAML file (default: stdin)",
    )
    yaml_in_out.add_argument(
        "-o",
        "--output",
        type=argparse.FileType("w", encoding="utf-8"),
        default=sys.stdout,
        help="Output JSON file (default: stdout)",
    )
    yaml_fmt = yaml_parser.add_argument_group("Formatting")
    yaml_fmt.add_argument(
        "-i",
        "--indent",
        type=int,
        default=2,
        help="Indentation spaces for pretty printing (default: 2, use 0 for compact)",
    )
    yaml_fmt.add_argument(
        "-c",
        "--compact",
        action="store_true",
        help="Compact output (minified, overrides --indent)",
    )
    yaml_fmt.add_argument(
        "-s",
        "--sort-keys",
        action="store_true",
        help="Sort dictionary keys alphabetically",
    )
    yaml_enc = yaml_parser.add_argument_group("Encoding")
    yaml_enc.add_argument(
        "--no-ensure-ascii",
        dest="ensure_ascii",
        action="store_false",
        default=True,
        help="Allow non-ASCII characters in output (default: escape them)",
    )
    yaml_enc.add_argument(
        "-u",
        "--allow-unicode",
        action="store_true",
        help="Keep unicode characters as-is (alias for --no-ensure-ascii)",
    )
    yaml_parse = yaml_parser.add_argument_group("Parsing")
    yaml_parse.add_argument(
        "--strict",
        action="store_true",
        help="Strict YAML parsing (disallow duplicate keys, etc.)",
    )
    yaml_parse.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate YAML, don't output JSON",
    )
    args = parser.parse_args()
    if args.command == "toml":
        for filepath in args.inputs:
            process_toml(filepath)
        return 0
    elif args.command == "xml":
        target_files: list[Path] = []
        if args.inputs:
            for p in args.inputs:
                if p.is_dir():
                    target_files.extend(get_files_in_dir(p, args.exts))
                else:
                    target_files.append(p)
        else:
            target_files = get_files_in_dir(Path.cwd(), args.exts)
        if not target_files:
            print("No XML files found to process.", file=sys.stderr)
            return 1
        with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_xml_file, f, args.engine, args.delete_source) for f in target_files]
            concurrent.futures.wait(futures)
        return 0
    elif args.command == "yaml":
        if yaml is None:
            print(
                "Error: PyYAML is required. Install with: pip install PyYAML",
                file=sys.stderr,
            )
            return 1
        if args.allow_unicode:
            args.ensure_ascii = False
        try:
            if args.input is sys.stdin and sys.stdin.isatty():
                print("Enter YAML content (Ctrl+D to finish):", file=sys.stderr)
            yaml_content = args.input.read()
        except Exception as e:
            print(f"Error reading input: {e}", file=sys.stderr)
            return 1
        if not yaml_content.strip():
            print("Error: Empty YAML input", file=sys.stderr)
            return 1
        try:
            json_str = convert_yaml_to_json_str(
                yaml_content,
                indent=args.indent,
                compact=args.compact,
                sort_keys=args.sort_keys,
                ensure_ascii=args.ensure_ascii,
                strict=args.strict,
            )
        except yaml.YAMLError as e:
            print(f"YAML Error: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"Conversion Error: {e}", file=sys.stderr)
            return 1
        if not args.validate_only:
            try:
                args.output.write(json_str)
                if args.output is sys.stdout:
                    args.output.write("\n")
                args.output.flush()
            except Exception as e:
                print(f"Error writing output: {e}", file=sys.stderr)
                return 1
        else:
            print("✓ YAML is valid", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
