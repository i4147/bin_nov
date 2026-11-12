#!/data/data/com.termux/files/usr/bin/env python
"""Write every value of a JSON list of objects to its own text file.
Reads the list, skips the entries that are not objects and writes each key/value pair to a ``.txt`` file named after the key, adding an index suffix when the name is already taken.
Usage: split_json INPUT.json Examples: $ split_json records.json Runs as a script (``python split_json.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys


def main() -> None:
    if len(sys.argv) < 2:
        msg = f"Usage: python {Path(sys.argv[0]).name} input.json"
        raise SystemExit(msg)
    input_path = Path(sys.argv[1])
    with input_path.open("r", encoding="utf-8") as f:
        records = json.load(f)
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            print(f"Skipping record {index}: expected a JSON object")
            continue
        for name, content in record.items():
            output_name = Path(name).name
            output_path = Path(output_name).with_suffix(".txt")
            if output_path.exists():
                output_path = output_path.with_name(f"{output_path.stem}_{index}{output_path.suffix}")
            output_path.write_text(str(content), encoding="utf-8")
            print(f"Saved {output_path}")


if __name__ == "__main__":
    main()
