#!/data/data/com.termux/files/usr/bin/env python
"""Empty a file by truncating it to zero bytes.
Writes an empty string to the given path, which clears the content while keeping the file (and its permissions) in place.
Usage: empt FILE Examples: $ empt notes.log Runs as a script (``python empt.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

if __name__ == "__main__":
    Path(sys.argv[1].strip()).write_text("")
