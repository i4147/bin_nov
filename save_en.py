#!/data/data/com.termux/files/usr/bin/env python
"""Write the English side of a bilingual JSON list to a text file.
Loads the JSON records, writes the ``en`` field of each entry, one per line, to a ``.txt`` file next to the input and prints the path.
Usage: save_en FILE.json Examples: $ save_en pairs.json Runs as a script (``python save_en.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys


def main() -> None:
    input_path = Path(sys.argv[1])
    output_path = input_path.with_suffix(".txt")
    with input_path.open("r", encoding="utf-8") as file:
        records = json.load(file)
    with output_path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(record["en"] + "\n")
    print(f"Saved English text to {output_path}")


if __name__ == "__main__":
    main()
