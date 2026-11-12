#!/data/data/com.termux/files/usr/bin/env python
"""Convert a CSV file into a JSON list of row objects.
Reads the CSV with :class:`csv.DictReader` and writes ``<name>.json`` next to the input, indented by four spaces.
Usage: csvtojson INPUT.csv Examples: $ csvtojson table.csv Runs as a script (``python csvtojson.py``) in the directory it should act on; the module can also be imported.
"""

import csv
import json
from pathlib import Path
import sys


def convert_csv_to_json(input_path: Path) -> None:
    output_path = input_path.with_suffix(".json")
    with input_path.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        data = list(reader)
    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(data, json_file, indent=4)


if __name__ == "__main__":
    input_path = Path(sys.argv[1])
    convert_csv_to_json(input_path)
