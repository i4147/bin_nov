#!/data/data/com.termux/files/usr/bin/env python
"""Convert a Java serialized binary into JSON.
Parses the stream with ``javaobj``, turns the Java objects and strings into dictionaries (dropping the internal fields) and writes the list to the requested JSON file.
Usage: java_serialization_data_converter INPUT.bin OUTPUT.json Examples: $ java_serialization_data_converter session.bin session.json Runs as a script (``python java_serialization_data_converter.py``) in the directory it should act on; the module can also be imported."""

import json
from pathlib import Path
import sys

import javaobj


def read_java_serialization(file_path: str) -> list:
    if not Path(file_path).exists():
        msg = f"The file {file_path} does not exist."
        raise FileNotFoundError(msg)
    with open(file_path, "rb") as f:
        binary_data = f.read()
    parsed_objects = javaobj.loads(binary_data)
    return parsed_objects


def default_json_encoder(obj: Any) -> Any:
    if isinstance(obj, javaobj.JavaObject):
        return {
            "__class__": obj.classdesc.name if obj.classdesc else "Unknown",
            "fields": obj.__dict__.get("annotations", {}),
            **{k: v for k, v in obj.__dict__.items() if not k.startswith("_") and k != "classdesc"},
        }
    if isinstance(obj, javaobj.JavaString):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    msg = f"Object of type {type(obj).__name__} is not JSON serializable"
    raise TypeError(msg)


def save_as_json(parsed_data: list, output_path: str) -> None:
    serializable_data = []
    for item in parsed_data:
        if hasattr(item, "to_dict"):
            serializable_data.append(item.to_dict())
        else:
            serializable_data.append(item)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable_data, f, default=default_json_encoder, indent=4, ensure_ascii=False)


def main():
    if len(sys.argv) < 3:
        print("Usage: python java_serializer.py <input_java_serialized_bin> <output_data_json>")
        sys.exit(1)
    input_bin = sys.argv[1]
    output_json = sys.argv[2]
    try:
        print(f"Reading and parsing Java serialization file: {input_bin}...")
        parsed_data = read_java_serialization(input_bin)
        print(f"Saving structured data to: {output_json}...")
        save_as_json(parsed_data, output_json)
        print("Done! Transformation complete.")
    except Exception as e:
        print(f"Error processing serialization data: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
