#!/data/data/com.termux/files/usr/bin/env python
"""Create a competitive-programming C++ file from a template and open it in ``$EDITOR``.
Writes a skeleton with ``bits/stdc++.h``, fast I/O and an empty ``main`` to the requested path (refusing to overwrite an existing file), then launches the editor named by ``EDITOR`` (``nano`` by default).
Usage: cppnew FILE.cpp Examples: $ cppnew a.cpp Runs as a script (``python cppnew.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import subprocess
import sys
from typing import Any

TEMPLATE: str = """\
#include <bits/stdc++.h>
using namespace std;
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    // Your code here
    return 0;
}
"""


def cppnew(*args: Any) -> int:
    if not args:
        print("Usage: cppnew <filename.cpp>")
        return 1
    file = Path(args[0]).expanduser()
    if file.exists():
        print(f"Error: {file} already exists", file=sys.stderr)
        return 1
    file.write_text(TEMPLATE)
    print(f"Created {file} with basic C++ template")
    editor = (
        subprocess.run(["sh", "-c", 'printf %s "$EDITOR"'], capture_output=True, text=True).stdout.strip() or "nano"
    )
    try:
        return subprocess.run([editor, str(file)]).returncode
    except FileNotFoundError:
        print(f"Error: editor '{editor}' not found", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(cppnew(*sys.argv[1:]))
